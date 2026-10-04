import sqlite3
import logging
import os
import re
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import List, Dict, Any, Optional

from db_config import resolve_database_path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Single source of truth: db_config reads SHN_DATABASE_PATH and raises a clear
# error when it is missing or unusable, so this module can never silently open
# the production network.db. `python app.py` sets the development database
# before importing this module; run_tests.py sets a temporary one.
DATABASE_PATH = resolve_database_path()
logger.info("Database file: %s", DATABASE_PATH)

# Every stored timestamp is UTC in the same text shape SQLite's CURRENT_TIMESTAMP
# produces (that default is UTC too), plus microseconds so a failure and an
# earlier resolution can be ordered even inside the same second, plus an explicit
# "+00:00" offset so a UTC value is never mistaken for a host local one.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
NAIVE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$")
OFFSET_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$")

def utc_now() -> str:
    """Timezone-aware UTC now, as an explicitly tagged string (never local time)."""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT) + "+00:00"

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_database():
    """Initialize all database tables."""
    with get_db_connection() as conn:
        cursor = conn.cursor()        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                device_type TEXT NOT NULL,
                ip_address TEXT,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_device TEXT NOT NULL,
                target_device TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                bandwidth TEXT DEFAULT '100Mbps',
                latency REAL DEFAULT 0.5,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_device) REFERENCES devices(name),
                FOREIGN KEY (target_device) REFERENCES devices(name)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS failure_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_name TEXT,
                link_source TEXT,
                link_target TEXT,
                failure_type TEXT NOT NULL,
                description TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved BOOLEAN DEFAULT FALSE,
                resolved_at TIMESTAMP,
                recovery_duration REAL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recovery_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                failure_event_id INTEGER,
                alternative_path TEXT,
                recovery_status TEXT,
                description TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (failure_event_id) REFERENCES failure_events(id)
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS monitoring_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                device_name TEXT,
                message TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS network_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_hosts INTEGER,
                total_switches INTEGER,
                active_links INTEGER,
                failed_links INTEGER,
                health_percentage REAL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Supports the ORDER BY in get_failures_for_dashboard() (unresolved rows
        # plus the newest resolved rows) without a full scan of the table.
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_failure_events_resolved_timestamp
            ON failure_events(resolved, timestamp DESC, id DESC)
        """)

        # Records which one-off data migrations have already run.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        conn.commit()
        logger.info("Database initialized successfully")

    # Runs once, after the tables exist, and only touches values that older
    # versions wrote in host local time.
    migrate_timestamps_to_utc()

def migrate_timestamps_to_utc() -> Dict[str, int]:
    """Convert the host-local timestamps written by older versions to UTC.

    Until now Python wrote datetime.now() (host local time) into
    devices.created_at/updated_at, links.created_at/updated_at and
    failure_events.resolved_at, while the SQLite CURRENT_TIMESTAMP default wrote
    UTC into the failure_events.timestamp column. Mixing the two means "was this
    link failed before that failure was resolved?" cannot be answered, so the
    untagged local values are converted once using the host's current UTC offset.

    Only the columns Python used to write are touched, and only values that carry
    no UTC offset: utc_now() tags every value it writes, so anything already
    tagged is left exactly as it is and the migration is safe to re-run. Tables
    are backed up before the first conversion and the migration is also recorded
    in schema_meta.
    """
    local_columns = {
        "devices": ("created_at", "updated_at"),
        "links": ("created_at", "updated_at"),
        "failure_events": ("resolved_at",),
    }
    migrated: Dict[str, int] = {}
    marker = "timestamps_to_utc"

    with get_db_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT value FROM schema_meta WHERE key = ?", (marker,)).fetchone()
        if row:
            logger.info("Timestamps already migrated to UTC, skipping")
            return migrated

        for table, columns in local_columns.items():
            columns_sql = ", ".join(columns)
            for column in columns:
                values = [r[0] for r in cursor.execute(
                    f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()]
                backup = f"{table}_localtime_backup"
                if values and not cursor.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                        (backup,)).fetchone():
                    cursor.execute(
                        f"CREATE TABLE {backup} AS SELECT * FROM {table}")
                    logger.info("Backed up %s to %s", table, backup)

                for value in values:
                    if not isinstance(value, str) or not NAIVE_TIMESTAMP.match(value):
                        continue  # already tagged as UTC, or not a timestamp we wrote
                    parsed = datetime.strptime(value.split(".")[0], "%Y-%m-%d %H:%M:%S")
                    # strptime() gives a naive datetime, so astimezone() reads it
                    # as the local time it was written in and converts to UTC.
                    utc_value = parsed.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT) + "+00:00"
                    cursor.execute(
                        f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                        (utc_value, value))
                    migrated[f"{table}.{column}"] = migrated.get(f"{table}.{column}", 0) + 1

        cursor.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                       (marker, utc_now()))
        conn.commit()

    if migrated:
        logger.info("Converted local timestamps to UTC: %s", migrated)
    else:
        logger.info("No local timestamps needed conversion")
    return migrated

# Device operations
def insert_device(name: str, device_type: str, ip_address: str = None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO devices (name, device_type, ip_address, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (name, device_type, ip_address, utc_now(), utc_now()))
        conn.commit()

def update_device_status(name: str, status: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE devices SET status = ?, updated_at = ? WHERE name = ?
        """, (status, utc_now(), name))
        conn.commit()

def get_all_devices() -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM devices ORDER BY device_type, name")
        return [dict(row) for row in cursor.fetchall()]

# Link operations
def insert_link(source: str, target: str, status: str = 'active', bandwidth: str = '100Mbps', latency: float = 0.5):
    # The links table has no unique constraint on the device pair, so a plain
    # INSERT OR REPLACE appended a new row every time the topology was built
    # (twice at startup under the debug reloader, plus every /api/reset) and
    # /api/links kept returning duplicates per pair. Replace the pair instead.
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM links
            WHERE (source_device = ? AND target_device = ?)
               OR (source_device = ? AND target_device = ?)
        """, (source, target, target, source))
        cursor.execute("""
            INSERT INTO links (source_device, target_device, status, bandwidth, latency, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (source, target, status, bandwidth, latency, utc_now(), utc_now()))
        conn.commit()

def update_link_status(source: str, target: str, status: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE links SET status = ?, updated_at = ? 
            WHERE (source_device = ? AND target_device = ?) OR (source_device = ? AND target_device = ?)
        """, (status, utc_now(), source, target, target, source))
        conn.commit()

def get_all_links() -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM links ORDER BY source_device, target_device")
        return [dict(row) for row in cursor.fetchall()]

# Failure events
def log_failure_event(device_name: str = None, link_source: str = None, link_target: str = None, 
                      failure_type: str = 'link', description: str = '') -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO failure_events (device_name, link_source, link_target, failure_type, description, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (device_name, link_source, link_target, failure_type, description, utc_now()))
        conn.commit()
        return cursor.lastrowid

def get_failure_events(limit: int = 50) -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM failure_events ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]

def get_active_failures() -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM failure_events WHERE resolved = FALSE ORDER BY timestamp DESC")
        return [dict(row) for row in cursor.fetchall()]

def get_active_failure_count() -> int:
    """Count unresolved failure events (used by failure_detector and recovery_manager)."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM failure_events WHERE resolved = FALSE")
        row = cursor.fetchone()
        return row[0] if row else 0

def get_failures_for_dashboard(resolved_limit: int = 50) -> List[Dict]:
    """Every unresolved failure plus the most recent resolved ones.

    The dashboard merges these rows with the failed links and nodes reported by
    the topology. Capping the whole list at 50 rows would drop an unresolved
    failure that is not among the newest events, which loses its #id in Failure
    Alerts and lets a resolved event for the same pair hide a new failure.
    Resolved rows are only kept for suppression, so they stay capped.
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM failure_events WHERE COALESCE(resolved, 0) = 0
            UNION ALL
            SELECT * FROM (
                SELECT * FROM failure_events WHERE COALESCE(resolved, 0) = 1
                ORDER BY timestamp DESC, id DESC LIMIT ?
            )
            ORDER BY timestamp DESC, id DESC
        """, (resolved_limit,))
        return [dict(row) for row in cursor.fetchall()]

# Recovery events
def log_recovery_event(failure_event_id: int, alternative_path: str, recovery_status: str, description: str = ''):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO recovery_events (failure_event_id, alternative_path, recovery_status, description, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (failure_event_id, alternative_path, recovery_status, description, utc_now()))
        
        if recovery_status == 'success':
            cursor.execute("""
                UPDATE failure_events SET resolved = TRUE, resolved_at = ?, recovery_duration = ?
                WHERE id = ?
            """, (utc_now(), 0.0, failure_event_id))
        
        conn.commit()

def get_recovery_events(limit: int = 50) -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM recovery_events ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]

# Monitoring logs
def log_monitoring_event(event_type: str, device_name: str = None, message: str = ''):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO monitoring_logs (event_type, device_name, message, timestamp)
            VALUES (?, ?, ?, ?)
        """, (event_type, device_name, message, utc_now()))
        conn.commit()

def get_monitoring_logs(limit: int = 100) -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitoring_logs ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]

# Network stats
def log_network_stats(total_hosts: int, total_switches: int, active_links: int, failed_links: int, health: float):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO network_stats (total_hosts, total_switches, active_links, failed_links, health_percentage, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (total_hosts, total_switches, active_links, failed_links, health, utc_now()))
        conn.commit()

def get_network_stats_history(limit: int = 50) -> List[Dict]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM network_stats ORDER BY timestamp DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]

def clear_all_data():
    """Reset all tables - useful for testing."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM devices")
        cursor.execute("DELETE FROM links")
        cursor.execute("DELETE FROM failure_events")
        cursor.execute("DELETE FROM recovery_events")
        cursor.execute("DELETE FROM monitoring_logs")
        cursor.execute("DELETE FROM network_stats")
        conn.commit()