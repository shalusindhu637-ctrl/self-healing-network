import threading
import time
import logging
import random
import subprocess
import sys
from typing import Dict, List, Optional, Callable
from datetime import datetime, timezone
from network_topology import topology, get_network_stats
from database import (
    log_failure_event, log_monitoring_event, 
    get_active_failure_count, update_link_status, update_device_status
)

logger = logging.getLogger(__name__)

class FailureDetector:
    def __init__(self, check_interval: float = 2.0, demo_mode: bool = True):
        self.check_interval = check_interval
        self.demo_mode = demo_mode
        self.running = False
        self.monitor_thread: Optional[threading.Thread] = None
        self.callbacks: List[Callable] = []
        self.last_stats = {}
        self.failure_history = []
    
    def add_callback(self, callback: Callable):
        self.callbacks.append(callback)
    
    def _notify_callbacks(self, event_type: str, data: Dict):
        for callback in self.callbacks:
            try:
                callback(event_type, data)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def start_monitoring(self):
        if self.running:
            return False
        
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        log_monitoring_event("monitoring_started", message="Network monitoring started")
        logger.info("Failure detection started")
        return True
    
    def stop_monitoring(self):
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        log_monitoring_event("monitoring_stopped", message="Network monitoring stopped")
        logger.info("Failure detection stopped")
        return True
    
    def _monitor_loop(self):
        while self.running:
            try:
                self._check_network_health()
                self._simulate_random_failures()
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
            time.sleep(self.check_interval)
    
    def _check_network_health(self):
        stats = get_network_stats()
        self.last_stats = stats
        
        if stats != getattr(self, '_prev_stats', {}):
            self._prev_stats = stats.copy()
            self._notify_callbacks("stats_update", stats)
        
        active_failures = get_active_failure_count()
        if active_failures > 0:
            self._notify_callbacks("active_failures", {"count": active_failures})
    
    def _simulate_random_failures(self):
        if not self.demo_mode:
            return
        
        if random.random() < 0.001:
            self._trigger_random_failure()
    
    def _trigger_random_failure(self):
        import network_topology as nt
        links = [(u, v) for u, v in nt.topology.graph.edges()]
        if links and random.random() < 0.5:
            link = random.choice(links)
            self.simulate_link_failure(link[0], link[1])
        elif nt.topology.hosts:
            host = random.choice(nt.topology.hosts)
            if host not in nt.topology.failed_hosts:
                self.simulate_host_failure(host)
    
    def simulate_link_failure(self, source: str, target: str) -> bool:
        if topology.simulate_link_failure(source, target):
            failure_id = log_failure_event(
                link_source=source, 
                link_target=target, 
                failure_type="link",
                description=f"Link between {source} and {target} failed"
            )
            self._notify_callbacks("link_failure", {
                "source": source,
                "target": target,
                "failure_id": failure_id,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            return True
        return False
    
    def simulate_host_failure(self, host: str) -> bool:
        if topology.simulate_host_failure(host):
            failure_id = log_failure_event(
                device_name=host,
                failure_type="host",
                description=f"Host {host} failed"
            )
            self._notify_callbacks("host_failure", {
                "host": host,
                "failure_id": failure_id,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            return True
        return False
    
    def restore_link(self, source: str, target: str) -> bool:
        if topology.restore_link(source, target):
            self._notify_callbacks("link_restored", {
                "source": source,
                "target": target,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            return True
        return False
    
    def restore_host(self, host: str) -> bool:
        if topology.restore_host(host):
            self._notify_callbacks("host_restored", {
                "host": host,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            return True
        return False
    
    def ping_host(self, host_ip: str) -> bool:
        if self.demo_mode:
            return host_ip not in [f"10.0.0.{i}" for i, h in enumerate(topology.hosts) if h in topology.failed_hosts]
        
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", "1", host_ip],
                capture_output=True,
                timeout=2
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def check_all_hosts(self) -> Dict[str, bool]:
        results = {}
        for host in topology.hosts:
            if host in topology.failed_hosts:
                results[host] = False
            else:
                ip = f"10.0.0.{topology.hosts.index(host)+1}"
                results[host] = self.ping_host(ip)
        return results


detector = FailureDetector()

def start_failure_detection(check_interval: float = 2.0, demo_mode: bool = True):
    # Reconfigure and reuse the existing detector instead of building a new one:
    # replacing it dropped every callback registered via register_callback() and
    # left the previous monitor thread running with no way to stop it, so each
    # /api/monitoring/start call leaked another detection thread.
    detector.check_interval = check_interval
    detector.demo_mode = demo_mode
    if detector.running:
        detector.stop_monitoring()
    return detector.start_monitoring()

def stop_failure_detection():
    return detector.stop_monitoring()

def get_detector():
    return detector

def register_callback(callback: Callable):
    detector.add_callback(callback)