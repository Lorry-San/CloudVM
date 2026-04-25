from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.db import connect_db


def percent(used: int | float, total: int | float) -> float:
    total_value = float(total or 0)
    if total_value <= 0:
        return 0.0
    return round(max(0.0, min(100.0, float(used or 0) / total_value * 100)), 6)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cutoff_iso(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def metric_sample_from_status(status: dict[str, Any]) -> dict[str, Any]:
    memory = status.get("memory") or {}
    disk = status.get("disk") or {}
    io = status.get("io") or {}
    network = status.get("network") or {}
    tap = network.get("tap_traffic") or {}
    scope = str(status.get("scope") or "host")
    net_total = float(network.get("total_gb") or 0)
    traffic_total = net_total
    if scope == "vm":
        if tap.get("total_gb") is not None:
            net_total = float(tap.get("total_gb") or 0)
        else:
            net_total = float(network.get("netin_gb") or 0) + float(
                network.get("netout_gb") or 0
            )
        traffic_total = float(tap.get("total_gb") or net_total)
    return {
        "scope": scope,
        "vmid": status.get("vmid"),
        "sampled_at": iso_now(),
        "cpu_percent": round(float((status.get("cpu") or {}).get("usage") or 0) * 100, 6),
        "memory_percent": percent(memory.get("used_bytes"), memory.get("total_bytes")),
        "disk_percent": percent(disk.get("used_bytes"), disk.get("total_bytes")),
        "io_gb": round(float(io.get("read_gb") or 0) + float(io.get("write_gb") or 0), 6),
        "network_gb": round(net_total, 6),
        "traffic_gb": round(traffic_total, 6),
        "gpu_percent": 0.0,
    }


def record_metric_sample(settings: Settings, status: dict[str, Any]) -> dict[str, Any]:
    sample = metric_sample_from_status(status)
    with connect_db(settings) as conn:
        conn.execute(
            """
            INSERT INTO metric_samples(
                scope, vmid, sampled_at, cpu_percent, memory_percent, disk_percent,
                io_gb, network_gb, traffic_gb, gpu_percent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample["scope"],
                sample["vmid"],
                sample["sampled_at"],
                sample["cpu_percent"],
                sample["memory_percent"],
                sample["disk_percent"],
                sample["io_gb"],
                sample["network_gb"],
                sample["traffic_gb"],
                sample["gpu_percent"],
            ),
        )
        conn.execute(
            "DELETE FROM metric_samples WHERE sampled_at < ?",
            (cutoff_iso(24),),
        )
    return sample


def list_metric_samples(
    settings: Settings,
    vmid: int | None = None,
    hours: int = 24,
) -> list[dict[str, Any]]:
    bounded_hours = max(1, min(24, int(hours or 24)))
    with connect_db(settings) as conn:
        if vmid is None:
            rows = conn.execute(
                """
                SELECT * FROM metric_samples
                WHERE scope = 'host' AND sampled_at >= ?
                ORDER BY sampled_at
                """,
                (cutoff_iso(bounded_hours),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM metric_samples
                WHERE scope = 'vm' AND vmid = ? AND sampled_at >= ?
                ORDER BY sampled_at
                """,
                (vmid, cutoff_iso(bounded_hours)),
            ).fetchall()
    return [dict(row) for row in rows]
