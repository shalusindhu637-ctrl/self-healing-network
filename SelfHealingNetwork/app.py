from flask import Flask, jsonify, render_template, request, send_from_directory
import logging
import threading
import time
from datetime import datetime, timezone

import db_config

# Must happen before `database` is imported, because database.DATABASE_PATH is
# resolved from SHN_DATABASE_PATH at import time.
#
# Running the app directly (`python app.py`) gets the development database, so
# the dev server can never write to the production network.db. An explicit
# SHN_DATABASE_PATH always wins, which is how run_tests.py points the app at a
# throwaway database and how anyone deliberately works on the real data.
DEV_DATABASE_PATH = db_config.use_development_database()

from database import (
    init_database, get_all_devices, get_all_links, get_failure_events,
    get_active_failures, get_failures_for_dashboard, get_recovery_events,
    get_monitoring_logs, get_network_stats_history, log_network_stats,
    clear_all_data
)
from network_topology import (
    initialize_network, get_topology, get_network_stats,
    fail_link, fail_host, restore_link, restore_host,
    find_path, find_alternatives, check_connectivity
)
from failure_detector import (
    start_failure_detection, stop_failure_detection, get_detector,
    register_callback
)
from recovery_manager import (
    initialize_recovery, recover_failure, set_auto_recovery,
    get_recovery_status, verify_recovery
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'self-healing-network-2024'

demo_mode = True
monitoring_callbacks = []

def on_network_event(event_type: str, data: dict):
    for callback in monitoring_callbacks:
        try:
            callback(event_type, data)
        except Exception as e:
            logger.error(f"Event callback error: {e}")

register_callback(on_network_event)

def request_payload():
    """Parsed JSON body as a dict; an absent or malformed body becomes {}."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    # Browsers request /favicon.ico at the site root, which the static route
    # (/static/<path>) does not serve. Answering it from the static folder keeps
    # the icon in one place instead of duplicating the file at the project root.
    return send_from_directory(app.static_folder, 'favicon.ico',
                               mimetype='image/vnd.microsoft.icon')

@app.route('/api/status')
def api_status():
    stats = get_network_stats()
    topology_data = get_topology()
    recovery_status = get_recovery_status()
    detector = get_detector()
    
    active_failures = []
    for failure in get_failure_events(10):
        if not failure.get('resolved', False):
            active_failures.append({
                'id': failure['id'],
                'type': failure['failure_type'],
                'message': failure['description'],
                'timestamp': failure['timestamp']
            })
    
    return jsonify({
        "stats": stats,
        "topology": topology_data,
        "recovery": recovery_status,
        "monitoring": {
            "running": detector.running if detector else False,
            "demo_mode": demo_mode
        },
        "alerts": active_failures,
        # Timezone-aware UTC, like every timestamp the app stores.
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

@app.route('/api/topology')
def api_topology():
    return jsonify(get_topology())

@app.route('/api/stats')
def api_stats():
    stats = get_network_stats()
    log_network_stats(
        stats['total_hosts'], stats['total_switches'],
        stats['active_links'], stats['failed_links'],
        stats['health_percentage']
    )
    return jsonify(stats)

@app.route('/api/devices')
def api_devices():
    return jsonify(get_all_devices())

@app.route('/api/links')
def api_links():
    return jsonify(get_all_links())

@app.route('/api/failures')
def api_failures():
    # ?unresolved=1 returns only the unresolved failures.
    # The default response is the same flat list of event objects as before, but
    # it now keeps *every* unresolved event (not just the ones that happen to be
    # among the newest 50 rows) plus the 50 most recent resolved events, which
    # the dashboard uses to stop a recovered link from re-alerting.
    if request.args.get('unresolved') in ('1', 'true', 'yes', ''):
        return jsonify(get_active_failures())
    return jsonify(get_failures_for_dashboard(50))

@app.route('/api/recoveries')
def api_recoveries():
    return jsonify(get_recovery_events(50))

@app.route('/api/logs')
def api_logs():
    return jsonify(get_monitoring_logs(100))

@app.route('/api/stats/history')
def api_stats_history():
    return jsonify(get_network_stats_history(50))

@app.route('/api/simulate/link_failure', methods=['POST'])
def simulate_link_failure():
    data = request_payload()
    source = data.get('source')
    target = data.get('target')
    
    if not source or not target:
        return jsonify({"success": False, "error": "Source and target required"}), 400
    
    # Route through the detector: it fails the link *and* records the failure
    # event. Failing the link here first would remove the edge, leaving the
    # detector with nothing to record and no row for /api/recovery/trigger.
    detector = get_detector()
    if detector:
        success = detector.simulate_link_failure(source, target)
    else:
        success = fail_link(source, target)
    
    return jsonify({"success": success, "source": source, "target": target})

@app.route('/api/simulate/host_failure', methods=['POST'])
def simulate_host_failure():
    data = request_payload()
    host = data.get('host')
    
    if not host:
        return jsonify({"success": False, "error": "Host required"}), 400
    
    detector = get_detector()
    if detector:
        # The detector fails the host and records the failure event.
        success = detector.simulate_host_failure(host)
    else:
        success = fail_host(host)
    
    return jsonify({"success": success, "host": host})

@app.route('/api/simulate/restore_link', methods=['POST'])
def simulate_restore_link():
    data = request_payload()
    source = data.get('source')
    target = data.get('target')
    
    if not source or not target:
        return jsonify({"success": False, "error": "Source and target required"}), 400
    
    success = restore_link(source, target)
    detector = get_detector()
    if detector:
        detector.restore_link(source, target)
    
    return jsonify({"success": success, "source": source, "target": target})

@app.route('/api/simulate/restore_host', methods=['POST'])
def simulate_restore_host():
    data = request_payload()
    host = data.get('host')
    
    if not host:
        return jsonify({"success": False, "error": "Host required"}), 400
    
    success = restore_host(host)
    detector = get_detector()
    if detector:
        detector.restore_host(host)
    
    return jsonify({"success": success, "host": host})

@app.route('/api/recovery/trigger', methods=['POST'])
def trigger_recovery():
    data = request_payload()
    failure_id = data.get('failure_id')
    source = data.get('source')
    target = data.get('target')
    failure_type = data.get('failure_type', 'link')
    
    if not failure_id or not source or not target:
        return jsonify({"success": False, "error": "Missing required parameters"}), 400
    
    try:
        result = recover_failure(failure_id, source, target, failure_type)
    except Exception as e:
        logger.error(f"Recovery failed for {failure_id}: {e}", exc_info=True)
        return jsonify({
            "success": False,
            "status": "error",
            "error": f"{type(e).__name__}: {e}"
        }), 500
    
    return jsonify(result)

@app.route('/api/recovery/auto', methods=['POST'])
def toggle_auto_recovery():
    data = request_payload()
    enabled = data.get('enabled', True)
    set_auto_recovery(enabled)
    return jsonify({"success": True, "auto_recovery": enabled})

@app.route('/api/monitoring/start', methods=['POST'])
def start_monitoring():
    data = request_payload()
    interval = data.get('interval', 2.0)
    global demo_mode
    demo_mode = data.get('demo_mode', True)
    
    success = start_failure_detection(check_interval=interval, demo_mode=demo_mode)
    return jsonify({"success": success, "interval": interval, "demo_mode": demo_mode})

@app.route('/api/monitoring/stop', methods=['POST'])
def stop_monitoring():
    success = stop_failure_detection()
    return jsonify({"success": success})

@app.route('/api/monitoring/status')
def monitoring_status():
    detector = get_detector()
    return jsonify({
        "running": detector.running if detector else False,
        "demo_mode": demo_mode,
        "check_interval": detector.check_interval if detector else 2.0
    })

@app.route('/api/test/connectivity', methods=['POST'])
def test_connectivity():
    data = request_payload()
    source = data.get('source')
    target = data.get('target')
    
    if not source or not target:
        return jsonify({"success": False, "error": "Source and target required"}), 400
    
    try:
        connected = check_connectivity(source, target)
        path = find_path(source, target) if connected else None
        alternatives = find_alternatives(source, target) if not connected else []
    except Exception as e:
        logger.error(f"Connectivity check failed for {source}-{target}: {e}", exc_info=True)
        return jsonify({"connected": False, "path": None, "alternatives": [],
                        "error": f"{type(e).__name__}: {e}"}), 404
    
    return jsonify({
        "connected": connected,
        "path": path,
        "alternatives": alternatives
    })

@app.route('/api/reset', methods=['POST'])
def reset_network():
    # Optional {"seed": N} rebuilds a reproducible topology; omitting it keeps the
    # previous random behaviour.
    data = request_payload()
    clear_all_data()
    initialize_network(demo_mode=demo_mode, seed=data.get('seed'))
    initialize_recovery(auto_recovery=True, demo_mode=demo_mode)
    return jsonify({"success": True, "message": "Network reset complete"})

def initialize_app():
    # The database file this process resolved once, at import time. Logged so it
    # is obvious which file is being written.
    logger.info("Database in use: %s", DEV_DATABASE_PATH)
    init_database()
    initialize_network(demo_mode=demo_mode)
    initialize_recovery(auto_recovery=True, demo_mode=demo_mode)
    start_failure_detection(check_interval=2.0, demo_mode=demo_mode)
    logger.info("Application initialized")

initialize_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)