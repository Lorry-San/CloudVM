from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import Settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS ip_pool (
    address TEXT PRIMARY KEY,
    cidr INTEGER NOT NULL,
    gateway TEXT NOT NULL,
    nameserver TEXT,
    bridge TEXT,
    status TEXT NOT NULL DEFAULT 'available',
    vmid INTEGER,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    allocated_at TEXT,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_ip_pool_status ON ip_pool(status);
CREATE INDEX IF NOT EXISTS idx_ip_pool_vmid ON ip_pool(vmid);

CREATE TABLE IF NOT EXISTS vm_credentials (
    vmid INTEGER PRIMARY KEY,
    username TEXT,
    password TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metric_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    vmid INTEGER,
    sampled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cpu_percent REAL NOT NULL DEFAULT 0,
    memory_percent REAL NOT NULL DEFAULT 0,
    disk_percent REAL NOT NULL DEFAULT 0,
    io_gb REAL NOT NULL DEFAULT 0,
    network_gb REAL NOT NULL DEFAULT 0,
    traffic_gb REAL NOT NULL DEFAULT 0,
    gpu_percent REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_metric_samples_scope_time
    ON metric_samples(scope, sampled_at);
CREATE INDEX IF NOT EXISTS idx_metric_samples_vmid_time
    ON metric_samples(vmid, sampled_at);

CREATE TABLE IF NOT EXISTS vm_traffic_configs (
    vmid INTEGER PRIMARY KEY,
    quota_gb REAL,
    reset_day INTEGER NOT NULL DEFAULT 1,
    reset_hour INTEGER NOT NULL DEFAULT 0,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    baseline_rx_bytes INTEGER NOT NULL DEFAULT 0,
    baseline_tx_bytes INTEGER NOT NULL DEFAULT 0,
    used_rx_bytes INTEGER NOT NULL DEFAULT 0,
    used_tx_bytes INTEGER NOT NULL DEFAULT 0,
    last_rx_bytes INTEGER NOT NULL DEFAULT 0,
    last_tx_bytes INTEGER NOT NULL DEFAULT 0,
    period_started_at TEXT,
    baseline_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vm_task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vmid INTEGER NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    task_id TEXT,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vm_task_logs_vmid_time
    ON vm_task_logs(vmid, created_at);
"""


def init_db(settings: Settings) -> None:
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        ensure_columns(
            conn,
            "vm_traffic_configs",
            {
                "used_rx_bytes": "INTEGER NOT NULL DEFAULT 0",
                "used_tx_bytes": "INTEGER NOT NULL DEFAULT 0",
                "last_rx_bytes": "INTEGER NOT NULL DEFAULT 0",
                "last_tx_bytes": "INTEGER NOT NULL DEFAULT 0",
                "period_started_at": "TEXT",
            },
        )


def ensure_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


@contextmanager
def connect_db(settings: Settings) -> Iterator[sqlite3.Connection]:
    init_db(settings)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
