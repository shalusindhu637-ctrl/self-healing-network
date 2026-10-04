import logging
import threading
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from network_topology import topology, find_path, find_alternatives, check_connectivity
from database import (
    log_failure_event, log_recovery_event, log_monitoring_event,
    get_active_failure_count
)

logger = logging.getLogger(__name__)

class RecoveryManager:
    def __init__(self, auto_recovery: bool = True, demo_mode: bool = True):
        self.auto_recovery = auto_recovery
        self.demo_mode = demo_mode
        self.recovery_history = []
        self.recovery_in_progress = False
        # Flask serves requests on multiple threads, so the busy guard has to be
        # atomic: two concurrent /api/recovery/trigger calls could otherwise both
        # see recovery_in_progress False and run at the same time.
        self._recovery_lock = threading.Lock()
        self.recovery_strategies = [
            self._strategy_shortest_path,
            self._strategy_least_hops,
            self._strategy_avoid_failed_links
        ]
    
    def set_auto_recovery(self, enabled: bool):
        self.auto_recovery = enabled
        log_monitoring_event("config_change", message=f"Auto recovery set to {enabled}")
    
    def attempt_recovery(self, failure_id: int, source: str, target: str, 
                        failure_type: str = "link") -> Dict:
        if not self._recovery_lock.acquire(blocking=False):
            return {"status": "busy", "message": "Recovery already in progress"}

        self.recovery_in_progress = True
        start_time = time.time()
        
        try:
            result = self._execute_recovery(failure_id, source, target, failure_type)
            duration = time.time() - start_time
            
            if result.get("status") == "success":
                log_recovery_event(
                    failure_event_id=failure_id,
                    alternative_path=" -> ".join(result.get("path", [])),
                    recovery_status="success",
                    description=f"Recovered via {result.get('path')}"
                )
                log_monitoring_event("recovery_success", message=f"Recovery successful in {duration:.2f}s")
            else:
                log_recovery_event(
                    failure_event_id=failure_id,
                    alternative_path="",
                    recovery_status="failed",
                    description=result.get("message", "No alternative path found")
                )
                log_monitoring_event("recovery_failed", message=result.get("message", "Recovery failed"))
            
            return result
        finally:
            self.recovery_in_progress = False
            self._recovery_lock.release()
    
    def _execute_recovery(self, failure_id: int, source: str, target: str, 
                         failure_type: str) -> Dict:
        if failure_type == "host":
            return self._recover_host(failure_id, source)
        else:
            return self._recover_link(failure_id, source, target)
    
    def _recover_link(self, failure_id: int, source: str, target: str) -> Dict:
        logger.info(f"Attempting to recover link {source}-{target}")
        
        alternatives = find_alternatives(source, target)
        
        if not alternatives:
            return {
                "status": "failed",
                "message": f"No alternative path found between {source} and {target}",
                "path": []
            }
        
        best_path = self._select_best_path(alternatives, source, target)
        
        if not best_path:
            return {
                "status": "failed",
                "message": "No viable alternative path found",
                "path": []
            }
        
        if self.demo_mode:
            self._apply_recovery_path(best_path)
        
        log_monitoring_event("recovery_path_found", message=f"Alternative path: {' -> '.join(best_path)}")
        
        return {
            "status": "success",
            "message": f"Traffic rerouted via alternative path",
            "path": best_path,
            "alternatives": alternatives
        }
    
    def _recover_host(self, failure_id: int, host: str) -> Dict:
        logger.info(f"Attempting to recover host {host}")
        
        connected_switches = [n for n in topology.graph.neighbors(host) 
                             if n.startswith('s')]
        
        if not connected_switches:
            return {
                "status": "failed",
                "message": f"Host {host} has no connected switches",
                "path": []
            }
        
        switch = connected_switches[0]
        
        if not topology.graph.has_edge(host, switch):
            return {
                "status": "failed",
                "message": f"Link between {host} and {switch} is down",
                "path": []
            }
        
        if self.demo_mode:
            topology.restore_host(host)
        
        return {
            "status": "success",
            "message": f"Host {host} restored and connected to {switch}",
            "path": [host, switch]
        }
    
    def _select_best_path(self, alternatives: List[List[str]], source: str, target: str) -> Optional[List[str]]:
        if not alternatives:
            return None
        
        scored_paths = []
        for path in alternatives:
            score = self._calculate_path_score(path)
            scored_paths.append((score, path))
        
        scored_paths.sort(key=lambda x: x[0])
        return scored_paths[0][1] if scored_paths else None
    
    def _calculate_path_score(self, path: List[str]) -> float:
        score = 0
        for i in range(len(path) - 1):
            u, v = path[i], path[i+1]
            edge_data = topology.graph.edges.get((u, v), topology.graph.edges.get((v, u), {}))
            score += edge_data.get('weight', 1) * 10
            score += edge_data.get('latency', 0.5) * 5
        
        score += len(path) * 2
        return score
    
    def _apply_recovery_path(self, path: List[str]):
        for i in range(len(path) - 1):
            u, v = path[i], path[i+1]
            if not topology.graph.has_edge(u, v):
                topology.graph.add_edge(u, v, weight=1, latency=0.5)
        
        log_monitoring_event("path_applied", message=f"Applied recovery path: {' -> '.join(path)}")
    
    def _strategy_shortest_path(self, alternatives: List[List[str]]) -> Optional[List[str]]:
        if not alternatives:
            return None
        return min(alternatives, key=len)
    
    def _strategy_least_hops(self, alternatives: List[List[str]]) -> Optional[List[str]]:
        return self._strategy_shortest_path(alternatives)
    
    def _strategy_avoid_failed_links(self, alternatives: List[List[str]]) -> Optional[List[str]]:
        for path in alternatives:
            has_failed = False
            for i in range(len(path) - 1):
                u, v = path[i], path[i+1]
                if (u, v) in topology.failed_links or (v, u) in topology.failed_links:
                    has_failed = True
                    break
            if not has_failed:
                return path
        return alternatives[0] if alternatives else None
    
    def verify_connectivity(self, source: str, target: str) -> bool:
        return check_connectivity(source, target)
    
    def get_recovery_status(self) -> Dict:
        return {
            "auto_recovery_enabled": self.auto_recovery,
            "recovery_in_progress": self.recovery_in_progress,
            "total_recoveries": len(self.recovery_history)
        }


recovery_manager = RecoveryManager()

def initialize_recovery(auto_recovery: bool = True, demo_mode: bool = True):
    global recovery_manager
    recovery_manager = RecoveryManager(auto_recovery=auto_recovery, demo_mode=demo_mode)
    return recovery_manager

def recover_failure(failure_id: int, source: str, target: str, failure_type: str = "link"):
    return recovery_manager.attempt_recovery(failure_id, source, target, failure_type)

def set_auto_recovery(enabled: bool):
    recovery_manager.set_auto_recovery(enabled)

def get_recovery_status():
    return recovery_manager.get_recovery_status()

def verify_recovery(source: str, target: str):
    return recovery_manager.verify_connectivity(source, target)