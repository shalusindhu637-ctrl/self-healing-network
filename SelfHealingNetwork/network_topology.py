import networkx as nx
import random
import logging
from typing import Dict, List, Tuple, Optional, Set
from database import (
    insert_device, insert_link, get_all_devices, get_all_links,
    update_link_status, update_device_status, log_monitoring_event
)

logger = logging.getLogger(__name__)

class NetworkTopology:
    def __init__(self, demo_mode: bool = False):
        self.graph = nx.Graph()
        self.demo_mode = demo_mode
        self.hosts = []
        self.switches = []
        self.links = []
        self.failed_links: Set[Tuple[str, str]] = set()
        self.failed_hosts: Set[str] = set()
        
    def create_topology(self, num_hosts: int = 10, num_switches: int = 4,
                        seed: Optional[int] = None) -> Dict:
        # seed=None keeps the previous behaviour (a different random layout on
        # every call); passing an int makes the mesh, the coordinates and the
        # link set fully reproducible for tests and debugging.
        rng = random.Random(seed)
        self.graph.clear()
        self.hosts = [f"h{i+1}" for i in range(num_hosts)]
        self.switches = [f"s{i+1}" for i in range(num_switches)]
        self.links = []
        self.failed_links.clear()
        self.failed_hosts.clear()
        
        all_devices = self.hosts + self.switches
        for device in all_devices:
            device_type = "host" if device.startswith("h") else "switch"
            ip = f"10.0.0.{self.hosts.index(device)+1}" if device_type == "host" else None
            # Coordinates are stored on the node so get_topology_data() returns the
            # same positions on every call instead of re-randomising them per request.
            self.graph.add_node(device, type=device_type, ip=ip,
                                x=rng.randint(100, 800), y=rng.randint(100, 600))
            insert_device(device, device_type, ip)
        
        self._create_mesh_topology(num_hosts, num_switches, rng)
        
        for u, v in self.graph.edges():
            self.links.append({"source": u, "target": v, "status": "active"})
            insert_link(u, v, "active")
        
        log_monitoring_event("topology_created", message=f"Created topology with {num_hosts} hosts and {num_switches} switches")
        
        return self.get_topology_data()
    
    def _create_mesh_topology(self, num_hosts: int, num_switches: int,
                              rng: Optional[random.Random] = None):
        generator = rng or random
        hosts_per_switch = num_hosts // num_switches
        remainder = num_hosts % num_switches
        
        host_idx = 0
        for i, switch in enumerate(self.switches):
            count = hosts_per_switch + (1 if i < remainder else 0)
            for _ in range(count):
                if host_idx < num_hosts:
                    host = self.hosts[host_idx]
                    self.graph.add_edge(host, switch, weight=1, latency=0.5)
                    host_idx += 1
        
        for i in range(num_switches):
            for j in range(i+1, num_switches):
                if generator.random() > 0.3:
                    self.graph.add_edge(self.switches[i], self.switches[j], weight=1, latency=1.0)
        
        for i in range(num_switches - 1):
            self.graph.add_edge(self.switches[i], self.switches[i+1], weight=1, latency=1.0)
    
    def get_topology_data(self) -> Dict:
        nodes = []
        edges = []
        
        for node in self.graph.nodes():
            node_data = self.graph.nodes[node]
            status = "failed" if node in self.failed_hosts else "active"
            # Fall back to assigning once (and persisting) for any node created
            # outside create_topology; never re-roll on each call.
            if node_data.get("x") is None:
                node_data["x"] = random.randint(100, 800)
            if node_data.get("y") is None:
                node_data["y"] = random.randint(100, 600)
            nodes.append({
                "id": node,
                "label": node,
                "type": node_data.get("type", "unknown"),
                "ip": node_data.get("ip"),
                "status": status,
                "x": node_data["x"],
                "y": node_data["y"]
            })
        
        for u, v in self.graph.edges():
            edge_data = self.graph.edges[u, v]
            failed = (u, v) in self.failed_links or (v, u) in self.failed_links
            edges.append({
                "source": u,
                "target": v,
                "status": "failed" if failed else "active",
                "weight": edge_data.get("weight", 1),
                "latency": edge_data.get("latency", 0.5)
            })
        
        return {
            "nodes": nodes,
            "edges": edges,
            "hosts": self.hosts,
            "switches": self.switches
        }
    
    def simulate_link_failure(self, source: str, target: str) -> bool:
        if not self.graph.has_edge(source, target):
            return False
        
        self.failed_links.add((source, target))
        self.graph.remove_edge(source, target)
        update_link_status(source, target, "failed")
        log_monitoring_event("link_failure", device_name=source, 
                           message=f"Link {source}-{target} failed")
        return True
    
    def simulate_host_failure(self, host: str) -> bool:
        # Only hosts can fail here. Accepting any graph node let a switch
        # (s1, s2, ...) enter failed_hosts, and since that count was subtracted
        # from total_hosts it reported active_hosts below the number of hosts
        # that were actually up.
        if host not in self.hosts:
            return False
        
        self.failed_hosts.add(host)
        update_device_status(host, "failed")
        log_monitoring_event("host_failure", device_name=host, 
                           message=f"Host {host} failed")
        return True
    
    def restore_link(self, source: str, target: str) -> bool:
        if (source, target) not in self.failed_links and (target, source) not in self.failed_links:
            return False
        
        self.failed_links.discard((source, target))
        self.failed_links.discard((target, source))
        
        if not self.graph.has_edge(source, target):
            self.graph.add_edge(source, target, weight=1, latency=0.5)
        
        update_link_status(source, target, "active")
        log_monitoring_event("link_restored", device_name=source, 
                           message=f"Link {source}-{target} restored")
        return True
    
    def restore_host(self, host: str) -> bool:
        if host not in self.failed_hosts:
            return False
        
        self.failed_hosts.discard(host)
        update_device_status(host, "active")
        log_monitoring_event("host_restored", device_name=host, 
                           message=f"Host {host} restored")
        return True
    
    def find_shortest_path(self, source: str, target: str) -> Optional[List[str]]:
        try:
            return nx.shortest_path(self.graph, source, target, weight='weight')
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
    
    def find_alternative_paths(self, source: str, target: str, max_paths: int = 3) -> List[List[str]]:
        paths = []
        try:
            for path in nx.shortest_simple_paths(self.graph, source, target, weight='weight'):
                paths.append(path)
                if len(paths) >= max_paths:
                    break
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            pass
        return paths
    
    def get_network_stats(self) -> Dict:
        total_links = len(self.links)
        failed_count = len(self.failed_links)
        active_links = total_links - failed_count
        total_hosts = len(self.hosts)
        failed_hosts = len(self.failed_hosts)
        active_hosts = total_hosts - failed_hosts
        total_switches = len(self.switches)
        
        # Link health alone stayed at 100% while hosts were down: failing a host
        # leaves its links in the graph, so active_links/total_links cannot see
        # it. Host availability is therefore applied as a second factor. It is
        # exactly 100% while every host is up, so link-only health is unchanged
        # whenever no host is down and the existing link behaviour is preserved.
        link_health = (active_links / total_links) * 100 if total_links > 0 else 0
        host_health = (active_hosts / total_hosts) * 100 if total_hosts > 0 else 100
        health = (link_health / 100) * host_health
        
        return {
            "total_hosts": total_hosts,
            "active_hosts": active_hosts,
            "failed_hosts": failed_hosts,
            "total_switches": total_switches,
            "total_links": total_links,
            "active_links": active_links,
            "failed_links": failed_count,
            "health_percentage": round(health, 2)
        }
    
    def is_connected(self, source: str, target: str) -> bool:
        return nx.has_path(self.graph, source, target)


topology = NetworkTopology()

def initialize_network(demo_mode: bool = False, seed: Optional[int] = None):
    # Reset the existing instance instead of rebinding the global: failure_detector
    # and recovery_manager bind this object with "from network_topology import topology"
    # at import time, so a new object here would leave them holding a stale graph.
    topology.demo_mode = demo_mode
    return topology.create_topology(seed=seed)

def get_topology():
    return topology.get_topology_data()

def get_network_stats():
    return topology.get_network_stats()

def fail_link(source: str, target: str):
    return topology.simulate_link_failure(source, target)

def fail_host(host: str):
    return topology.simulate_host_failure(host)

def restore_link(source: str, target: str):
    return topology.restore_link(source, target)

def restore_host(host: str):
    return topology.restore_host(host)

def find_path(source: str, target: str):
    return topology.find_shortest_path(source, target)

def find_alternatives(source: str, target: str):
    return topology.find_alternative_paths(source, target)

def check_connectivity(source: str, target: str):
    return topology.is_connected(source, target)