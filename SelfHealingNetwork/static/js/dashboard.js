// Self-Healing Network Dashboard - dashboard controls
// Routes verified against app.py (no invented endpoints):
//   GET  /api/status
//   GET  /api/topology
//   GET  /api/links        (source of truth for a downed link: status='failed')
//   GET  /api/failures     (every unresolved event with its #id, plus the 50
//                           most recent resolved ones - the resolved entries are
//                           what stop a recovered link from re-alerting)
//   GET  /api/failures?unresolved=1  (every unresolved failure, no 50 row cap)
//   GET  /api/recoveries
//   GET  /api/logs        (Event History; newest 100 monitoring_logs rows)
//   POST /api/simulate/link_failure  { source, target }   -> { success, source, target }
//   POST /api/simulate/host_failure  { host }             -> { success, host }
//   POST /api/monitoring/start       { interval, demo_mode } -> { success, interval, demo_mode }
//   POST /api/monitoring/stop                             -> { success }
//   POST /api/recovery/trigger       { failure_id, source, target, failure_type }
//                                          -> { status: success|failed|busy, message, path }

(function () {
    'use strict';

    var POLL_INTERVAL_MS = 3000;
    // Backend health at or above this is still called healthy, but the exact
    // percentage is always displayed next to it.
    var HEALTHY_THRESHOLD = 90;

    var API = {
        status: '/api/status',
        topology: '/api/topology',
        links: '/api/links',
        failures: '/api/failures',
        unresolvedFailures: '/api/failures?unresolved=1',
        recoveries: '/api/recoveries',
        logs: '/api/logs',
        simulateLinkFailure: '/api/simulate/link_failure',
        simulateHostFailure: '/api/simulate/host_failure',
        monitoringStart: '/api/monitoring/start',
        monitoringStop: '/api/monitoring/stop',
        connectivity: '/api/test/connectivity',
        recoveryTrigger: '/api/recovery/trigger'
    };

    // Only the six most recent "Find paths" answers stay on screen.
    var PATH_CHECK_LIMIT = 6;

    function byId(id) {
        return document.getElementById(id);
    }

    // ------------------------------------------------------------- errors

    function ApiError(message, status, payload) {
        this.name = 'ApiError';
        this.message = message;
        this.status = status;
        this.payload = payload;
        this.stack = new Error(message).stack;
    }
    ApiError.prototype = Object.create(Error.prototype);
    ApiError.prototype.constructor = ApiError;

    function stripHtml(value) {
        return String(value).replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    }

    function htmlErrorTitle(rawText) {
        var match = /<title>([\s\S]*?)<\/title>/i.exec(rawText || '');
        if (!match) {
            return '';
        }
        return stripHtml(match[1]).replace(/\s*\/\/\s*Werkzeug Debugger\s*$/i, '').trim();
    }

    // Prefers the exact text the server sent back (error / message field,
    // otherwise the response body or the error page title), never a made-up
    // string.
    function serverMessage(payload, rawText, fallback) {
        if (payload && typeof payload === 'object') {
            var direct = payload.error || payload.message || payload.detail;
            if (typeof direct === 'string' && direct.trim()) {
                return direct.trim();
            }
        }
        if (typeof payload === 'string' && payload.trim()) {
            return stripHtml(payload).slice(0, 300);
        }
        var title = htmlErrorTitle(rawText);
        if (title) {
            return title.slice(0, 300);
        }
        var raw = stripHtml(rawText || '');
        if (raw) {
            return raw.slice(0, 300);
        }
        return fallback;
    }

    async function readJson(url, response) {
        var rawText = '';
        try {
            rawText = await response.text();
        } catch (err) {
            rawText = '';
        }

        var payload = null;
        if (rawText) {
            try {
                payload = JSON.parse(rawText);
            } catch (err) {
                payload = null;
            }
        }

        if (!response.ok) {
            throw new ApiError(
                serverMessage(payload, rawText, url + ' failed with HTTP ' + response.status),
                response.status,
                payload
            );
        }

        // A 200 with a body we cannot parse is an error in its own right; keep
        // the server's text instead of silently reporting a generic message.
        if (rawText && payload === null) {
            throw new ApiError(
                'Invalid JSON response from ' + url + ': ' + serverMessage(null, rawText, ''),
                response.status,
                null
            );
        }

        return payload;
    }

    async function getJson(url) {
        var response;
        try {
            response = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' });
        } catch (err) {
            throw new ApiError('Cannot reach the server (' + url + '): ' + err.message, 0, null);
        }
        return readJson(url, response);
    }

    async function postJson(url, body) {
        var response;
        try {
            response = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify(body || {})
            });
        } catch (err) {
            throw new ApiError('Cannot reach the server (' + url + '): ' + err.message, 0, null);
        }
        return readJson(url, response);
    }

    // --------------------------------------------------- toast notifications

    // Single notification system: one container, Bootstrap toasts.
    var TOAST_HOST_ID = 'dashboardToasts';

    function toastHost() {
        var host = byId(TOAST_HOST_ID);
        if (!host) {
            host = document.createElement('div');
            host.id = TOAST_HOST_ID;
            host.className = 'toast-container position-fixed top-0 end-0 p-3';
            host.style.zIndex = '1090';
            document.body.appendChild(host);
        }
        return host;
    }

    function showToast(kind, message, delay) {
        var variants = {
            success: 'text-bg-success',
            error: 'text-bg-danger',
            info: 'text-bg-primary'
        };
        var lifetime = delay || (kind === 'error' ? 8000 : 5000);
        var variant = variants[kind] || variants.info;

        var toast = document.createElement('div');
        toast.className = 'toast align-items-center border-0 ' + variant;
        toast.setAttribute('role', 'alert');
        toast.setAttribute('aria-live', kind === 'error' ? 'assertive' : 'polite');
        toast.setAttribute('aria-atomic', 'true');

        var wrapper = document.createElement('div');
        wrapper.className = 'd-flex';

        var body = document.createElement('div');
        body.className = 'toast-body';
        body.textContent = message;
        wrapper.appendChild(body);

        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close me-2 m-auto' + (kind === 'info' ? '' : ' btn-close-white');
        close.setAttribute('data-bs-dismiss', 'toast');
        close.setAttribute('aria-label', 'Close');
        wrapper.appendChild(close);

        toast.appendChild(wrapper);
        toastHost().appendChild(toast);

        function dispose() {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }

        if (window.bootstrap && window.bootstrap.Toast) {
            toast.addEventListener('hidden.bs.toast', dispose);
            window.bootstrap.Toast.getOrCreateInstance(toast, { delay: lifetime }).show();
        } else {
            toast.classList.add('show');
            window.setTimeout(dispose, lifetime);
        }
    }

    // ------------------------------------------------------------ elements

    var ui = {};

    function cacheElements() {
        ui = {
            cardTotalHosts: byId('cardTotalHosts'),
            cardTotalSwitches: byId('cardTotalSwitches'),
            cardActiveNodes: byId('cardActiveNodes'),
            cardFailedNodes: byId('cardFailedNodes'),
            navStatusText: byId('navStatusText'),
            navStatusDot: byId('navStatusDot'),
            lastUpdate: byId('lastUpdate'),
            sidebarStatus: byId('sidebarStatus'),
            sidebarStatusText: byId('sidebarStatusText'),
            topologyLinkCount: byId('topologyLinkCount'),
            topologyCanvas: byId('topologyCanvas'),
            detailedTopologyCanvas: byId('detailedTopologyCanvas'),
            topologyPlaceholder: byId('topologyPlaceholder'),
            healthChart: byId('healthChart'),
            healthChartEmpty: byId('healthChartEmpty'),
            alertCount: byId('alertCount'),
            alertsList: byId('alertsList'),
            recoveryCount: byId('recoveryCount'),
            recoveryList: byId('recoveryList'),
            eventsBody: byId('eventsBody'),
            btnRefreshLogs: byId('btnRefreshLogs'),
            btnSimulateFailure: byId('btnSimulateFailure'),
            btnStartMonitoring: byId('btnStartMonitoring'),
            btnStopMonitoring: byId('btnStopMonitoring'),
            btnRecoverNetwork: byId('btnRecoverNetwork'),
            failureType: byId('failureType'),
            failureSource: byId('failureSource'),
            failureTarget: byId('failureTarget'),
            failureHost: byId('failureHost'),
            failureForm: byId('failureForm'),
            linkFailureFields: byId('linkFailureFields'),
            linkFailureTargetFields: byId('linkFailureTargetFields'),
            hostFailureFields: byId('hostFailureFields'),
            checkInterval: byId('checkInterval'),
            demoMode: byId('demoMode'),
            activeFailuresList: byId('activeFailuresList'),
            alternativePathsList: byId('alternativePathsList')
        };

        ui.failureSubmitButton = ui.failureForm
            ? ui.failureForm.querySelector('button[type="submit"]')
            : null;
    }

    // ------------------------------------------------------------ rendering

    function setText(node, value) {
        if (node) {
            node.textContent = value;
        }
    }

    function renderStats(stats) {
        if (!stats) {
            return;
        }
        setText(ui.cardTotalHosts, stats.total_hosts);
        setText(ui.cardTotalSwitches, stats.total_switches);
        setText(ui.cardActiveNodes, (stats.active_hosts || 0) + (stats.total_switches || 0));
        setText(ui.cardFailedNodes, stats.failed_hosts);

        if (ui.topologyLinkCount) {
            setText(ui.topologyLinkCount, stats.active_links + ' Links Active');
        }

        // The word follows the health threshold, but the number is always shown:
        // a rerouted link leaves the backend health below 100 while the network
        // is still above the threshold, and hiding that hides a real failure.
        var health = Number(stats.health_percentage);
        var healthy = isFinite(health) && health >= HEALTHY_THRESHOLD;
        setText(ui.navStatusText, (healthy ? 'Network Healthy' : 'Network Degraded')
            + ' (' + (isFinite(health) ? health : '--') + '%)');
        if (ui.navStatusDot) {
            ui.navStatusDot.style.backgroundColor = healthy ? '#28a745' : '#dc3545';
        }
    }

    function renderMonitoring(monitoring) {
        if (!monitoring) {
            return;
        }
        setMonitoringState(!!monitoring.running);
        setText(ui.sidebarStatusText, monitoring.running ? 'Monitoring Active' : 'Monitoring Stopped');
        if (ui.sidebarStatus) {
            ui.sidebarStatus.classList.toggle('bg-success', !!monitoring.running);
            ui.sidebarStatus.classList.toggle('bg-secondary', !monitoring.running);
        }
    }

    function setMonitoringState(running) {
        state.monitoringRunning = !!running;
        if (ui.btnStartMonitoring) {
            ui.btnStartMonitoring.disabled = state.monitoringRunning;
        }
        if (ui.btnStopMonitoring) {
            ui.btnStopMonitoring.disabled = !state.monitoringRunning;
        }
    }

    function linkKey(source, target) {
        return 'link:' + source + '-' + target;
    }

    // Every timestamp the backend returns is UTC. Values written by current code
    // carry an explicit "+00:00" offset; values written by older versions are
    // untagged, so they are parsed as UTC here instead of being silently read in
    // the browser's own zone. Returns epoch milliseconds, or null when the value
    // is not a timestamp this app wrote.
    var TIMESTAMP_PATTERN = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?/;

    function parseUtcTimestamp(value) {
        if (!value || typeof value !== 'string') {
            return null;
        }
        var match = TIMESTAMP_PATTERN.exec(value);
        if (!match) {
            return null;
        }
        var millis = match[7] ? Number((match[7] + '000').slice(0, 3)) : 0;
        return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]),
            Number(match[4]), Number(match[5]), Number(match[6]), millis);
    }

    // The database stores "YYYY-MM-DD HH:MM:SS" while /api/status returns ISO
    // 8601 ("YYYY-MM-DDTHH:MM:SS.mmm+00:00"). Both are UTC, so both are read
    // the same way: the SQL form explicitly, the ISO form through Date.parse.
    function parseTimestamp(value) {
        var millis = parseUtcTimestamp(value);
        if (millis !== null) {
            return millis;
        }
        if (typeof value === 'string' && value.trim()) {
            var parsed = Date.parse(value.trim());
            return isNaN(parsed) ? null : parsed;
        }
        return null;
    }

    function pad2(number) {
        return (number < 10 ? '0' : '') + number;
    }

    function formatClock(millis) {
        var date = new Date(millis);
        return pad2(date.getHours()) + ':' + pad2(date.getMinutes()) + ':' + pad2(date.getSeconds());
    }

    // Timestamps are stored in UTC; only what is displayed is converted to the
    // viewer's local time.
    function formatTimestamp(value) {
        var millis = parseUtcTimestamp(value);
        if (millis === null) {
            return value || '';
        }
        var date = new Date(millis);
        return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate())
            + ' ' + formatClock(millis);
    }

    // A resolved event hides the link/topology fallback for its pair, but only
    // while nothing has failed that pair since. A link touched after the
    // resolution is a new failure and must be shown again instead of being
    // hidden forever by the stale resolved entry. Stored timestamps carry
    // microseconds, so a failure and a resolution inside the same second are
    // still ordered correctly.
    // resolvedMs is already parsed (see noteResolution); itemTime is still the
    // raw value from the API, and a missing one is treated as "unknown age".
    function suppressedBy(resolvedMs, itemTime) {
        if (resolvedMs === null || resolvedMs === undefined) {
            return false;
        }
        var item = parseUtcTimestamp(itemTime);
        return item === null || item <= resolvedMs;
    }

    // Failure Alerts: unresolved failure events from /api/failures merged with
    // the links and nodes the backend already marks as failed.
    //   - /api/links reports status='failed' for a downed link, while
    //     /api/topology removes that edge from the graph entirely, so the
    //     topology alone cannot show it.
    //   - a failure that predates the current failure_detector behaviour may have
    //     no failure_events row at all, so the two sources are merged.
    //   - recovery reroutes traffic over an alternative path and resolves the
    //     failure event, but never restores links.status, so a pair that only has
    //     a *resolved* event is treated as recovered and not re-alerted - unless
    //     the link was failed again after that resolution.
    function buildFailureEntries(failures, topology, links) {
        var entries = [];
        var seen = {};
        var recovered = {};

        function addEntry(entry, extraKeys) {
            if (seen[entry.key]) {
                return;
            }
            seen[entry.key] = true;
            (extraKeys || []).forEach(function (extraKey) {
                seen[extraKey] = true;
            });
            entries.push(entry);
        }

        function noteResolution(key, resolvedAt) {
            if (!key || resolvedAt === null) {
                return;
            }
            var current = recovered[key];
            if (current === undefined || resolvedAt > current) {
                recovered[key] = resolvedAt;
            }
        }

        (failures || []).forEach(function (failure) {
            var isHost = failure.failure_type === 'host';
            var source = isHost ? failure.device_name : failure.link_source;
            var target = isHost ? failure.device_name : failure.link_target;

            if (failure.resolved) {
                if (!source) {
                    return;
                }
                // resolved_at is the moment the failure was cleared; fall back to
                // the event timestamp for rows that never got one.
                var resolvedAt = parseUtcTimestamp(failure.resolved_at || failure.timestamp);
                if (isHost) {
                    noteResolution('host:' + source, resolvedAt);
                } else {
                    noteResolution(linkKey(source, target), resolvedAt);
                    noteResolution(linkKey(target, source), resolvedAt);
                }
                return;
            }
            addEntry({
                key: isHost ? 'host:' + source : linkKey(source, target),
                id: failure.id,
                type: failure.failure_type,
                message: failure.description,
                detail: isHost ? source : source + ' <-> ' + target,
                timestamp: failure.timestamp,
                source: source,
                target: isHost ? null : target
            }, isHost ? [] : [linkKey(target, source)]);
        });

        var pairs = {};
        var hostTouched = {};
        (links || []).forEach(function (link) {
            if (link.status === 'active' || !link.source_device || !link.target_device) {
                return;
            }

            // Newest time any failed link of a host was recorded, used to tell a
            // re-failed host from one that was merely never restored. Topology
            // nodes carry no timestamp of their own, so nothing is invented for
            // them: the value comes from the host's own link rows.
            [link.source_device, link.target_device].forEach(function (device) {
                var seen_at = parseUtcTimestamp(link.updated_at);
                if (seen_at !== null
                    && (hostTouched[device] === undefined || seen_at > hostTouched[device])) {
                    hostTouched[device] = seen_at;
                }
            });

            var forward = recovered[linkKey(link.source_device, link.target_device)];
            var reverse = recovered[linkKey(link.target_device, link.source_device)];
            var resolvedMs = forward === undefined ? reverse : forward;
            if (suppressedBy(resolvedMs, link.updated_at)) {
                return;
            }
            var pair = link.source_device + ' <-> ' + link.target_device;
            if (pairs[pair]) {
                return;
            }
            pairs[pair] = true;
            addEntry({
                key: linkKey(link.source_device, link.target_device),
                id: null,
                type: 'link',
                message: 'Link ' + pair + ' failed',
                detail: pair,
                timestamp: link.updated_at || null,
                source: link.source_device,
                target: link.target_device
            }, [linkKey(link.target_device, link.source_device)]);
        });

        var topologyData = topology || {};
        (topologyData.nodes || []).forEach(function (node) {
            if (node.status !== 'failed' || suppressedBy(recovered['host:' + node.id], hostTouched[node.id])) {
                return;
            }
            addEntry({
                key: 'host:' + node.id,
                id: null,
                type: 'host',
                message: 'Host ' + node.id + ' failed',
                detail: node.ip || node.id,
                timestamp: null,
                source: node.id,
                target: null
            });
        });

        return entries;
    }

    function element(tag, className, text) {
        var node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined && text !== null) {
            node.textContent = text;
        }
        return node;
    }

    function renderFailureAlerts(entries) {
        var host = ui.alertsList;
        if (!host) {
            return;
        }

        setText(ui.alertCount, entries.length + ' Active');
        host.textContent = '';

        if (entries.length === 0) {
            host.appendChild(element('div', 'text-center text-muted py-4', 'No active failures detected'));
            return;
        }

        entries.forEach(function (entry) {
            var alert = element('div', 'alert alert-danger d-flex justify-content-between align-items-start');
            var content = element('div');

            var header = element('div', 'fw-semibold', entry.type + ' failure' + (entry.id ? ' #' + entry.id : ''));
            var message = element('div', 'small', entry.message || '');
            var detail = entry.detail ? element('div', 'small text-muted', entry.detail) : null;
            var stamp = element('div', 'small text-muted', entry.timestamp ? formatTimestamp(entry.timestamp) : 'no failure event recorded yet');

            content.appendChild(header);
            content.appendChild(message);
            if (detail) {
                content.appendChild(detail);
            }
            content.appendChild(stamp);

            alert.appendChild(content);
            alert.appendChild(element('span', 'badge bg-danger', entry.type));
            host.appendChild(alert);
        });
    }

    // Recovery Status: results returned by /api/recovery/trigger for this run
    // first, then the server side history from /api/recoveries.
    function renderRecoveryStatus(dbEvents, attempts) {
        var host = ui.recoveryList;
        if (!host) {
            return;
        }

        var events = dbEvents || [];
        var runAttempts = attempts || [];
        setText(ui.recoveryCount, events.length + ' Recoveries');

        var represented = {};
        var rows = [];

        runAttempts.forEach(function (attempt) {
            var status = attempt.status || 'unknown';
            if (attempt.failureId) {
                represented[attempt.failureId + '|' + status] = true;
            }
            rows.push({
                status: status,
                message: attempt.message,
                path: attempt.path,
                note: recoveryOutcomeNote(attempt.failureId, status),
                label: attempt.failureId ? 'Failure #' + attempt.failureId : 'Failure',
                timestamp: null
            });
        });

        events.forEach(function (event) {
            var normalized = event.recovery_status === 'success' ? 'success' : 'failed';
            var key = event.failure_event_id + '|' + normalized;
            if (represented[key]) {
                return;
            }
            rows.push({
                status: normalized,
                message: event.description,
                path: event.alternative_path,
                note: recoveryOutcomeNote(event.failure_event_id, normalized),
                label: event.failure_event_id ? 'Failure #' + event.failure_event_id : 'Failure',
                timestamp: event.timestamp
            });
        });

        host.textContent = '';

        if (rows.length === 0) {
            host.appendChild(element('div', 'text-center text-muted py-4', 'No recovery events'));
            return;
        }

        rows.forEach(function (row) {
            var success = row.status === 'success';
            var alert = element('div', 'alert alert-' + (success ? 'success' : 'warning') + ' d-flex justify-content-between align-items-start');

            var content = element('div');
            content.appendChild(element('div', 'fw-semibold', row.label + ' - ' + row.status));
            if (row.message) {
                content.appendChild(element('div', 'small', row.message));
            }
            if (row.path && row.path.length) {
                var pathText = Array.isArray(row.path) ? row.path.join(' -> ') : row.path;
                content.appendChild(element('div', 'small text-muted', 'path: ' + pathText));
            }
            if (row.note) {
                content.appendChild(element('div', 'small text-muted', row.note));
            }
            content.appendChild(element('div', 'small text-muted', row.timestamp ? formatTimestamp(row.timestamp) : 'this run'));

            alert.appendChild(content);
            alert.appendChild(element('span', 'badge ' + (success ? 'bg-success' : 'bg-danger'), row.status));
            host.appendChild(alert);
        });
    }

    // --------------------------------------- failure detection panels
    // #activeFailuresList and #alternativePathsList sit in the Failure Detection
    // section. Both are filled from data the poll already fetches, so they cost
    // no extra request per cycle: the active failures reuse the merged entries
    // behind Failure Alerts, and the paths reuse the recovery history plus this
    // run's attempts. Paths for one specific pair are only requested when the
    // operator asks, through POST /api/test/connectivity.

    function panelMessage(host, className, message) {
        if (!host) {
            return;
        }
        host.textContent = '';
        host.appendChild(element('div', 'text-center text-muted py-4'
            + (className ? ' ' + className : ''), message));
    }

    function renderActiveFailures() {
        var host = ui.activeFailuresList;
        if (!host) {
            return;
        }

        var panels = state.failurePanels;
        if (panels.failuresError) {
            panelMessage(host, 'text-danger',
                'Could not load active failures: ' + panels.failuresError);
            return;
        }

        var entries = panels.entries;
        if (!entries || entries.length === 0) {
            panelMessage(host, '', 'No active failures detected');
            return;
        }

        host.textContent = '';
        entries.forEach(function (entry) {
            var row = element('div', 'active-failure');
            var content = element('div', 'flex-grow-1');
            content.appendChild(element('div', 'fw-semibold',
                entry.type + ' failure' + (entry.id ? ' #' + entry.id : '')));
            content.appendChild(element('div', 'small', entry.message || ''));
            if (entry.detail) {
                content.appendChild(element('div', 'small text-muted', entry.detail));
            }
            content.appendChild(element('div', 'small text-muted',
                entry.timestamp ? formatTimestamp(entry.timestamp)
                    : 'no failure event recorded yet'));
            row.appendChild(content);

            var side = element('div', 'd-flex flex-column align-items-end gap-2');
            if (entry.source && entry.target) {
                var check = element('button', 'btn btn-sm btn-outline-secondary');
                check.type = 'button';
                check.setAttribute('data-path-check', '1');
                check.setAttribute('data-source', entry.source);
                check.setAttribute('data-target', entry.target);
                check.appendChild(element('span', 'loading-spinner d-none'));
                check.appendChild(element('span', '', 'Find paths'));
                side.appendChild(check);
            }
            side.appendChild(element('span', 'badge '
                + (entry.type === 'host' ? 'bg-danger' : 'bg-warning'), entry.type));
            row.appendChild(side);

            host.appendChild(row);
        });
    }

    function pathCheckBadge(check) {
        if (check.status === 'loading') {
            return element('span', 'badge bg-info', 'checking...');
        }
        if (check.status === 'connected') {
            return element('span', 'badge bg-info', 'connected');
        }
        if (check.status === 'error') {
            return element('span', 'badge bg-danger', 'error');
        }
        if (check.status === 'empty') {
            return element('span', 'badge bg-secondary', 'no path');
        }
        return element('span', 'badge bg-success',
            check.paths.length + (check.paths.length === 1 ? ' path' : ' paths'));
    }

    function renderPathCard(label, pathText, badge, metaParts) {
        var card = element('div', 'path-item');
        var header = element('div', 'path-header');
        header.appendChild(element('strong', '', label));
        header.appendChild(badge);
        card.appendChild(header);
        card.appendChild(element('div', 'path-nodes', pathText));
        if (metaParts && metaParts.length) {
            var meta = element('div', 'path-meta');
            metaParts.forEach(function (part) {
                meta.appendChild(element('span', '', part));
            });
            card.appendChild(meta);
        }
        return card;
    }

    function renderAlternativePaths() {
        var host = ui.alternativePathsList;
        if (!host) {
            return;
        }

        var panels = state.failurePanels;
        if (panels.failuresError || panels.recoveriesError) {
            panelMessage(host, 'text-danger', 'Could not load alternative paths: '
                + (panels.failuresError || panels.recoveriesError));
            return;
        }

        var checks = state.pathChecks;
        var keys = Object.keys(checks);
        var recoveries = panels.recoveries || [];
        var attempts = state.lastAttempts || [];
        var usedRecoveries = recoveries.filter(function (event) {
            return !!event.alternative_path;
        });
        var usedAttempts = attempts.filter(function (attempt) {
            return !!(attempt.path && attempt.path.length);
        });

        if (keys.length === 0 && usedRecoveries.length === 0
            && usedAttempts.length === 0) {
            panelMessage(host, '', 'No alternative paths yet. Use "Find paths" on an '
                + 'active failure to ask the topology for a route.');
            return;
        }

        host.textContent = '';

        // Paths requested during this session, newest first.
        keys.slice().reverse().forEach(function (key) {
            var check = checks[key];
            var label = check.source + ' <-> ' + check.target;
            if (check.status === 'error') {
                host.appendChild(renderPathCard(label, check.message || 'Request failed',
                    pathCheckBadge(check), ['connectivity check']));
                return;
            }
            if (check.status === 'loading') {
                host.appendChild(renderPathCard(label, 'Asking the topology for routes...',
                    pathCheckBadge(check), ['connectivity check']));
                return;
            }
            if (check.status === 'connected') {
                host.appendChild(renderPathCard(label,
                    (check.directPath && check.directPath.length
                        ? check.directPath.join(' -> ') : 'The two devices are still reachable'),
                    element('span', 'badge bg-info', 'connected'),
                    ['the direct link is still up']));
                return;
            }
            if (!check.paths.length) {
                host.appendChild(renderPathCard(label,
                    'No alternative path is available for this pair.',
                    pathCheckBadge(check), ['connectivity check']));
                return;
            }
            check.paths.forEach(function (path) {
                host.appendChild(renderPathCard(label, path.join(' -> '),
                    pathCheckBadge(check), ['connectivity check']));
            });
        });

        // Paths the backend already used, from the polled recovery history.
        usedRecoveries.forEach(function (event) {
            var label = 'Failure ' + (event.failure_event_id === null
                || event.failure_event_id === undefined ? '' : '#' + event.failure_event_id);
            host.appendChild(renderPathCard(label, event.alternative_path,
                element('span', 'badge ' + (event.recovery_status === 'success'
                    ? 'bg-success' : 'bg-warning'), event.recovery_status || 'unknown'),
                event.timestamp ? [formatTimestamp(event.timestamp)] : []));
        });

        // Paths used by this run's Recover Network, before the poll returns.
        usedAttempts.forEach(function (attempt) {
            var label = attempt.failureId ? 'Failure #' + attempt.failureId : 'This run';
            host.appendChild(renderPathCard(label,
                Array.isArray(attempt.path) ? attempt.path.join(' -> ') : attempt.path,
                element('span', 'badge ' + (attempt.status === 'success'
                    ? 'bg-success' : 'bg-warning'), attempt.status || 'unknown'),
                ['this run']));
        });
    }

    // Only requested on demand, never as part of the poll. The caller owns the
    // busy state of the button that triggered it.
    async function checkAlternativePaths(source, target) {
        var key = pairKey(source, target);
        state.pathChecks[key] = { source: source, target: target, status: 'loading', paths: [] };
        renderAlternativePaths();

        try {
            var result = await postJson(API.connectivity, { source: source, target: target });
            var alternatives = (result && result.alternatives) || [];
            var direct = (result && result.path) || [];
            // The endpoint only fills `alternatives` when the pair is NOT
            // connected. A pair that is still reachable (for example because the
            // mesh carries more than one link between the same two switches)
            // reports connected=true with its direct path and no alternatives,
            // which must not be reported as "no route exists".
            var connected = !!(result && result.connected);
            state.pathChecks[key] = {
                source: source,
                target: target,
                status: connected ? 'connected' : (alternatives.length ? 'ready' : 'empty'),
                paths: alternatives,
                directPath: direct,
                message: null
            };
        } catch (err) {
            state.pathChecks[key] = {
                source: source,
                target: target,
                status: 'error',
                paths: [],
                message: err && err.message ? err.message : String(err)
            };
        }

        // Keep the panel bounded over a long session.
        var keys = Object.keys(state.pathChecks);
        if (keys.length > PATH_CHECK_LIMIT) {
            keys.slice(0, keys.length - PATH_CHECK_LIMIT).forEach(function (stale) {
                delete state.pathChecks[stale];
            });
        }

        renderAlternativePaths();
    }

    // ------------------------------------------------------- health trend
    // The trend is built from stats.health_percentage in /api/status, which the
    // poll already fetches, so the chart costs no extra request.
    // /api/stats/history is deliberately not used: nothing in this app writes
    // network_stats rows (only POST /api/stats does, and the dashboard has no
    // reason to POST it), so that endpoint returns an empty list and would add
    // a sixth request to every cycle.
    // Samples are kept for the last 40 polls (~2 minutes at the 3s cadence);
    // one chart instance is created and then updated in place.

    var HEALTH_SAMPLE_LIMIT = 40;

    function chartHealthyColor() {
        return cssColor('--accent-green', '#28a745');
    }

    function chartDegradedColor() {
        return cssColor('--accent-red', '#dc3545');
    }

    function cssColor(name, fallback) {
        try {
            if (window.getComputedStyle) {
                var value = window.getComputedStyle(document.documentElement)
                    .getPropertyValue(name);
                if (value && value.trim()) {
                    return value.trim();
                }
            }
        } catch (err) {
            // getComputedStyle is unavailable or throws in some environments;
            // the fallback colour keeps the chart readable.
        }
        return fallback;
    }

    function recordHealthSample(status) {
        var stats = status && status.stats;
        if (!stats) {
            return;
        }
        var health = Number(stats.health_percentage);
        if (!isFinite(health)) {
            return;
        }

        var stamp = parseTimestamp(status.timestamp);
        var at = stamp === null ? Date.now() : stamp;
        var label = formatClock(at);
        var samples = state.healthSamples;
        var last = samples[samples.length - 1];

        // /api/status is also read after every action, so several samples can
        // land in the same second; one point per second is enough for a trend.
        if (last && Math.floor(last.at / 1000) === Math.floor(at / 1000)) {
            return;
        }

        samples.push({
            at: at,
            label: label,
            health: health,
            failedLinks: Number(stats.failed_links) || 0
        });

        while (samples.length > HEALTH_SAMPLE_LIMIT) {
            samples.shift();
        }
    }

    function renderHealthTrend() {
        var canvas = ui.healthChart;
        var empty = ui.healthChartEmpty;
        var samples = state.healthSamples;

        if (empty) {
            empty.classList.toggle('d-none', samples.length > 0);
        }
        if (!canvas || samples.length === 0) {
            return;
        }
        if (!window.Chart) {
            // Chart.js is loaded from a CDN; without it the rest of the
            // dashboard still works, and the placeholder keeps saying why the
            // card is empty instead of showing a blank box.
            if (empty) {
                empty.classList.remove('d-none');
                empty.textContent = 'Chart library unavailable';
            }
            return;
        }

        var labels = samples.map(function (sample) {
            return sample.label;
        });
        var values = samples.map(function (sample) {
            return sample.health;
        });
        var failed = samples[samples.length - 1].health < HEALTHY_THRESHOLD;

        if (!state.healthChartInstance) {
            state.healthChartInstance = new window.Chart(canvas.getContext('2d'), {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Network health %',
                        data: values,
                        borderColor: failed ? chartDegradedColor() : chartHealthyColor(),
                        backgroundColor: 'rgba(40, 167, 69, 0.12)',
                        borderWidth: 2,
                        pointRadius: 2,
                        pointHoverRadius: 4,
                        tension: 0.3,
                        fill: true
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function (context) {
                                    return context.parsed.y + '% healthy';
                                }
                            }
                        }
                    },
                    scales: {
                        y: {
                            min: 0,
                            // suggestedMax rather than max: Chart.js ignores
                            // `grace` when min/max are both pinned, and a line
                            // sitting exactly on the top border of the plot area
                            // reads as clipped. The extra headroom is above the
                            // real maximum and is left unlabelled.
                            suggestedMax: 108,
                            ticks: {
                                stepSize: 25,
                                color: cssColor('--text-muted', '#8a94a6'),
                                callback: function (value) {
                                    return value > 100 ? '' : value + '%';
                                }
                            },
                            grid: { color: cssColor('--border-color', 'rgba(0,0,0,0.1)') }
                        },
                        x: {
                            ticks: {
                                maxTicksLimit: 6,
                                autoSkip: true,
                                maxRotation: 0,
                                color: cssColor('--text-muted', '#8a94a6')
                            },
                            grid: { display: false }
                        }
                    }
                }
            });
            return;
        }

        var chart = state.healthChartInstance;
        chart.data.labels = labels;
        chart.data.datasets[0].data = values;
        chart.data.datasets[0].borderColor = failed
            ? chartDegradedColor()
            : chartHealthyColor();
        chart.update('none');
    }

    // ------------------------------------------------------- event history
    // The template ships a tbody that says "Loading events..." and a Refresh
    // button, but nothing ever filled either of them: no code referenced
    // #eventsBody or #btnRefreshLogs, so the section stayed on its placeholder
    // forever and the button did nothing.
    // /api/logs returns the newest monitoring_logs rows as
    // {id, event_type, device_name, message, timestamp}, already newest first.
    // It is fetched on page load and on Refresh, never inside the five-request
    // poll cycle, so the polling budget is unchanged.

    function eventRowCells(event) {
        var type = event.event_type || 'event';
        var device = event.device_name;
        return [
            event.timestamp ? formatTimestamp(event.timestamp) : 'unknown time',
            type,
            device ? device : '-',
            event.message || ''
        ];
    }

    // errorMessage is only set when the request failed; either way the tbody is
    // rewritten, so the "Loading events..." placeholder is always cleared.
    function renderEvents(events, errorMessage) {
        var body = ui.eventsBody;
        if (!body) {
            return;
        }

        body.textContent = '';

        function placeholderRow(text, className) {
            var row = element('tr');
            var cell = element('td', 'text-center py-4 ' + (className || 'text-muted'), text);
            cell.setAttribute('colspan', '4');
            row.appendChild(cell);
            body.appendChild(row);
        }

        if (errorMessage) {
            placeholderRow('Could not load events: ' + errorMessage, 'text-danger');
            return;
        }

        var rows = events || [];
        if (rows.length === 0) {
            placeholderRow('No events found');
            return;
        }

        rows.forEach(function (event) {
            var row = element('tr');
            var time = eventRowCells(event);
            row.appendChild(element('td', 'text-nowrap', time[0]));

            // event_type is a machine value (link_failure); show it readably but
            // keep the raw value on the element so it stays greppable.
            var typeCell = element('td');
            var badge = element('span', 'badge ' + eventBadgeClass(time[1]), time[1].replace(/_/g, ' '));
            typeCell.appendChild(badge);
            row.appendChild(typeCell);

            row.appendChild(element('td', 'text-nowrap', time[2]));
            row.appendChild(element('td', '', time[3]));
            body.appendChild(row);
        });
    }

    function eventBadgeClass(eventType) {
        if (eventType.indexOf('failure') !== -1 || eventType.indexOf('failed') !== -1) {
            return 'bg-danger';
        }
        if (eventType.indexOf('recovery') !== -1 || eventType.indexOf('restored') !== -1) {
            return 'bg-success';
        }
        if (eventType.indexOf('monitoring') !== -1) {
            return 'bg-primary';
        }
        return 'bg-secondary';
    }

    async function loadEvents() {
        try {
            var events = await getJson(API.logs);
            if (!Array.isArray(events)) {
                renderEvents([], 'the server returned ' + typeof events
                    + ' instead of a list of events');
                return;
            }
            renderEvents(events, null);
        } catch (err) {
            // Reported in the table itself, not only as a toast, so the section
            // never silently keeps the "Loading events..." placeholder.
            renderEvents([], err && err.message ? err.message : String(err));
        } finally {
            setSkeletonState(false);
        }
    }

    // ------------------------------------------------------- loading skeleton
    // The placeholders these hosts ship with are dimmed and swept by a shimmer
    // until the first payload lands. The 3s poll must not flip them back, so the
    // state is cleared once and never set again.
    var SKELETON_HOST_IDS = [
        'alertsList',
        'recoveryList',
        'eventsBody',
        'hostsList',
        'switchesList',
        'activeFailuresList',
        'alternativePathsList',
        'recoveryHistoryBody'
    ];

    function setSkeletonState(loading) {
        SKELETON_HOST_IDS.forEach(function (id) {
            var host = byId(id);
            if (host) {
                host.classList.toggle('is-loading', !!loading);
            }
        });
    }

    // Bootstrap 5.3 ships the tooltip component in the bundle loaded ahead of
    // this script; the test harness stubs bootstrap with Toast only, so the
    // capability is checked before it is used.
    function initTooltips() {
        if (!window.bootstrap || typeof window.bootstrap.Tooltip !== 'function') {
            return;
        }
        Array.prototype.forEach.call(
            document.querySelectorAll('[data-bs-toggle="tooltip"]'),
            function (element) {
                window.bootstrap.Tooltip.getOrCreateInstance(element);
            }
        );
    }

    // -------------------------------------------------------- topology graph
    // The template ships an empty <svg id="topologyCanvas"> behind the
    // .topology-placeholder overlay; nothing ever drew into it, so the
    // placeholder covered an empty box forever. The graph is drawn from the poll
    // that already fetches /api/topology and /api/links - no extra requests.

    var SVG_NS = 'http://www.w3.org/2000/svg';

    function svgElement(name, attributes) {
        var node = document.createElementNS(SVG_NS, name);
        Object.keys(attributes || {}).forEach(function (key) {
            node.setAttribute(key, String(attributes[key]));
        });
        return node;
    }

    // /api/topology drops an edge when a link fails, so failed links are read
    // from /api/links. A pair reported by both sources is drawn as failed, which
    // is what the alert list and recovery status say as well.
    function mergeTopology(topology, links) {
        var nodes = (topology && topology.nodes) || [];
        var statusByPair = {};
        (links || []).forEach(function (link) {
            if (!link.source_device || !link.target_device) {
                return;
            }
            statusByPair[pairKey(link.source_device, link.target_device)]
                = (link.status && link.status !== 'active') ? 'failed' : 'active';
        });

        var edges = [];
        var seen = {};
        (topology && topology.edges ? topology.edges : []).forEach(function (edge) {
            var key = pairKey(edge.source, edge.target);
            seen[key] = true;
            edges.push({
                source: edge.source,
                target: edge.target,
                status: statusByPair[key] === 'failed' ? 'failed' : 'active'
            });
        });

        Object.keys(statusByPair).forEach(function (key) {
            if (seen[key]) {
                return;
            }
            var parts = key.split('-');
            edges.push({ source: parts[0], target: parts[1], status: statusByPair[key] });
        });

        return { nodes: nodes, edges: edges };
    }

    // Deterministic layout: seeded from the coordinates the API returns (stable
    // between polls) and refined with a fixed number of bounded force steps, so
    // the same data always draws the same picture and the graph never jumps
    // around. Steps are capped so the simulation cannot explode on the first
    // iteration and pile nodes up on the clamp.
    function computeLayout(graph) {
        var positions = {};
        graph.nodes.forEach(function (node) {
            var x = (typeof node.x === 'number' ? node.x : 400) / 800;
            var y = (typeof node.y === 'number' ? node.y : 300) / 600;
            positions[node.id] = {
                x: Math.min(0.95, Math.max(0.05, x)),
                y: Math.min(0.95, Math.max(0.05, y))
            };
        });

        var ids = graph.nodes.map(function (node) {
            return node.id;
        });
        if (ids.length < 2) {
            return positions;
        }

        var ITERATIONS = 300;
        var REPULSION = 0.08;
        var SPRING = 0.05;
        var REST_LENGTH = 0.18;
        var GRAVITY = 0.02;
        var MAX_STEP = 0.02;

        for (var step = 0; step < ITERATIONS; step += 1) {
            var cooling = 1 - (step / ITERATIONS) * 0.6;
            var forces = {};
            ids.forEach(function (id) {
                forces[id] = { x: 0, y: 0 };
            });

            for (var i = 0; i < ids.length; i += 1) {
                for (var j = i + 1; j < ids.length; j += 1) {
                    var a = positions[ids[i]];
                    var b = positions[ids[j]];
                    var dx = a.x - b.x;
                    var dy = a.y - b.y;
                    var distance = Math.sqrt(dx * dx + dy * dy) || 0.01;
                    var push = REPULSION / (distance * distance);
                    forces[ids[i]].x += (dx / distance) * push;
                    forces[ids[i]].y += (dy / distance) * push;
                    forces[ids[j]].x -= (dx / distance) * push;
                    forces[ids[j]].y -= (dy / distance) * push;
                }
            }

            graph.edges.forEach(function (edge) {
                var from = positions[edge.source];
                var to = positions[edge.target];
                if (!from || !to) {
                    return;
                }
                var ex = to.x - from.x;
                var ey = to.y - from.y;
                var length = Math.sqrt(ex * ex + ey * ey) || 0.01;
                var stretch = (length - REST_LENGTH) * SPRING;
                forces[edge.source].x += (ex / length) * stretch;
                forces[edge.source].y += (ey / length) * stretch;
                forces[edge.target].x -= (ex / length) * stretch;
                forces[edge.target].y -= (ey / length) * stretch;
            });

            ids.forEach(function (id) {
                var point = positions[id];
                forces[id].x = (forces[id].x + (0.5 - point.x) * GRAVITY) * cooling;
                forces[id].y = (forces[id].y + (0.5 - point.y) * GRAVITY) * cooling;

                var magnitude = Math.sqrt(
                    forces[id].x * forces[id].x + forces[id].y * forces[id].y);
                if (magnitude > MAX_STEP) {
                    forces[id].x = (forces[id].x / magnitude) * MAX_STEP;
                    forces[id].y = (forces[id].y / magnitude) * MAX_STEP;
                }

                point.x = Math.min(0.97, Math.max(0.03, point.x + forces[id].x));
                point.y = Math.min(0.97, Math.max(0.03, point.y + forces[id].y));
            });
        }

        return positions;
    }

    function containerSize(element) {
        var parent = element.parentNode;
        var width = element.clientWidth || (parent && parent.clientWidth) || 0;
        var height = element.clientHeight || (parent && parent.clientHeight) || 0;
        return {
            width: width || 600,
            height: height || 350
        };
    }

    // Link status changes must repaint immediately, so the cache key covers only
    // what moves a node: the node set, the API coordinates and the edge pairs.
    // A link failing or coming back leaves the key untouched and the graph keeps
    // the same layout while the styling updates.
    function layoutKey(graph) {
        return graph.nodes.map(function (node) {
            return node.id + '@' + (typeof node.x === 'number' ? node.x : '')
                + ',' + (typeof node.y === 'number' ? node.y : '');
        }).sort().join('|') + '#' + graph.edges.map(function (edge) {
            return pairKey(edge.source, edge.target);
        }).sort().join(',');
    }

    function layoutFor(graph) {
        var key = layoutKey(graph);
        var cached = state.topologyLayout;
        if (cached && cached.key === key) {
            return cached.positions;
        }
        var positions = computeLayout(graph);
        state.topologyLayout = { key: key, positions: positions };
        return positions;
    }

    function drawTopologyGraph(svg, graph) {
        var size = containerSize(svg);
        var padding = size.width < 420 ? 34 : 46;
        var drawWidth = Math.max(120, size.width - padding * 2);
        var drawHeight = Math.max(120, size.height - padding * 2 - 18);

        svg.setAttribute('viewBox', '0 0 ' + size.width + ' ' + size.height);
        svg.setAttribute('width', size.width);
        svg.setAttribute('height', size.height);
        while (svg.firstChild) {
            svg.removeChild(svg.firstChild);
        }

        var positions = layoutFor(graph);
        var hostRadius = size.width < 420 ? 7 : 8.5;
        function place(id) {
            var point = positions[id];
            return {
                x: padding + point.x * drawWidth,
                y: padding + point.y * drawHeight
            };
        }

        // Links first, so the node shapes are drawn on top of them.
        var edgeLayer = svgElement('g', { class: 'topology-edges' });
        graph.edges.forEach(function (edge) {
            if (!positions[edge.source] || !positions[edge.target]) {
                return;
            }
            var start = place(edge.source);
            var end = place(edge.target);
            edgeLayer.appendChild(svgElement('line', {
                class: 'topology-edge' + (edge.status === 'failed' ? ' failed' : ''),
                x1: start.x.toFixed(1),
                y1: start.y.toFixed(1),
                x2: end.x.toFixed(1),
                y2: end.y.toFixed(1),
                'data-source': edge.source,
                'data-target': edge.target,
                'data-status': edge.status
            }));
        });
        svg.appendChild(edgeLayer);

        var nodeLayer = svgElement('g', { class: 'topology-nodes' });
        graph.nodes.forEach(function (node) {
            var point = positions[node.id];
            if (!point) {
                return;
            }
            var here = place(node.id);
            var failed = node.status === 'failed';
            var isSwitch = node.type === 'switch';
            var attributes = {
                class: 'topology-node ' + (isSwitch ? 'switch' : 'host') + (failed ? ' failed' : ''),
                'data-node': node.id,
                'data-type': isSwitch ? 'switch' : 'host',
                'data-status': failed ? 'failed' : 'active'
            };

            var shape;
            if (isSwitch) {
                attributes.x = (here.x - hostRadius).toFixed(1);
                attributes.y = (here.y - hostRadius).toFixed(1);
                attributes.width = hostRadius * 2;
                attributes.height = hostRadius * 2;
                attributes.rx = 4;
                shape = svgElement('rect', attributes);
            } else {
                attributes.cx = here.x.toFixed(1);
                attributes.cy = here.y.toFixed(1);
                attributes.r = hostRadius;
                shape = svgElement('circle', attributes);
            }
            var hint = svgElement('title', {});
            hint.textContent = (node.label || node.id)
                + (node.ip ? ' (' + node.ip + ')' : '')
                + (failed ? ' - failed' : '');
            shape.appendChild(hint);
            nodeLayer.appendChild(shape);

            var label = svgElement('text', {
                class: 'topology-label',
                x: here.x.toFixed(1),
                y: (here.y + hostRadius + 12).toFixed(1),
                'text-anchor': 'middle'
            });
            label.textContent = node.label || node.id;
            nodeLayer.appendChild(label);
        });
        svg.appendChild(nodeLayer);

        svg.appendChild(buildTopologyLegend(graph, size, padding));
    }

    function buildTopologyLegend(graph, size, padding) {
        var hosts = graph.nodes.filter(function (node) {
            return node.type !== 'switch';
        }).length;
        var switches = graph.nodes.length - hosts;
        var failedLinks = graph.edges.filter(function (edge) {
            return edge.status === 'failed';
        }).length;

        var legend = svgElement('g', { class: 'topology-legend' });
        var box = 10;
        var x = padding;
        var y = size.height - 12;

        var items = [
            { className: 'legend-host', text: hosts + ' hosts' },
            { className: 'legend-switch', text: switches + ' switches' },
            { className: 'legend-edge', text: graph.edges.length + ' links' }
        ];
        if (failedLinks) {
            items.push({ className: 'legend-edge-failed', text: failedLinks + ' failed' });
        }

        items.forEach(function (item) {
            legend.appendChild(svgElement('rect', {
                class: 'topology-legend-swatch ' + item.className,
                x: x,
                y: y - box + 2,
                width: box,
                height: box,
                rx: 2
            }));
            var text = svgElement('text', {
                class: 'topology-legend-text',
                x: x + box + 5,
                y: y
            });
            text.textContent = item.text;
            legend.appendChild(text);
            x += box + 5 + (item.text.length * 6.2) + 14;
        });

        return legend;
    }

    function renderTopology() {
        var graph = state.topologyGraph;
        if (!graph) {
            return;
        }

        [ui.topologyCanvas, ui.detailedTopologyCanvas].forEach(function (svg) {
            if (!svg) {
                return;
            }
            // A section that is not on screen draws itself when it is shown.
            if (!svg.getClientRects().length && svg.offsetParent === null) {
                return;
            }
            try {
                drawTopologyGraph(svg, graph);
            } catch (err) {
                // a drawing problem must never break the rest of the dashboard
                showToast('error', 'Could not draw the topology: ' + err.message);
            }
        });

        var placeholder = ui.topologyPlaceholder;
        if (placeholder) {
            var hasNodes = graph.nodes.length > 0;
            placeholder.style.display = hasNodes ? 'none' : '';
            if (!hasNodes) {
                var caption = placeholder.querySelector('p');
                if (caption) {
                    caption.textContent = state.reachable
                        ? 'No topology data yet'
                        : 'Topology unavailable while the server is unreachable';
                }
            }
        }
    }

    function bindTopologyResize() {
        function redraw() {
            renderTopology();
        }
        window.addEventListener('resize', redraw);
        // jsdom and older browsers have no ResizeObserver; the window resize
        // listener plus the 3s poll cover those.
        if (typeof window.ResizeObserver === 'function') {
            var observer = new window.ResizeObserver(redraw);
            [ui.topologyCanvas, ui.detailedTopologyCanvas].forEach(function (svg) {
                if (svg && svg.parentNode) {
                    observer.observe(svg.parentNode);
                }
            });
        }
    }

    // ------------------------------------------------------------- polling

    var state = {
        pollInFlight: false,
        refreshQueued: false,
        pollTimer: null,
        reachable: true,
        monitoringRunning: false,
        lastAttempts: [],
        listenersBound: false,
        linkState: {},
        failureEndpoints: {},
        topologyGraph: null,
        topologyPayload: null,
        failurePanels: {
            entries: [],
            recoveries: [],
            failuresError: '',
            recoveriesError: ''
        },
        pathChecks: {},
        healthSamples: [],
        healthChartInstance: null,
        topologyLayout: null
    };

    // Order-independent key for a device pair, used by the topology merge and the
    // alert suppression.
    function pairKey(a, b) {
        return a < b ? a + '-' + b : b + '-' + a;
    }

    // Indexes what each failure was and what the backend currently reports for
    // that link, so a recovery result can be labelled as a reroute or a real
    // restore without guessing.
    function indexRecoveryContext(links, failures) {
        state.linkState = {};
        state.failureEndpoints = {};

        (links || []).forEach(function (link) {
            if (link.source_device && link.target_device) {
                state.linkState[pairKey(link.source_device, link.target_device)] = link.status;
            }
        });

        (failures || []).forEach(function (failure) {
            var isHost = failure.failure_type === 'host';
            state.failureEndpoints[failure.id] = {
                type: failure.failure_type,
                source: isHost ? failure.device_name : failure.link_source,
                target: isHost ? failure.device_name : failure.link_target
            };
        });
    }

    // Recovery reroutes traffic over an alternative path; the backend deliberately
    // leaves the failed link down, so a successful link recovery is reported as
    // "rerouted" for as long as /api/links still calls the link failed. Only a
    // link the backend reports as active again is described as restored.
    function recoveryOutcomeNote(failureId, status) {
        if (status !== 'success') {
            return null;
        }
        var failure = state.failureEndpoints[failureId];
        if (!failure) {
            return null;
        }
        if (failure.type === 'host') {
            return 'host restored: the device is active again';
        }
        if (!failure.source || !failure.target) {
            return null;
        }
        var linkActive = state.linkState[pairKey(failure.source, failure.target)] === 'active'
            || state.linkState[pairKey(failure.target, failure.source)] === 'active';
        return linkActive
            ? 'link restored: the backend reports the link as active'
            : 'traffic rerouted only, the link is still down (no physical restore)';
    }

    async function refreshDashboard() {
        // A refresh requested while one is running is queued, never dropped,
        // so post-action refreshes always reach the screen.
        if (state.pollInFlight) {
            state.refreshQueued = true;
            return;
        }
        state.pollInFlight = true;

        try {
            var results = await Promise.allSettled([
                getJson(API.status),
                getJson(API.topology),
                getJson(API.links),
                getJson(API.failures),
                getJson(API.recoveries)
            ]);

            var status = results[0].status === 'fulfilled' ? results[0].value : null;
            var topology = results[1].status === 'fulfilled' ? results[1].value : null;
            var links = results[2].status === 'fulfilled' ? results[2].value : [];
            var failures = results[3].status === 'fulfilled' ? results[3].value : [];
            var recoveries = results[4].status === 'fulfilled' ? results[4].value : [];

            if (results[0].status === 'rejected') {
                if (state.reachable) {
                    showToast('error', results[0].reason.message);
                }
                state.reachable = false;
                setText(ui.navStatusText, 'Server Unreachable');
                if (ui.navStatusDot) {
                    ui.navStatusDot.style.backgroundColor = '#dc3545';
                }
                setText(ui.lastUpdate, 'Last updated: --:--:--');
                return;
            }

            if (!state.reachable) {
                showToast('success', 'Connection to the server restored');
            }
            state.reachable = true;

            renderStats(status.stats);
            renderMonitoring(status.monitoring);
            recordHealthSample(status);
            renderHealthTrend();
            indexRecoveryContext(links, failures);
            state.topologyGraph = mergeTopology(topology, links);
            state.topologyPayload = topology;
            renderTopology();
            if (topology) {
                renderFailureTargets(topology);
            }
            var entries = buildFailureEntries(failures, topology, links);
            renderFailureAlerts(entries);
            renderRecoveryStatus(recoveries, state.lastAttempts);

            // Both Failure Detection panels read from the same payloads, so they
            // are refreshed by this poll and by the refresh that follows every
            // simulate/recovery action without any extra request.
            state.failurePanels.entries = entries;
            state.failurePanels.recoveries = recoveries;
            state.failurePanels.failuresError = results[3].status === 'rejected'
                ? String((results[3].reason && results[3].reason.message)
                    || results[3].reason) : '';
            state.failurePanels.recoveriesError = results[4].status === 'rejected'
                ? String((results[4].reason && results[4].reason.message)
                    || results[4].reason) : '';
            renderActiveFailures();
            renderAlternativePaths();

            if (status.timestamp) {
                setText(ui.lastUpdate, 'Last updated: ' + new Date(status.timestamp).toLocaleTimeString());
            }
        } catch (err) {
            showToast('error', err.message);
        } finally {
            state.pollInFlight = false;
            setSkeletonState(false);
            if (state.refreshQueued) {
                state.refreshQueued = false;
                await refreshDashboard();
            }
        }
    }

    // Chained timeout: exactly one timer, never overlapping requests.
    function scheduleNextPoll() {
        if (state.pollTimer !== null) {
            window.clearTimeout(state.pollTimer);
        }
        state.pollTimer = window.setTimeout(function () {
            refreshDashboard().then(scheduleNextPoll);
        }, POLL_INTERVAL_MS);
    }

    // ------------------------------------------------------------- actions

    function withBusy(button, work) {
        function restore() {
            if (!button) {
                return;
            }
            // The two monitoring buttons are owned by setMonitoringState, so
            // re-apply that state instead of blindly re-enabling them.
            if (button === ui.btnStartMonitoring || button === ui.btnStopMonitoring) {
                setMonitoringState(state.monitoringRunning);
            } else {
                button.disabled = false;
            }
        }

        if (button) {
            button.disabled = true;
        }

        return Promise.resolve()
            .then(work)
            .catch(function (err) {
                showToast('error', err && err.message ? err.message : String(err));
            })
            .then(restore);
    }

    // The Failure Detection form and the Network Controls button both simulate a
    // failure, so they share one entry point. A request that is already in flight
    // is never started a second time, whichever control was used, and a form
    // submit cannot fire twice while its button is disabled.
    var failureRequestInFlight = false;

    function runFailureSimulation(button) {
        if (failureRequestInFlight) {
            return Promise.resolve();
        }
        failureRequestInFlight = true;
        return withBusy(button, simulateFailure).then(function () {
            failureRequestInFlight = false;
        });
    }

    // The selects start with a blank option, so an untouched form keeps the
    // previous behaviour: resolveFailureTarget() falls back to the first
    // available target instead of always failing the device listed first.
    function fillDeviceSelect(select, options, placeholder) {
        if (!select) {
            return;
        }
        var previous = select.value;
        select.textContent = '';
        var blank = document.createElement('option');
        blank.value = '';
        blank.textContent = placeholder;
        select.appendChild(blank);
        options.forEach(function (entry) {
            var option = document.createElement('option');
            option.value = entry.id;
            option.textContent = entry.label;
            select.appendChild(option);
        });
        // The poll runs every 3s: keep the operator's choice while it is valid.
        if (previous && options.some(function (entry) { return entry.id === previous; })) {
            select.value = previous;
        }
    }

    function deviceOption(node) {
        return {
            id: node.id,
            label: node.id + (node.ip ? ' (' + node.ip + ')' : '')
                + ' - ' + (node.type === 'switch' ? 'switch' : 'host')
        };
    }

    function renderFailureTargets(topology) {
        var nodes = (topology && topology.nodes) || [];
        var activeHosts = nodes.filter(function (node) {
            return node.type !== 'switch' && node.status === 'active';
        });
        var activeDevices = nodes.filter(function (node) {
            return node.status === 'active';
        });

        fillDeviceSelect(ui.failureHost, activeHosts.map(deviceOption), 'First active host');
        fillDeviceSelect(ui.failureSource, activeDevices.map(deviceOption), 'Any active link');

        var source = ui.failureSource ? ui.failureSource.value : '';
        fillDeviceSelect(ui.failureTarget, activeDevices.filter(function (node) {
            return node.id !== source;
        }).map(deviceOption), 'Any active link');

        syncFailureFields();
    }

    // Only the fields the selected failure type uses are shown.
    function syncFailureFields() {
        var hostMode = !!(ui.failureType && ui.failureType.value === 'host');
        if (ui.linkFailureFields) {
            ui.linkFailureFields.style.display = hostMode ? 'none' : '';
        }
        if (ui.linkFailureTargetFields) {
            ui.linkFailureTargetFields.style.display = hostMode ? 'none' : '';
        }
        if (ui.hostFailureFields) {
            ui.hostFailureFields.style.display = hostMode ? '' : 'none';
        }
    }

    async function resolveFailureTarget() {
        var failureType = ui.failureType ? ui.failureType.value : 'link';

        if (failureType === 'host') {
            var host = ui.failureHost && ui.failureHost.value ? ui.failureHost.value : null;
            if (host) {
                return { failureType: 'host', host: host };
            }
        } else {
            var source = ui.failureSource ? ui.failureSource.value : '';
            var target = ui.failureTarget ? ui.failureTarget.value : '';
            if (source && target && source !== target) {
                return { failureType: 'link', source: source, target: target };
            }
        }

        var topology = await getJson(API.topology);

        if (failureType === 'host') {
            var activeHost = (topology.nodes || []).filter(function (node) {
                return node.type === 'host' && node.status === 'active';
            })[0];
            if (!activeHost) {
                throw new ApiError('No active host available to fail', 0, null);
            }
            return { failureType: 'host', host: activeHost.id };
        }

        var activeLink = (topology.edges || []).filter(function (edge) {
            return edge.status === 'active';
        })[0];
        if (!activeLink) {
            throw new ApiError('No active link available to fail', 0, null);
        }
        return { failureType: 'link', source: activeLink.source, target: activeLink.target };
    }

    async function simulateFailure() {
        var selection = await resolveFailureTarget();
        var response;

        if (selection.failureType === 'host') {
            response = await postJson(API.simulateHostFailure, { host: selection.host });
            if (response && response.success) {
                showToast('success', 'Failure simulated: host ' + response.host + ' is down');
            } else {
                showToast('error', serverMessage(response, '', 'Server did not fail host ' + selection.host));
            }
        } else {
            response = await postJson(API.simulateLinkFailure, {
                source: selection.source,
                target: selection.target
            });
            if (response && response.success) {
                showToast('success', 'Failure simulated: link ' + response.source + ' <-> ' + response.target + ' is down');
            } else {
                showToast('error', serverMessage(response, '', 'Server did not fail link ' + selection.source + ' <-> ' + selection.target));
            }
        }

        await refreshDashboard();
    }

    async function startMonitoring() {
        var interval = ui.checkInterval && ui.checkInterval.value ? Number(ui.checkInterval.value) : 2;
        if (!isFinite(interval) || interval <= 0) {
            interval = 2;
        }
        var demoMode = ui.demoMode ? !!ui.demoMode.checked : true;

        var response = await postJson(API.monitoringStart, { interval: interval, demo_mode: demoMode });

        if (response && response.success) {
            setMonitoringState(true);
            setText(ui.sidebarStatusText, 'Monitoring Active');
            if (ui.sidebarStatus) {
                ui.sidebarStatus.classList.add('bg-success');
                ui.sidebarStatus.classList.remove('bg-secondary');
            }
            showToast('success', 'Monitoring started (every ' + response.interval + 's, ' + (response.demo_mode ? 'demo mode' : 'live mode') + ')');
        } else {
            showToast('error', serverMessage(response, '', 'Server did not start monitoring'));
        }

        await refreshDashboard();
    }

    async function stopMonitoring() {
        var response = await postJson(API.monitoringStop, {});

        if (response && response.success) {
            setMonitoringState(false);
            setText(ui.sidebarStatusText, 'Monitoring Stopped');
            if (ui.sidebarStatus) {
                ui.sidebarStatus.classList.remove('bg-success');
                ui.sidebarStatus.classList.add('bg-secondary');
            }
            showToast('success', 'Monitoring stopped');
        } else {
            showToast('error', serverMessage(response, '', 'Server did not stop monitoring'));
        }

        await refreshDashboard();
    }

    async function recoverNetwork() {
        // ?unresolved=1 returns every unresolved failure, so a failure older than
        // the newest 50 events is still recovered instead of being missed.
        var failures = await getJson(API.unresolvedFailures);
        var unresolved = (failures || []).filter(function (failure) {
            return !failure.resolved;
        });

        if (unresolved.length === 0) {
            showToast('info', 'No active failures to recover');
            return;
        }

        var attempts = [];
        var succeeded = 0;
        var failed = 0;

        for (var index = 0; index < unresolved.length; index += 1) {
            var failure = unresolved[index];
            var isHost = failure.failure_type === 'host';
            var source = isHost ? (failure.device_name || failure.link_source) : failure.link_source;
            var target = isHost ? (failure.device_name || failure.link_source) : failure.link_target;

            if (!failure.id || !source || !target) {
                failed += 1;
                attempts.push({
                    failureId: failure.id,
                    status: 'failed',
                    message: 'Failure record #' + failure.id + ' has no usable endpoint (source=' + (source || '-') + ', target=' + (target || '-') + ')',
                    path: null
                });
                showToast('error', 'Failure record #' + failure.id + ' has no usable endpoint, skipped');
                continue;
            }

            try {
                var response = await postJson(API.recoveryTrigger, {
                    failure_id: failure.id,
                    source: source,
                    target: target,
                    failure_type: failure.failure_type || 'link'
                });

                var status = (response && response.status) || 'failed';
                var message = serverMessage(response, '', 'Recovery returned no status for failure #' + failure.id);

                attempts.push({
                    failureId: failure.id,
                    status: status,
                    message: message,
                    path: response && response.path ? response.path : null
                });

                if (status === 'success') {
                    succeeded += 1;
                    showToast('success', message);
                } else {
                    failed += 1;
                    showToast('error', message);
                }
            } catch (err) {
                failed += 1;
                attempts.push({
                    failureId: failure.id,
                    status: 'error',
                    message: err && err.message ? err.message : String(err),
                    path: null
                });
                showToast('error', err && err.message ? err.message : String(err));
            }
        }

        state.lastAttempts = attempts;
        showToast(failed === 0 ? 'success' : 'info', 'Recovery finished: ' + succeeded + ' succeeded, ' + failed + ' failed');

        await refreshDashboard();
    }

    // ---------------------------------------------------------------- init

    function bindEvents() {
        if (state.listenersBound) {
            return;
        }
        state.listenersBound = true;

        if (ui.btnSimulateFailure) {
            ui.btnSimulateFailure.addEventListener('click', function () {
                runFailureSimulation(ui.btnSimulateFailure);
            });
        }
        if (ui.failureForm) {
            ui.failureForm.addEventListener('submit', function (event) {
                event.preventDefault();
                runFailureSimulation(ui.failureSubmitButton);
            });
        }
        if (ui.failureType) {
            ui.failureType.addEventListener('change', syncFailureFields);
        }
        if (ui.failureSource) {
            // Re-fill the target list so the same device cannot be picked twice.
            ui.failureSource.addEventListener('change', function () {
                renderFailureTargets(state.topologyPayload);
            });
        }
        if (ui.btnStartMonitoring) {
            ui.btnStartMonitoring.addEventListener('click', function () {
                withBusy(ui.btnStartMonitoring, startMonitoring);
            });
        }
        if (ui.btnStopMonitoring) {
            ui.btnStopMonitoring.addEventListener('click', function () {
                withBusy(ui.btnStopMonitoring, stopMonitoring);
            });
        }
        if (ui.btnRecoverNetwork) {
            ui.btnRecoverNetwork.addEventListener('click', function () {
                withBusy(ui.btnRecoverNetwork, recoverNetwork);
            });
        }
        if (ui.btnRefreshLogs) {
            ui.btnRefreshLogs.addEventListener('click', function () {
                withBusy(ui.btnRefreshLogs, loadEvents);
            });
        }
        if (ui.activeFailuresList) {
            // One delegated listener on the panel that holds the buttons, bound
            // once here. Rows are replaced on every poll, so per-row listeners
            // would either be lost or multiply; delegation survives any number of
            // re-renders.
            ui.activeFailuresList.addEventListener('click', function (event) {
                var trigger = event.target && event.target.closest
                    ? event.target.closest('[data-path-check]')
                    : null;
                if (!trigger || !ui.activeFailuresList.contains(trigger)) {
                    return;
                }
                var source = trigger.getAttribute('data-source');
                var target = trigger.getAttribute('data-target');
                if (!source || !target || trigger.disabled) {
                    return;
                }
                trigger.disabled = true;
                trigger.querySelector('.loading-spinner')
                    .classList.remove('d-none');
                checkAlternativePaths(source, target).then(function () {
                    trigger.disabled = false;
                    trigger.querySelector('.loading-spinner')
                        .classList.add('d-none');
                });
            });
        }

        // The sidebar switches the content sections. The Network Topology section
        // carries its own graph, so it has to be drawn when it becomes visible.
        var navLinks = document.querySelectorAll('[data-section]');
        Array.prototype.forEach.call(navLinks, function (link) {
            link.addEventListener('click', function (event) {
                event.preventDefault();
                showSection(link.getAttribute('data-section'));
            });
        });
    }

    function showSection(name) {
        document.querySelectorAll('.content-section').forEach(function (section) {
            section.classList.toggle('d-none', section.id !== 'section-' + name);
        });
        Array.prototype.forEach.call(document.querySelectorAll('[data-section]'), function (link) {
            link.classList.toggle('active', link.getAttribute('data-section') === name);
        });
        // the newly visible graph has no size until now
        renderTopology();
    }

    function init() {
        cacheElements();
        bindEvents();
        bindTopologyResize();
        initTooltips();
        syncFailureFields();
        setMonitoringState(false);
        setSkeletonState(true);
        // Event History is not part of the five-request poll cycle; it is read
        // once here and then only when Refresh is pressed.
        loadEvents();
        refreshDashboard().then(scheduleNextPoll);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
