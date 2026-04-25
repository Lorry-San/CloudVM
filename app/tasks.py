from __future__ import annotations

from app.config import Settings
from app.db import connect_db


def record_task_log(
    settings: Settings,
    vmid: int,
    action: str,
    status: str = "ok",
    task_id: str | None = None,
    message: str | None = None,
) -> None:
    with connect_db(settings) as conn:
        conn.execute(
            """
            INSERT INTO vm_task_logs(vmid, action, status, task_id, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (vmid, action, status, task_id, message),
        )


def list_task_logs(
    settings: Settings,
    vmid: int,
    limit: int = 50,
) -> list[dict[str, object]]:
    bounded_limit = max(1, min(200, int(limit or 50)))
    with connect_db(settings) as conn:
        rows = conn.execute(
            """
            SELECT id, vmid, action, status, task_id, message, created_at
            FROM vm_task_logs
            WHERE vmid = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (vmid, bounded_limit),
        ).fetchall()
    return [dict(row) for row in rows]
