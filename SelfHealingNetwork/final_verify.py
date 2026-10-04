"""final_verify.py - end-to-end verification for the Self-Healing Network project.

Run from CMD:

    cd /d C:\\SelfHealingNetwork
    python final_verify.py

What it does
------------
1. Snapshots the real network.db READ-ONLY (sqlite mode=ro) and compares the row
   counts afterwards, so it can prove it never wrote to it.
2. Creates a throwaway database in a temporary folder OUTSIDE the project and
   starts the real Flask app against it on port 5097 (never the dev port 5000).
3. Checks the development database configuration (python app.py must resolve to
   network_dev.db, and an explicit SHN_DATABASE_PATH must win).
4. Exercises the API: topology, failed_hosts health arithmetic, failure
   detection, recovery, alert de-duplication and the Event History log.
5. Drives real Chrome over the DevTools Protocol: three viewports, real clicks on
   Simulate Failure / Recover Network, status correctness against the live API,
   console errors and API responses.

Every check prints PASS, FAIL or NOT TESTED. A check is only PASS when it was
actually executed; anything skipped is reported as NOT TESTED, never as a pass.
The temporary database and the temporary browser profile are deleted at the end.
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PROJECT = os.path.dirname(os.path.abspath(__file__))
REAL_DB = os.path.join(PROJECT, "network.db")
DEV_DB = os.path.join(PROJECT, "network_dev.db")
DEV_SERVER_PORT = 5000
PORT = int(os.environ.get("SHN_VERIFY_PORT", "5097"))
BASE = f"http://127.0.0.1:{PORT}"
ENV_VAR = "SHN_DATABASE_PATH"
TABLES = ["devices", "links", "failure_events", "recovery_events",
          "monitoring_logs", "network_stats"]

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


# --------------------------------------------------------------------- report

RESULTS = []


def record(status, name, detail=""):
    RESULTS.append({"status": status, "name": name, "detail": detail})
    print(f"{status:<11} {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def check(name, ok, detail=""):
    record("PASS" if ok else "FAIL", name, detail)
    return bool(ok)


def skipped(name, detail):
    record("NOT TESTED", name, detail)


def section(title):
    print()
    print(f"--- {title} " + "-" * max(0, 62 - len(title)), flush=True)


# ------------------------------------------------------------------ utilities

def snapshot(path=REAL_DB):
    """Read-only row counts. mode=ro makes an accidental write an error."""
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {t: (conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      if t in present else None) for t in TABLES}
        counts["__size__"] = os.stat(path).st_size
        return counts
    finally:
        conn.close()


def diff_snapshots(before, after):
    if before is None or after is None:
        return ["database missing in the before or after snapshot"]
    return [f"{k}: {before.get(k)} -> {after.get(k)}"
            for k in sorted(set(before) | set(after))
            if k != "__size__" and before.get(k) != after.get(k)]


def health(status_payload):
    """The health percentage lives in stats.health_percentage, not at the top."""
    return status_payload["stats"]["health_percentage"]


def api(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def api_post(path, payload=None, timeout=60):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, {"raw": raw[:200]}


def run_python(code, env=None, cwd=PROJECT, timeout=180):
    """Run a snippet in a fresh interpreter; returns (rc, stdout, stderr)."""
    done = subprocess.run([sys.executable, "-c", code], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=timeout)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_for_server(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/api/status", timeout=3):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def fresh_env(**overrides):
    """A child environment without SHN_DATABASE_PATH unless asked for."""
    env = dict(os.environ)
    env["PYTHONPATH"] = PROJECT
    env["SHN_VERIFY_BASE"] = BASE
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


# ------------------------------------------------------- 1. dev db configuration

def verify_dev_database(workdir):
    section("1. development database configuration")
    rc, out, err = run_python(
        "import db_config;"
        "print(db_config.use_development_database())",
        env=fresh_env(**{ENV_VAR: None}))
    check("db_config.use_development_database() resolves to network_dev.db",
          rc == 0 and out.lower().endswith("network_dev.db"), out or err[-160:])

    rc, out, err = run_python(
        "import db_config;"
        "p = db_config.use_development_database();"
        "print(p, db_config.is_production_database(p))",
        env=fresh_env(**{ENV_VAR: None}))
    check("the development database is never reported as production",
          rc == 0 and out.endswith("False"), out or err[-160:])

    # The development target can be pointed somewhere else, but the file name is
    # always network_dev.db - a temp folder proves the logic without touching the
    # project's own development database.
    alt = os.path.join(workdir, "altdev")
    os.makedirs(alt, exist_ok=True)
    done = subprocess.run(
        [sys.executable, "-c",
         "import db_config, sys;print(db_config.use_development_database(sys.argv[1]))", alt],
        cwd=PROJECT, env=fresh_env(**{ENV_VAR: None}),
        capture_output=True, text=True, timeout=120)
    ok = done.returncode == 0 and done.stdout.strip().lower() == os.path.join(
        alt, "network_dev.db").lower()
    check("use_development_database(<dir>) targets <dir>\\network_dev.db", ok,
          done.stdout.strip() or done.stderr.strip()[-160:])

    done = subprocess.run(
        [sys.executable, "-c",
         "import db_config, os, sys;"
         "db_config.use_development_database(sys.argv[1]);"
         "os.environ['X']='1';"
         "db_config.os.environ[db_config.ENV_VAR] = sys.argv[2];"
         "print(db_config.resolve_database_path())", alt, os.path.join(workdir, "explicit.db")],
        cwd=PROJECT, env=fresh_env(**{ENV_VAR: None}),
        capture_output=True, text=True, timeout=120)
    ok = done.returncode == 0 and done.stdout.strip().lower() == os.path.join(
        workdir, "explicit.db").lower()
    check("an explicit SHN_DATABASE_PATH is respected (not overridden by the dev default)",
          ok, done.stdout.strip() or done.stderr.strip()[-160:])

    done = subprocess.run(
        [sys.executable, "-c", "import database"],
        cwd=PROJECT, env=fresh_env(**{ENV_VAR: None}),
        capture_output=True, text=True, timeout=120)
    check("importing database with no SHN_DATABASE_PATH fails loudly, never defaulting to network.db",
          done.returncode != 0 and "DatabaseConfigurationError" in done.stderr,
          f"rc={done.returncode}")

    # What `python app.py` really resolves to. This initialises the development
    # database (that is its purpose); network.db is untouched.
    done = subprocess.run(
        [sys.executable, "-c",
         "import app, database, db_config;"
         "p = database.DATABASE_PATH;"
         "print(p);print(db_config.is_production_database(p))"],
        cwd=PROJECT, env=fresh_env(**{ENV_VAR: None}),
        capture_output=True, text=True, timeout=180)
    parts = done.stdout.strip().splitlines()
    resolved = parts[0] if parts else ""
    is_prod = parts[1] if len(parts) > 1 else ""
    check("python app.py with no SHN_DATABASE_PATH resolves to network_dev.db",
          done.returncode == 0 and resolved.lower() == DEV_DB.lower(),
          resolved or done.stderr.strip()[-200:])
    check("python app.py does NOT resolve to network.db",
          done.returncode == 0 and resolved.lower() != REAL_DB.lower() and is_prod == "False",
          f"path={resolved} is_production={is_prod}")
    if os.path.exists(DEV_DB):
        conn = sqlite3.connect("file:" + DEV_DB.replace("\\", "/") + "?mode=ro", uri=True)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        check("the development database exists and has every table",
              {"devices", "links", "failure_events", "recovery_events",
               "monitoring_logs"} <= names,
              f"{DEV_DB} tables={sorted(names)}")
    else:
        skipped("the development database exists and has every table",
                f"{DEV_DB} not created")


# ------------------------------------------------------------- 2. API behaviour

def verify_api():
    section("2. topology, failure detection, recovery and event history (temporary database)")

    status, body = api_post("/api/reset")
    check("POST /api/reset rebuilds the seeded mesh",
          status == 200 and body.get("success") is True, f"{status} {body}")

    topology = api("/api/topology")
    nodes = topology.get("nodes", [])
    edges = topology.get("edges", [])
    hosts = [n for n in nodes if n.get("type") != "switch"]
    switches = [n for n in nodes if n.get("type") == "switch"]
    check("topology returns 10 hosts and 4 switches",
          len(hosts) == 10 and len(switches) == 4,
          f"hosts={len(hosts)} switches={len(switches)}")
    check("topology returns at least 13 links", len(edges) >= 13, f"{len(edges)} edges")
    check("every node has numeric x/y coordinates",
          all(isinstance(n.get("x"), (int, float)) and isinstance(n.get("y"), (int, float))
              for n in nodes), f"{len(nodes)} nodes")

    again = api("/api/topology")
    stable = all(a.get("id") == b.get("id") and a.get("x") == b.get("x") and a.get("y") == b.get("y")
                 for a, b in zip(nodes, again.get("nodes", [])))
    check("coordinates are stable across two calls (no jitter for the frontend cache)",
          stable, f"{len(nodes)} nodes compared")

    base = api("/api/status")
    check("a clean network reports 100% health and 0 failed hosts",
          health(base) == 100.0 and base["stats"]["failed_hosts"] == 0,
          f"health={health(base)} failed_hosts={base['stats']['failed_hosts']}")

    # failed_hosts must be subtracted from the health percentage, once per host.
    api_post("/api/simulate/host_failure", {"host": "h3"})
    one = api("/api/status")
    check("one host down lowers health by exactly that host's share",
          abs(health(one) - 90.0) < 0.01 and one["stats"]["failed_hosts"] == 1,
          f"health 100 -> {health(one)} failed_hosts={one['stats']['failed_hosts']}")
    api_post("/api/simulate/host_failure", {"host": "h8"})
    two = api("/api/status")
    check("two hosts down gives 80% health",
          abs(health(two) - 80.0) < 0.01 and two["stats"]["failed_hosts"] == 2,
          f"health={health(two)} failed_hosts={two['stats']['failed_hosts']}")
    api_post("/api/simulate/host_failure", {"host": "h3"})
    dup = api("/api/status")
    check("a repeated failure of the same host is counted once",
          dup["stats"]["failed_hosts"] == 2, f"failed_hosts={dup['stats']['failed_hosts']}")
    api_post("/api/simulate/restore_host", {"host": "h3"})
    api_post("/api/simulate/restore_host", {"host": "h8"})
    restored = api("/api/status")
    check("restoring the hosts returns health to the 100% baseline",
          health(restored) == 100.0 and restored["stats"]["failed_hosts"] == 0,
          f"health={health(restored)} failed_hosts={restored['stats']['failed_hosts']}")

    api_post("/api/reset")
    links = api("/api/links")
    # /api/links rows come straight out of the links table: source_device /
    # target_device, not source / target.
    target = next(l for l in links if l["status"] == "active"
                  and l["source_device"].startswith("s")
                  and l["target_device"].startswith("s"))
    src, dst = target["source_device"], target["target_device"]
    status, body = api_post("/api/simulate/link_failure", {"source": src, "target": dst})
    check("simulating a link failure succeeds",
          status == 200 and body.get("success") is True, f"{status} {body}")
    failures = api("/api/failures?unresolved=1")
    mine = [f for f in failures if f.get("link_source") == src
            and f.get("link_target") == dst]
    check("the failed link produces exactly one unresolved failure event with an id",
          len(mine) == 1 and isinstance(mine[0].get("id"), int),
          f"events={[f.get('id') for f in mine]}")
    after = api("/api/links")
    marked = [l for l in after if l["source_device"] == src and l["target_device"] == dst]
    check("/api/links reports the link as failed",
          len(marked) == 1 and marked[0]["status"] == "failed",
          f"{src}-{dst} status={marked[0]['status'] if marked else 'missing'}")

    api_post("/api/simulate/link_failure", {"source": src, "target": dst})
    again_failures = [f for f in api("/api/failures?unresolved=1")
                      if f.get("link_source") == src and f.get("link_target") == dst]
    check("re-reporting the same pair does not create a duplicate unresolved event",
          len(again_failures) == 1, f"events={[f.get('id') for f in again_failures]}")

    status, rec = api_post("/api/recovery/trigger",
                           {"failure_id": mine[0]["id"], "source": src, "target": dst,
                            "failure_type": "link"})
    check("recovery on a redundant link succeeds and returns an alternative path",
          status == 200 and rec.get("status") == "success" and bool(rec.get("path")),
          f"{status} status={rec.get('status')} path={rec.get('path')}")
    check("recovery reports the traffic was rerouted",
          "rerout" in str(rec.get("message", "")).lower(), str(rec.get("message"))[:90])
    resolved = [f for f in api("/api/failures?unresolved=1")
                if f.get("id") == mine[0]["id"]]
    check("the failure event is marked resolved after a successful recovery",
          not resolved, f"still unresolved={len(resolved)}")
    recovery_events = api("/api/recoveries")
    check("a recovery_events row was written",
          any(r.get("failure_event_id") == mine[0]["id"] for r in recovery_events),
          f"{len(recovery_events)} rows")
    status_now = api("/api/status")
    check("health is NOT restored by a reroute alone (link still down)",
          health(status_now) < 100.0, f"health={health(status_now)}")

    # A leaf link has no alternative path: must be a clean JSON failure, not a 500.
    leaf = next((l for l in api("/api/links") if l["status"] == "active"
                 and l["source_device"].startswith("h")
                 and l["target_device"].startswith("s")), None)
    if leaf:
        api_post("/api/simulate/link_failure",
                 {"source": leaf["source_device"], "target": leaf["target_device"]})
        leaf_src, leaf_dst = leaf["source_device"], leaf["target_device"]
        leaf_failures = [f for f in api("/api/failures?unresolved=1")
                         if f.get("link_source") == leaf_src
                         and f.get("link_target") == leaf_dst]
        if leaf_failures:
            status, body = api_post("/api/recovery/trigger",
                                    {"failure_id": leaf_failures[0]["id"],
                                     "source": leaf_src, "target": leaf_dst,
                                     "failure_type": "link"})
            check("a leaf link with no alternative path fails cleanly (HTTP 200 JSON, no 500 page)",
                  status == 200 and body.get("status") == "failed" and bool(body.get("message")),
                  f"{status} status={body.get('status')} message={body.get('message')}")
            still = [f for f in api("/api/failures?unresolved=1")
                     if f.get("id") == leaf_failures[0]["id"]]
            check("an unrecoverable failure is NOT marked resolved",
                  len(still) == 1, f"still unresolved={len(still)}")
        else:
            skipped("a leaf link with no alternative path fails cleanly",
                    "the leaf link did not register a failure event")

    status, body = api_post("/api/simulate/link_failure", {"source": "s1"})
    check("a malformed simulate request returns 400 JSON, not an HTML error page",
          status == 400 and isinstance(body, dict) and "error" in body,
          f"{status} {body}")

    logs = api("/api/logs")
    required = {"event_type", "device_name", "message", "timestamp"}
    check("/api/logs (Event History) returns rows with the four displayed fields",
          isinstance(logs, list) and len(logs) > 0
          and all(required <= set(row) for row in logs),
          f"{len(logs)} rows, fields={sorted(logs[0]) if logs else None}")
    check("every Event History timestamp is timezone-aware UTC",
          all(str(row.get("timestamp", "")).endswith("+00:00") for row in logs),
          str(logs[0].get("timestamp")) if logs else "no rows")
    before_count = len(logs)
    api_post("/api/simulate/host_failure", {"host": "h2"})
    time.sleep(1.2)
    grown = api("/api/logs")
    newest = grown[0] if grown else {}
    check("a new failure is appended to Event History and comes first",
          len(grown) >= before_count and "h2" in json.dumps(newest),
          f"{before_count} -> {len(grown)} rows, newest={json.dumps(newest)[:100]}")

    alerts = api("/api/status")["alerts"]
    pairs = [(a.get("type"), a.get("message")) for a in alerts]
    check("/api/status does not report the same alert twice",
          len(pairs) == len(set(pairs)), f"{len(pairs)} alerts, {len(set(pairs))} distinct")

    monitoring_ok = True
    api_post("/api/monitoring/stop")
    stopped = api("/api/status")
    api_post("/api/monitoring/start", {"interval": 2, "demo_mode": True})
    started = api("/api/status")
    monitoring_ok = (stopped["monitoring"]["running"] is False
                     and started["monitoring"]["running"] is True)
    check("stop/start monitoring is reflected in /api/status", monitoring_ok,
          f"stopped={stopped['monitoring']} started={started['monitoring']}")


# ------------------------------------------------------- 3. real browser checks

BROWSER_JS = r"""
// Written into the temporary work folder by final_verify.py and run with node.
// Drives real Chrome over the DevTools Protocol. Reports one JSON line per check.
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = process.env.SHN_VERIFY_BASE;
const CHROME = process.env.SHN_VERIFY_CHROME;
const PORT = parseInt(process.env.SHN_VERIFY_CDP_PORT || '9335', 10);
const OUT = process.env.SHN_VERIFY_JS_OUT;
const SHOTS = process.env.SHN_VERIFY_SHOT_DIR;

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
function record(name, ok, detail) {
    results.push({ status: ok === null ? 'NOT TESTED' : (ok ? 'PASS' : 'FAIL'), name, detail: detail || '' });
}
async function fetchJson(url, attempts = 60) {
    for (let i = 0; i < attempts; i += 1) {
        try { const r = await fetch(url); if (r.ok) return r.json(); } catch (e) {}
        await wait(500);
    }
    throw new Error('server never came up: ' + url);
}
function connect(wsUrl) {
    const socket = new WebSocket(wsUrl);
    const pending = new Map(); const events = []; let nextId = 1;
    socket.addEventListener('message', (e) => {
        const m = JSON.parse(e.data);
        if (m.id && pending.has(m.id)) {
            const p = pending.get(m.id); pending.delete(m.id);
            if (m.error) p.reject(new Error(JSON.stringify(m.error))); else p.resolve(m.result);
        } else if (m.method) events.push(m);
    });
    const ready = new Promise((res, rej) => {
        socket.addEventListener('open', res); socket.addEventListener('error', rej);
    });
    const send = (method, params) => {
        const id = nextId; nextId += 1;
        return new Promise((resolve, reject) => {
            pending.set(id, { resolve, reject });
            socket.send(JSON.stringify({ id, method, params: params || {} }));
        });
    };
    return { ready, send, events, close: () => socket.close() };
}
async function ev(client, expression) {
    const r = await client.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('page threw: ' + JSON.stringify(r.exceptionDetails));
    return r.result.value;
}
async function api(pathname) { return (await fetch(BASE + pathname)).json(); }

const LAYOUT = `(() => {
    const q = (s) => document.querySelector(s);
    const R = (el) => el ? el.getBoundingClientRect() : null;
    const side = R(q('#sidebar')), svg = R(q('#topologyCanvas')), main = q('#mainContent');
    const canvas = q('#healthChart'), cbox = R(canvas);
    let painted = 0;
    try {
        const d = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        for (let i = 3; i < d.length; i += 4) if (d[i] !== 0) painted += 1;
    } catch (e) { painted = -1; }
    return {
        iw: window.innerWidth, ih: window.innerHeight,
        scrollW: document.documentElement.scrollWidth,
        clientW: document.documentElement.clientWidth,
        bodyW: document.body.scrollWidth,
        sidebarLeft: side ? Math.round(side.left) : null,
        mainMargin: main ? getComputedStyle(main).marginLeft : '',
        svgW: svg ? Math.round(svg.width) : 0, svgH: svg ? Math.round(svg.height) : 0,
        svgRight: svg ? Math.round(svg.right) : null,
        nodes: document.querySelectorAll('#topologyCanvas .topology-node').length,
        labels: document.querySelectorAll('#topologyCanvas text.topology-label').length,
        cards: Array.from(document.querySelectorAll('.stat-card')).map((el) => {
            const r = el.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) };
        }),
        controls: Array.from(document.querySelectorAll('button, select, input')).map((el) => {
            const r = el.getBoundingClientRect();
            return { id: el.id || '', visible: r.width > 0 && r.height > 0, right: Math.round(r.right) };
        }),
        canvasW: cbox ? Math.round(cbox.width) : 0, painted: painted,
        statValue: q('#cardTotalHosts') ? getComputedStyle(q('#cardTotalHosts')).fontSize : '0px',
        statLabel: q('#summaryCards .stat-label') ? getComputedStyle(q('#summaryCards .stat-label')).fontSize : '0px',
        skeletons: Array.from(document.querySelectorAll('.is-loading')).map((el) => el.id)
    };
})()`;

const GRAPH = `(() => {
    const svg = document.getElementById('topologyCanvas');
    const nodes = Array.from(svg.querySelectorAll('.topology-node'));
    const edges = Array.from(svg.querySelectorAll('.topology-edge'));
    const cs = (el) => getComputedStyle(el);
    const host = nodes.find((n) => n.classList.contains('host'));
    const sw = nodes.find((n) => n.classList.contains('switch'));
    const active = edges.find((e) => !e.classList.contains('failed'));
    return {
        nodeCount: nodes.length,
        failedNodes: nodes.filter((n) => n.classList.contains('failed'))
            .map((n) => n.getAttribute('data-node')),
        hostFill: host ? cs(host).fill : '', switchFill: sw ? cs(sw).fill : '',
        edgeCount: edges.length,
        failedEdges: edges.filter((e) => e.classList.contains('failed')).map((e) => ({
            pair: e.getAttribute('data-source') + '-' + e.getAttribute('data-target'),
            stroke: cs(e).stroke, dash: cs(e).strokeDasharray
        })),
        activeStroke: active ? cs(active).stroke : '',
        labels: svg.querySelectorAll('text.topology-label').length,
        tooltips: nodes.map((n) => (n.querySelector('title') ? n.querySelector('title').textContent : ''))
    };
})()`;

(async () => {
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'shn-verify-chrome-'));
    fs.mkdirSync(SHOTS, { recursive: true });
    const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run',
        '--no-default-browser-check', '--disable-extensions', '--hide-scrollbars',
        '--window-size=1440,900', '--user-data-dir=' + profile,
        '--remote-debugging-port=' + PORT, 'about:blank'], { stdio: 'ignore' });
    let client;
    try {
        const targets = await fetchJson('http://127.0.0.1:' + PORT + '/json/list');
        client = connect(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
        await client.ready;
        await client.send('Runtime.enable');
        await client.send('Page.enable');
        await client.send('Log.enable');
        await client.send('Network.enable');

        const waitNodes = async (n, ms) => {
            const end = Date.now() + ms;
            while (Date.now() < end) {
                if (await ev(client, "document.querySelectorAll('#topologyCanvas .topology-node').length") >= n) return true;
                await wait(700);
            }
            return false;
        };
        const waitValue = async (expr, ms) => {
            const end = Date.now() + ms;
            while (Date.now() < end) {
                const v = await ev(client, '(' + expr + ')');
                if (v) return v;
                await wait(500);
            }
            return null;
        };
        const setVp = (w, h) => client.send('Emulation.setDeviceMetricsOverride',
            { width: w, height: h, deviceScaleFactor: 1, mobile: false });
        const shoot = async (name) => {
            const s = await client.send('Page.captureScreenshot', { format: 'png' });
            fs.writeFileSync(path.join(SHOTS, name + '.png'), Buffer.from(s.data, 'base64'));
        };
        await fetch(BASE + '/api/reset', { method: 'POST' });

        // ---- 1. responsive layout at three viewports
        for (const vp of [
            { n: 'desktop', w: 1440, h: 900, side: true },
            { n: 'tablet', w: 820, h: 1180, side: false },
            { n: 'mobile', w: 390, h: 844, side: false }
        ]) {
            await setVp(vp.w, vp.h);
            await client.send('Page.navigate', { url: BASE + '/' });
            const ready = await waitNodes(14, 60000);
            await wait(1200);
            const m = await ev(client, LAYOUT);
            const tag = 'browser: responsive ' + vp.n + ' ' + vp.w + 'x' + vp.h;
            record(tag + ' renders the graph', ready && m.nodes === 14 && m.labels === 14,
                'nodes=' + m.nodes + ' labels=' + m.labels);
            record(tag + ' has no horizontal overflow',
                m.scrollW <= m.clientW + 1 && m.bodyW <= m.iw + 1,
                'scrollWidth=' + m.scrollW + ' clientWidth=' + m.clientW + ' body=' + m.bodyW);
            if (vp.side) {
                record(tag + ' sidebar fixed, content offset 260px',
                    m.sidebarLeft >= -1 && Math.abs(parseFloat(m.mainMargin) - 260) < 1,
                    'sidebarLeft=' + m.sidebarLeft + ' margin=' + m.mainMargin);
            } else {
                record(tag + ' sidebar off-canvas, content offset removed',
                    m.sidebarLeft < -100 && Math.abs(parseFloat(m.mainMargin)) < 1,
                    'sidebarLeft=' + m.sidebarLeft + ' margin=' + m.mainMargin);
                const t = m.controls.find((c) => c.id === 'sidebarToggle');
                record(tag + ' sidebar toggle is on screen',
                    !!(t && t.visible && t.right <= m.iw), JSON.stringify(t));
            }
            record(tag + ' topology svg fits the viewport',
                m.svgW > 100 && m.svgH > 100 && m.svgRight <= m.iw + 1,
                'svg=' + m.svgW + 'x' + m.svgH + ' right=' + m.svgRight);
            record(tag + ' four stat cards laid out',
                m.cards.length === 4 && m.cards.every((c) => c.w > 40 && c.h > 40),
                JSON.stringify(m.cards));
            const clipped = m.controls.filter((c) => c.visible && c.right > m.iw + 1);
            record(tag + ' no visible control is clipped off-screen', clipped.length === 0,
                clipped.length ? JSON.stringify(clipped) : m.controls.filter((c) => c.visible).length + ' controls ok');
            record(tag + ' health chart canvas painted', m.canvasW > 100 && m.painted > 500,
                'width=' + m.canvasW + ' paintedPixels=' + m.painted);
            record(tag + ' type stays legible',
                parseFloat(m.statValue) >= 24 && parseFloat(m.statLabel) >= 10,
                'statValue=' + m.statValue + ' statLabel=' + m.statLabel);
            record(tag + ' loading skeletons cleared after first paint',
                m.skeletons.length === 0, JSON.stringify(m.skeletons));
            await shoot('viewport-' + vp.n);
        }

        // ---- 2. real clicks: simulate failure, recovery, host failure
        await setVp(1440, 900);
        await client.send('Page.navigate', { url: BASE + '/' });
        await waitNodes(14, 60000);
        await fetch(BASE + '/api/reset', { method: 'POST' });
        await client.send('Page.reload', { ignoreCache: true });
        await waitNodes(14, 60000);
        await wait(1500);

        const clean = await ev(client, GRAPH);
        record('browser: a clean network draws no failed edge or node',
            clean.failedEdges.length === 0 && clean.failedNodes.length === 0,
            'edges=' + clean.edgeCount + ' nodes=' + clean.nodeCount);
        record('browser: every node has a hover tooltip title',
            clean.tooltips.length > 0 && clean.tooltips.every((t) => t.length > 0),
            clean.tooltips.slice(0, 2).join(' | '));

        await ev(client, "document.getElementById('btnSimulateFailure').click(), true");
        const toast = await waitValue(`(() => {
            const t = Array.from(document.querySelectorAll('.toast .toast-body')).map((e) => e.textContent).join(' || ');
            return /is down/i.test(t) ? t : null; })()`, 25000);
        record('browser: Simulate Failure click raises a success toast', !!toast,
            toast ? toast.slice(0, 100) : 'no toast');
        const failed = await waitValue(
            "document.querySelectorAll('#topologyCanvas .topology-edge.failed').length || null", 25000);
        record('browser: the failed link is drawn as a failed edge', !!failed,
            failed ? failed + ' failed edge(s)' : 'none drawn');

        const links = await api('/api/links');
        const topo = await api('/api/topology');
        const status = await api('/api/status');
        const g1 = await ev(client, GRAPH);
        const apiFailed = links.filter((l) => l.status === 'failed');
        record('browser: failed edges drawn equal the failed links the API reports',
            g1.failedEdges.length === apiFailed.length,
            'ui=' + g1.failedEdges.length + ' api=' + apiFailed.length
            + ' (' + apiFailed.map((l) => l.source_device + '-' + l.target_device).join(',') + ')');
        record('browser: the failed link is red and dashed, healthy links are not',
            g1.failedEdges.length > 0 && g1.failedEdges.every((e) =>
                e.dash && e.dash !== 'none' && e.stroke !== g1.activeStroke),
            JSON.stringify(g1.failedEdges[0] || {}) + ' active=' + g1.activeStroke);
        record('browser: node and label counts are unchanged by the failure',
            g1.nodeCount === topo.nodes.length && g1.labels === topo.nodes.length,
            'nodes=' + g1.nodeCount + ' labels=' + g1.labels);
        const navText = await ev(client, "document.getElementById('navStatusText').textContent");
        record('browser: nav status shows the API health percentage',
            navText.indexOf(String(status.stats.health_percentage)) >= 0,
            'nav="' + navText + '" apiHealth=' + status.stats.health_percentage);
        const badge = await ev(client, "document.getElementById('topologyLinkCount').textContent.trim()");
        const apiActive = links.filter((l) => l.status !== 'failed').length;
        record('browser: link badge matches the API active-link count',
            badge === apiActive + ' Links Active', 'ui="' + badge + '" api=' + apiActive);
        await shoot('state-link-failed');

        // There is no submit handler on #failureForm: the working path for a host
        // failure is #failureType=host plus the Simulate Failure control button,
        // which auto-picks an active host.
        await ev(client, `(() => {
            const sel = document.getElementById('failureType');
            sel.value = 'host'; sel.dispatchEvent(new Event('change', { bubbles: true }));
            document.getElementById('btnSimulateFailure').click();
            return true; })()`);
        const failedHost = await waitValue(
            "document.querySelectorAll('#topologyCanvas .topology-node.failed').length || null", 30000);
        const topo2 = await api('/api/topology');
        const g2 = await ev(client, GRAPH);
        const apiFailedNodes = topo2.nodes.filter((n) => n.status === 'failed');
        record('browser: the host failure form marks a node failed', !!failedHost,
            failedHost ? 'failed node(s): ' + g2.failedNodes.join(',') : 'no failed node');
        record('browser: failed nodes drawn equal the failed hosts the API reports',
            g2.failedNodes.length === apiFailedNodes.length,
            'ui=' + g2.failedNodes.length + ' api=' + apiFailedNodes.length);
        const cardFailed = await ev(client, "document.getElementById('cardFailedNodes').textContent");
        record('browser: the Failed Nodes card matches the API failed-host count',
            cardFailed === String(apiFailedNodes.length),
            'card=' + cardFailed + ' api=' + apiFailedNodes.length);
        await shoot('state-host-failed');

        await ev(client, "document.querySelector('[data-section=\"dashboard\"]').click(), true");
        await wait(500);
        await ev(client, "document.getElementById('btnRecoverNetwork').click(), true");
        const recToast = await waitValue(`(() => {
            const t = Array.from(document.querySelectorAll('.toast .toast-body')).map((e) => e.textContent).join(' || ');
            return /Recovery finished|rerouted|No alternative/i.test(t) ? t : null; })()`, 45000);
        record('browser: Recover Network click reports the outcome', !!recToast,
            recToast ? recToast.slice(0, 120) : 'no recovery toast');
        await wait(3000);
        // renderRecoveryStatus() writes div.alert.alert-success / .alert-warning,
        // not .recovery-item.
        const rows = await ev(client, "document.querySelectorAll('#recoveryList .alert').length");
        record('browser: Recovery Status lists the attempt', rows > 0,
            rows + ' rows in #recoveryList');
        const g3 = await ev(client, GRAPH);
        const links3 = await api('/api/links');
        const topo3 = await api('/api/topology');
        record('browser: after recovery the graph still matches the API exactly',
            g3.failedEdges.length === links3.filter((l) => l.status === 'failed').length
            && g3.failedNodes.length === topo3.nodes.filter((n) => n.status === 'failed').length,
            'uiEdges=' + g3.failedEdges.length
            + ' apiEdges=' + links3.filter((l) => l.status === 'failed').length
            + ' uiNodes=' + g3.failedNodes.length
            + ' apiNodes=' + topo3.nodes.filter((n) => n.status === 'failed').length);
        await shoot('state-after-recovery');

        // ---- 3. console and network
        const cerr = client.events.filter((e) => e.method === 'Runtime.consoleAPICalled'
            && e.params.type === 'error')
            .map((e) => e.params.args.map((a) => a.value || a.description || '').join(' '));
        const exc = client.events.filter((e) => e.method === 'Runtime.exceptionThrown')
            .map((e) => e.params.exceptionDetails.text);
        const lerr = client.events.filter((e) => e.method === 'Log.entryAdded'
            && e.params.entry.level === 'error')
            .map((e) => e.params.entry.text + ' ' + (e.params.entry.url || ''));
        const favicon = lerr.filter((t) => /favicon\.ico/.test(t));
        const realErrors = lerr.filter((t) => !/favicon\.ico/.test(t));
        record('browser: no JavaScript console errors', cerr.length === 0,
            cerr.length ? cerr.join(' | ').slice(0, 200) : '0 error-level messages');
        record('browser: no uncaught exceptions', exc.length === 0,
            exc.length ? exc.join(' | ').slice(0, 200) : 'none');
        record('browser: no failed network requests (favicon reported separately)',
            realErrors.length === 0,
            realErrors.length ? realErrors.join(' | ').slice(0, 200) : 'none');
        record('browser: /favicon.ico is served (no 404)', favicon.length === 0,
            favicon.length ? favicon[0].slice(0, 110)
                + '  -> cosmetic: no favicon is declared and Flask has no route for it'
                : 'no favicon request failed');
        const seen = new Map();
        client.events.filter((e) => e.method === 'Network.responseReceived'
            && e.params.response.url.includes('/api/')).forEach((e) => {
                seen.set(e.params.response.url.replace(BASE, ''), e.params.response.status); });
        const routes = ['/api/status', '/api/topology', '/api/links', '/api/failures', '/api/recoveries'];
        record('browser: all five polled API routes answered 200',
            routes.every((r) => seen.get(r) === 200),
            routes.map((r) => r + '=' + (seen.get(r) || 'not seen')).join(' '));
        record('browser: /api/logs answered 200 for Event History',
            seen.get('/api/logs') === 200, '/api/logs=' + (seen.get('/api/logs') || 'not seen'));
    } catch (err) {
        record('browser: harness completed without an internal error', false, err.message);
    } finally {
        if (client) client.close();
        chrome.kill();
        try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
    }
    fs.writeFileSync(OUT, JSON.stringify(results, null, 2));
})();
"""


def verify_browser(workdir):
    section("3. real browser (Chrome DevTools Protocol)")
    chrome = next((c for c in CHROME_CANDIDATES
                   if os.path.exists(c)), None)
    if not chrome:
        for name in ["browser: responsive layout at three viewports",
                     "browser: Simulate Failure / Recover Network real clicks",
                     "browser: node and link status correctness vs the API",
                     "browser: console errors and API responses"]:
            skipped(name, "Chrome not found in " + ", ".join(CHROME_CANDIDATES))
        return
    node = shutil.which("node")
    if not node:
        for name in ["browser: responsive layout at three viewports",
                     "browser: Simulate Failure / Recover Network real clicks",
                     "browser: node and link status correctness vs the API",
                     "browser: console errors and API responses"]:
            skipped(name, "node is not on PATH (the CDP driver is JavaScript)")
        return
    if port_in_use(9335):
        skipped("browser: real browser checks", "CDP port 9335 already in use")
        return

    js_path = os.path.join(workdir, "browser_check.js")
    out_path = os.path.join(workdir, "browser_results.json")
    shot_dir = os.path.join(workdir, "shots")
    with open(js_path, "w", encoding="utf-8") as handle:
        handle.write(BROWSER_JS)
    env = dict(os.environ)
    env.update({"SHN_VERIFY_BASE": BASE, "SHN_VERIFY_CHROME": chrome,
                "SHN_VERIFY_JS_OUT": out_path, "SHN_VERIFY_SHOT_DIR": shot_dir,
                "SHN_VERIFY_CDP_PORT": "9335"})

    print(f"chrome         : {chrome}", flush=True)
    print(f"node           : {node}", flush=True)
    print(f"screenshots    : {shot_dir}", flush=True)
    try:
        done = subprocess.run([node, js_path], env=env, timeout=420,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        skipped("browser: real browser checks", "the CDP driver timed out after 420s")
        return

    if not os.path.exists(out_path):
        skipped("browser: real browser checks",
                f"the driver produced no results (rc={done.returncode}) "
                f"{done.stderr.strip()[-200:]}")
        return
    with open(out_path, encoding="utf-8") as handle:
        for item in json.load(handle):
            record(item["status"], item["name"], item["detail"])


# ---------------------------------------------------------------------- driver

def main():
    print("=" * 72)
    print("FINAL VERIFICATION - Self-Healing Network")
    print("=" * 72)
    print(f"project        : {PROJECT}")
    print(f"production db  : {REAL_DB}   (opened read-only)")
    print(f"dev db         : {DEV_DB}")
    print(f"isolated port  : {PORT}  (dev server is {DEV_SERVER_PORT}, never used)")

    before = snapshot(REAL_DB)
    if before is None:
        check("real network.db exists", False, f"{REAL_DB} not found")
        return 1
    check("real network.db is readable read-only", True,
          ", ".join(f"{k}={v}" for k, v in before.items() if not k.startswith("__")))

    workdir = tempfile.mkdtemp(prefix="shn-final-verify-")
    db_path = os.path.join(workdir, "final_verify.db")
    print(f"temp dir       : {workdir}")
    print(f"temp database  : {db_path}")

    section("0. test isolation")
    check("the temporary database lives outside the project folder",
          os.path.commonpath([os.path.abspath(db_path), PROJECT]) != PROJECT,
          db_path)
    check("the temporary database is not the production network.db",
          os.path.normcase(db_path) != os.path.normcase(REAL_DB), db_path)
    check(f"the isolated server port is not the dev server port {DEV_SERVER_PORT}",
          PORT != DEV_SERVER_PORT, f"port={PORT}")

    server = None
    temp_db_existed = False
    try:
        if port_in_use(PORT):
            check(f"port {PORT} is free for the isolated server", False, "already in use")
            return 2
        env = fresh_env(**{ENV_VAR: db_path})
        env["SHN_TEST_PORT"] = str(PORT)
        env["SHN_TEST_BASE"] = BASE
        server = subprocess.Popen(
            [sys.executable, "-c",
             "import app; app.app.run(host='127.0.0.1', port=int(__import__('os').environ['SHN_TEST_PORT']),"
             " threaded=True, debug=False, use_reloader=False)"],
            cwd=PROJECT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        if not wait_for_server():
            check("the isolated test server started", False, f"no answer on {BASE}")
            return 1
        check("the isolated test server started", True, BASE)
        temp_db_existed = os.path.exists(db_path)
        check("the server created the temporary database file", temp_db_existed, db_path)

        # Read-only even here: this script never opens a database for writing.
        conn = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
        try:
            devices = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        finally:
            conn.close()
        check("the server wrote its rows to the temporary database", devices == 14,
              f"{db_path} devices={devices}")

        verify_api()
        verify_dev_database(workdir)
        verify_browser(workdir)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)

    section("4. the production database was not touched")
    after = snapshot(REAL_DB)
    diffs = diff_snapshots(before, after)
    check("every real network.db table has the same row count as before the run",
          not diffs, "; ".join(diffs) if diffs else
          ", ".join(f"{k}={v}" for k, v in after.items() if not k.startswith("__")))
    check("real network.db size is unchanged",
          before.get("__size__") == after.get("__size__"),
          f"{before.get('__size__')} bytes")
    check("the temporary database and working folder were deleted",
          not os.path.exists(workdir), workdir)

    passed = [r for r in RESULTS if r["status"] == "PASS"]
    failed = [r for r in RESULTS if r["status"] == "FAIL"]
    untested = [r for r in RESULTS if r["status"] == "NOT TESTED"]
    print()
    print("=" * 72)
    print(f"RESULT: {len(passed)} PASS, {len(failed)} FAIL, {len(untested)} NOT TESTED"
          f"  ({len(RESULTS)} checks)")
    for item in failed:
        print(f"  FAIL         {item['name']}  [{item['detail']}]")
    for item in untested:
        print(f"  NOT TESTED   {item['name']}  [{item['detail']}]")
    print()
    print("run it again with:")
    print("    cd /d C:\\SelfHealingNetwork && python final_verify.py")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())