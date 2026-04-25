from __future__ import annotations

import calendar
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from app.config import Settings
from app.db import connect_db
from app.schemas import VmTrafficConfigRequest


DEFAULT_TZ = "Asia/Shanghai"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime:
    if not value:
        return utc_now()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TZ)


def month_reset(moment: datetime, day: int, hour: int, tz_name: str) -> tuple[datetime, datetime]:
    tz = safe_zone(tz_name)
    local = moment.astimezone(tz)

    def reset_for(year: int, month: int) -> datetime:
        bounded_day = min(max(1, day), calendar.monthrange(year, month)[1])
        return datetime(year, month, bounded_day, hour, tzinfo=tz)

    current = reset_for(local.year, local.month)
    if local >= current:
        next_year = local.year + 1 if local.month == 12 else local.year
        next_month = 1 if local.month == 12 else local.month + 1
        next_reset = reset_for(next_year, next_month)
        return current.astimezone(timezone.utc), next_reset.astimezone(timezone.utc)

    prev_year = local.year - 1 if local.month == 1 else local.year
    prev_month = 12 if local.month == 1 else local.month - 1
    previous = reset_for(prev_year, prev_month)
    return previous.astimezone(timezone.utc), current.astimezone(timezone.utc)


def bytes_to_gb(value: int | float) -> float:
    return round(float(value or 0) / 1_000_000_000, 6)


def current_counters(status: dict[str, Any]) -> tuple[int, int]:
    network = status.get("network") or {}
    tap = network.get("tap_traffic") or {}
    rx = int(tap.get("rx_bytes") or network.get("netin_bytes") or 0)
    tx = int(tap.get("tx_bytes") or network.get("netout_bytes") or 0)
    return rx, tx


def set_vm_traffic_config(
    settings: Settings,
    vmid: int,
    req: VmTrafficConfigRequest,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rx, tx = current_counters(status or {})
    now = utc_now().isoformat()
    existing = get_config_row(settings, vmid)
    if existing is None or req.reset_usage:
        used_rx = 0
        used_tx = 0
        baseline_rx = rx
        baseline_tx = tx
        baseline_at = now
        period_started_at = now
    else:
        tracked = update_usage_from_counters(settings, existing, status or {})
        used_rx = int(tracked.get("used_rx_bytes") or 0)
        used_tx = int(tracked.get("used_tx_bytes") or 0)
        baseline_rx = int(tracked.get("baseline_rx_bytes") or 0)
        baseline_tx = int(tracked.get("baseline_tx_bytes") or 0)
        baseline_at = str(tracked.get("baseline_at") or now)
        period_started_at = tracked.get("period_started_at") or baseline_at
    with connect_db(settings) as conn:
        conn.execute(
            """
            INSERT INTO vm_traffic_configs(
                vmid, quota_gb, reset_day, reset_hour, timezone,
                baseline_rx_bytes, baseline_tx_bytes,
                used_rx_bytes, used_tx_bytes, last_rx_bytes, last_tx_bytes,
                period_started_at, baseline_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vmid) DO UPDATE SET
                quota_gb = excluded.quota_gb,
                reset_day = excluded.reset_day,
                reset_hour = excluded.reset_hour,
                timezone = excluded.timezone,
                baseline_rx_bytes = excluded.baseline_rx_bytes,
                baseline_tx_bytes = excluded.baseline_tx_bytes,
                used_rx_bytes = excluded.used_rx_bytes,
                used_tx_bytes = excluded.used_tx_bytes,
                last_rx_bytes = excluded.last_rx_bytes,
                last_tx_bytes = excluded.last_tx_bytes,
                period_started_at = excluded.period_started_at,
                baseline_at = excluded.baseline_at,
                updated_at = excluded.updated_at
            """,
            (
                vmid,
                req.quota_gb,
                req.reset_day,
                req.reset_hour,
                req.timezone or DEFAULT_TZ,
                baseline_rx,
                baseline_tx,
                used_rx,
                used_tx,
                rx,
                tx,
                period_started_at,
                baseline_at,
                now,
            ),
        )
    return get_vm_traffic_usage(settings, vmid, status or {})


def get_config_row(settings: Settings, vmid: int) -> dict[str, Any] | None:
    with connect_db(settings) as conn:
        row = conn.execute(
            "SELECT * FROM vm_traffic_configs WHERE vmid = ?",
            (vmid,),
        ).fetchone()
    return dict(row) if row else None


def reset_period(
    settings: Settings,
    row: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    rx, tx = current_counters(status)
    now = utc_now().isoformat()
    with connect_db(settings) as conn:
        conn.execute(
            """
            UPDATE vm_traffic_configs
            SET baseline_rx_bytes = ?, baseline_tx_bytes = ?,
                used_rx_bytes = 0, used_tx_bytes = 0,
                last_rx_bytes = ?, last_tx_bytes = ?,
                period_started_at = ?, baseline_at = ?, updated_at = ?
            WHERE vmid = ?
            """,
            (rx, tx, rx, tx, now, now, now, row["vmid"]),
        )
    updated = dict(row)
    updated.update(
        {
            "baseline_rx_bytes": rx,
            "baseline_tx_bytes": tx,
            "used_rx_bytes": 0,
            "used_tx_bytes": 0,
            "last_rx_bytes": rx,
            "last_tx_bytes": tx,
            "period_started_at": now,
            "baseline_at": now,
        }
    )
    return updated


def ensure_period_baseline(
    settings: Settings,
    row: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    now = utc_now()
    period_start, _ = month_reset(
        now,
        int(row["reset_day"]),
        int(row["reset_hour"]),
        str(row["timezone"]),
    )
    baseline_at = parse_dt(row.get("baseline_at"))
    if baseline_at >= period_start:
        return row
    return reset_period(settings, row, status)


def update_usage_from_counters(
    settings: Settings,
    row: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    row = ensure_period_baseline(settings, row, status)
    rx, tx = current_counters(status)
    last_rx = int(row.get("last_rx_bytes") or row.get("baseline_rx_bytes") or 0)
    last_tx = int(row.get("last_tx_bytes") or row.get("baseline_tx_bytes") or 0)
    used_rx = int(row.get("used_rx_bytes") or 0)
    used_tx = int(row.get("used_tx_bytes") or 0)
    rx_delta = rx - last_rx if rx >= last_rx else rx
    tx_delta = tx - last_tx if tx >= last_tx else tx
    used_rx += max(0, rx_delta)
    used_tx += max(0, tx_delta)
    now = utc_now().isoformat()
    with connect_db(settings) as conn:
        conn.execute(
            """
            UPDATE vm_traffic_configs
            SET used_rx_bytes = ?, used_tx_bytes = ?,
                last_rx_bytes = ?, last_tx_bytes = ?, updated_at = ?
            WHERE vmid = ?
            """,
            (used_rx, used_tx, rx, tx, now, row["vmid"]),
        )
    updated = dict(row)
    updated["used_rx_bytes"] = used_rx
    updated["used_tx_bytes"] = used_tx
    updated["last_rx_bytes"] = rx
    updated["last_tx_bytes"] = tx
    return updated


def get_vm_traffic_usage(
    settings: Settings,
    vmid: int,
    status: dict[str, Any],
) -> dict[str, Any]:
    row = get_config_row(settings, vmid)
    if row is None:
        rx, tx = current_counters(status)
        return {
            "vmid": vmid,
            "configured": False,
            "quota_gb": None,
            "reset_day": 1,
            "reset_hour": 0,
            "timezone": DEFAULT_TZ,
            "used_gb": bytes_to_gb(rx + tx),
            "remaining_gb": None,
            "percent": None,
            "next_reset_at": month_reset(utc_now(), 1, 0, DEFAULT_TZ)[1].isoformat(),
            "baseline_at": None,
        }

    row = update_usage_from_counters(settings, row, status)
    used_bytes = int(row.get("used_rx_bytes") or 0) + int(row.get("used_tx_bytes") or 0)
    used_gb = bytes_to_gb(used_bytes)
    quota_gb = row.get("quota_gb")
    remaining_gb = None
    percent = None
    if quota_gb:
        quota = float(quota_gb)
        remaining_gb = round(max(0.0, quota - used_gb), 6)
        percent = round(min(100.0, used_gb / quota * 100), 6)
    _, next_reset = month_reset(
        utc_now(),
        int(row["reset_day"]),
        int(row["reset_hour"]),
        str(row["timezone"]),
    )
    return {
        "vmid": vmid,
        "configured": True,
        "quota_gb": quota_gb,
        "reset_day": int(row["reset_day"]),
        "reset_hour": int(row["reset_hour"]),
        "timezone": str(row["timezone"]),
        "used_gb": used_gb,
        "remaining_gb": remaining_gb,
        "percent": percent,
        "next_reset_at": next_reset.isoformat(),
        "baseline_at": row["baseline_at"],
    }
