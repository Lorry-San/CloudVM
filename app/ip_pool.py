from __future__ import annotations

from ipaddress import ip_address, ip_network

from app.config import Settings
from app.db import connect_db
from app.schemas import IpLease, IpPoolAddRequest, IpPoolAddress


def _row_to_address(row) -> IpPoolAddress:
    return IpPoolAddress(
        address=row["address"],
        cidr=row["cidr"],
        gateway=row["gateway"],
        nameserver=row["nameserver"],
        bridge=row["bridge"],
        status=row["status"],
        vmid=row["vmid"],
        note=row["note"],
    )


def add_ip_addresses(settings: Settings, req: IpPoolAddRequest) -> list[IpPoolAddress]:
    addresses = [str(ip_address(item)) for item in req.addresses]
    if req.range:
        network = ip_network(req.range, strict=False)
        addresses.extend(str(item) for item in network.hosts())

    unique_addresses = sorted(set(addresses), key=lambda item: int(ip_address(item)))
    with connect_db(settings) as conn:
        for address in unique_addresses:
            conn.execute(
                """
                INSERT INTO ip_pool(address, cidr, gateway, nameserver, bridge, note)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    cidr=excluded.cidr,
                    gateway=excluded.gateway,
                    nameserver=excluded.nameserver,
                    bridge=excluded.bridge,
                    note=excluded.note
                WHERE ip_pool.status = 'available'
                """,
                (
                    address,
                    req.cidr,
                    str(ip_address(req.gateway)),
                    req.nameserver,
                    req.bridge,
                    req.note,
                ),
            )
        rows = conn.execute(
            """
            SELECT * FROM ip_pool
            WHERE address IN ({})
            ORDER BY address
            """.format(",".join("?" for _ in unique_addresses)),
            unique_addresses,
        ).fetchall()
    return [_row_to_address(row) for row in rows]


def list_ip_addresses(
    settings: Settings,
    status: str | None = None,
) -> list[IpPoolAddress]:
    with connect_db(settings) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM ip_pool WHERE status = ? ORDER BY address",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ip_pool ORDER BY address").fetchall()
    return [_row_to_address(row) for row in rows]


def list_ip_addresses_by_vmid(settings: Settings, vmid: int) -> list[IpPoolAddress]:
    with connect_db(settings) as conn:
        rows = conn.execute(
            "SELECT * FROM ip_pool WHERE vmid = ? ORDER BY address",
            (vmid,),
        ).fetchall()
    return [_row_to_address(row) for row in rows]


def allocate_ip(settings: Settings, vmid: int | None = None) -> IpLease | None:
    with connect_db(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM ip_pool
            WHERE status = 'available'
            ORDER BY address
            LIMIT 1
            """,
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE ip_pool
            SET status = 'allocated',
                vmid = ?,
                allocated_at = CURRENT_TIMESTAMP,
                released_at = NULL
            WHERE address = ?
            """,
            (vmid, row["address"]),
        )
        updated = conn.execute(
            "SELECT * FROM ip_pool WHERE address = ?",
            (row["address"],),
        ).fetchone()
    return IpLease(
        address=updated["address"],
        cidr=updated["cidr"],
        gateway=updated["gateway"],
        nameserver=updated["nameserver"],
        bridge=updated["bridge"],
        ip_config=f"ip={updated['address']}/{updated['cidr']},gw={updated['gateway']}",
    )


def release_ip(settings: Settings, address: str) -> IpPoolAddress | None:
    normalized = str(ip_address(address))
    with connect_db(settings) as conn:
        row = conn.execute(
            "SELECT * FROM ip_pool WHERE address = ?",
            (normalized,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE ip_pool
            SET status = 'available',
                vmid = NULL,
                released_at = CURRENT_TIMESTAMP
            WHERE address = ?
            """,
            (normalized,),
        )
        updated = conn.execute(
            "SELECT * FROM ip_pool WHERE address = ?",
            (normalized,),
        ).fetchone()
    return _row_to_address(updated)


def release_ip_by_vmid(settings: Settings, vmid: int) -> IpPoolAddress | None:
    with connect_db(settings) as conn:
        row = conn.execute(
            "SELECT * FROM ip_pool WHERE vmid = ? AND status = 'allocated'",
            (vmid,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE ip_pool
            SET status = 'available',
                vmid = NULL,
                released_at = CURRENT_TIMESTAMP
            WHERE address = ?
            """,
            (row["address"],),
        )
        updated = conn.execute(
            "SELECT * FROM ip_pool WHERE address = ?",
            (row["address"],),
        ).fetchone()
    return _row_to_address(updated)


def bind_allocated_ip(settings: Settings, address: str, vmid: int) -> None:
    normalized = str(ip_address(address))
    with connect_db(settings) as conn:
        conn.execute(
            "UPDATE ip_pool SET vmid = ? WHERE address = ? AND status = 'allocated'",
            (vmid, normalized),
        )
