from __future__ import annotations

from app.config import Settings
from app.db import connect_db


def save_vm_credentials(
    settings: Settings,
    vmid: int,
    username: str | None,
    password: str | None,
) -> tuple[bool, bool]:
    normalized_username = (username or "").strip() or None
    normalized_password = (password or "").strip() or None
    # If user supplies a password but leaves username empty, default to root.
    if normalized_password and not normalized_username:
        normalized_username = "root"
    if not normalized_username and not normalized_password:
        return (False, False)
    with connect_db(settings) as conn:
        conn.execute(
            """
            INSERT INTO vm_credentials(vmid, username, password)
            VALUES(?, ?, ?)
            ON CONFLICT(vmid) DO UPDATE SET
                username = COALESCE(excluded.username, username),
                password = COALESCE(excluded.password, password),
                updated_at = CURRENT_TIMESTAMP
            """,
            (vmid, normalized_username, normalized_password),
        )
    return (normalized_username is not None, normalized_password is not None)


def get_vm_credentials(settings: Settings, vmid: int) -> dict[str, str | None]:
    with connect_db(settings) as conn:
        row = conn.execute(
            "SELECT username, password FROM vm_credentials WHERE vmid = ?",
            (vmid,),
        ).fetchone()
    if not row:
        return {"username": None, "password": None}
    return {"username": row["username"], "password": row["password"]}


def delete_vm_credentials(settings: Settings, vmid: int) -> None:
    with connect_db(settings) as conn:
        conn.execute("DELETE FROM vm_credentials WHERE vmid = ?", (vmid,))
