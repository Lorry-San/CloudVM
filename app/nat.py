from __future__ import annotations

import asyncio
from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from urllib.parse import urlparse

from app.config import Settings
from app.db import connect_db
from app.schemas import NatLease


class NatError(RuntimeError):
    pass


def nat_network(settings: Settings) -> IPv4Network:
    network = ip_network(settings.nat_network_cidr, strict=False)
    if not isinstance(network, IPv4Network):
        raise NatError("NAT network must be IPv4")
    if network.prefixlen != 24:
        raise NatError("NAT network must be /24 in this branch")
    return network


def nat_host_address(settings: Settings) -> IPv4Address:
    host_ip = ip_address(settings.nat_host_ip)
    if not isinstance(host_ip, IPv4Address):
        raise NatError("NAT host IP must be IPv4")
    if host_ip not in nat_network(settings):
        raise NatError("NAT host IP is outside NAT network")
    return host_ip


def external_host(settings: Settings) -> str:
    if settings.public_base_url:
        parsed = urlparse(settings.public_base_url)
        if parsed.hostname:
            return parsed.hostname
    parsed = urlparse(settings.pve_host)
    if parsed.hostname:
        return parsed.hostname
    return settings.pve_host


def port_block_for_ip(settings: Settings, address: str) -> tuple[int, int, int]:
    host_octet = int(str(address).split(".")[-1])
    if host_octet < 1 or host_octet > 253:
        raise NatError(f"Unsupported NAT host octet: {host_octet}")
    start = settings.nat_port_start + (host_octet - 1) * settings.nat_ports_per_vm
    end = start + settings.nat_ports_per_vm - 1
    return start, end, start


def _row_to_nat_lease(row) -> NatLease:
    return NatLease(
        vmid=row["vmid"],
        address=row["address"],
        cidr=row["cidr"],
        gateway=row["gateway"],
        nameserver=row["nameserver"],
        bridge=row["bridge"],
        host_ip=row["host_ip"],
        external_host=row["external_host"],
        port_start=row["port_start"],
        port_end=row["port_end"],
        ssh_port=row["ssh_port"],
        ip_config=f"ip={row['address']}/{row['cidr']},gw={row['gateway']}",
    )


def get_nat_lease(settings: Settings, vmid: int) -> NatLease | None:
    with connect_db(settings) as conn:
        row = conn.execute(
            "SELECT * FROM nat_leases WHERE vmid = ?",
            (vmid,),
        ).fetchone()
    return _row_to_nat_lease(row) if row else None


def list_nat_leases(settings: Settings) -> list[NatLease]:
    with connect_db(settings) as conn:
        rows = conn.execute("SELECT * FROM nat_leases ORDER BY address").fetchall()
    return [_row_to_nat_lease(row) for row in rows]


def allocate_nat_lease(settings: Settings, vmid: int) -> NatLease:
    existing = get_nat_lease(settings, vmid)
    if existing:
        return existing

    network = nat_network(settings)
    host_ip = nat_host_address(settings)
    ext_host = external_host(settings)

    with connect_db(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")
        used = {
            row["address"]
            for row in conn.execute("SELECT address FROM nat_leases").fetchall()
        }
        selected: IPv4Address | None = None
        for candidate in network.hosts():
            if candidate == host_ip:
                continue
            if str(candidate) not in used:
                selected = candidate
                break
        if selected is None:
            raise NatError("No available NAT IP address")
        port_start, port_end, ssh_port = port_block_for_ip(settings, str(selected))
        conn.execute(
            """
            INSERT INTO nat_leases(
                vmid, address, cidr, gateway, nameserver, bridge, host_ip,
                external_host, port_start, port_end, ssh_port
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vmid,
                str(selected),
                network.prefixlen,
                str(host_ip),
                settings.nat_nameserver,
                settings.nat_bridge,
                str(host_ip),
                ext_host,
                port_start,
                port_end,
                ssh_port,
            ),
        )
        row = conn.execute("SELECT * FROM nat_leases WHERE vmid = ?", (vmid,)).fetchone()
    return _row_to_nat_lease(row)


def release_nat_lease(settings: Settings, vmid: int) -> NatLease | None:
    lease = get_nat_lease(settings, vmid)
    if not lease:
        return None
    with connect_db(settings) as conn:
        conn.execute("DELETE FROM nat_leases WHERE vmid = ?", (vmid,))
    return lease


async def ensure_nat_ready(settings: Settings) -> None:
    if not settings.nat_enabled:
        raise NatError("NAT mode is disabled")
    network = nat_network(settings)
    host_ip = nat_host_address(settings)
    bridge = settings.nat_bridge
    uplink = await detect_uplink_interface(settings)

    await ensure_bridge(bridge)
    await ensure_address(bridge, f"{host_ip}/{network.prefixlen}")
    await run_cmd(["ip", "link", "set", "dev", bridge, "up"])
    await run_cmd(["sysctl", "-w", "net.ipv4.ip_forward=1"])

    await ensure_iptables_rule(
        ["iptables", "-w", "-C", "FORWARD", "-i", bridge, "-o", uplink, "-s", str(network), "-j", "ACCEPT"],
        ["iptables", "-w", "-A", "FORWARD", "-i", bridge, "-o", uplink, "-s", str(network), "-j", "ACCEPT"],
    )
    await ensure_iptables_rule(
        ["iptables", "-w", "-C", "FORWARD", "-i", uplink, "-o", bridge, "-d", str(network), "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ["iptables", "-w", "-A", "FORWARD", "-i", uplink, "-o", bridge, "-d", str(network), "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    )
    await ensure_iptables_rule(
        ["iptables", "-w", "-t", "nat", "-C", "POSTROUTING", "-s", str(network), "-o", uplink, "-j", "MASQUERADE"],
        ["iptables", "-w", "-t", "nat", "-A", "POSTROUTING", "-s", str(network), "-o", uplink, "-j", "MASQUERADE"],
    )


async def ensure_nat_rules(settings: Settings, lease: NatLease) -> None:
    uplink = await detect_uplink_interface(settings)
    await ensure_nat_ready(settings)
    await ensure_iptables_rule(
        ["iptables", "-w", "-t", "nat", "-C", "PREROUTING", "-i", uplink, "-p", "tcp", "--dport", str(lease.ssh_port), "-j", "DNAT", "--to-destination", f"{lease.address}:22"],
        ["iptables", "-w", "-t", "nat", "-A", "PREROUTING", "-i", uplink, "-p", "tcp", "--dport", str(lease.ssh_port), "-j", "DNAT", "--to-destination", f"{lease.address}:22"],
    )
    for port in range(lease.ssh_port + 1, lease.port_end + 1):
        destination = f"{lease.address}:{port}"
        await ensure_iptables_rule(
            ["iptables", "-w", "-t", "nat", "-C", "PREROUTING", "-i", uplink, "-p", "tcp", "--dport", str(port), "-j", "DNAT", "--to-destination", destination],
            ["iptables", "-w", "-t", "nat", "-A", "PREROUTING", "-i", uplink, "-p", "tcp", "--dport", str(port), "-j", "DNAT", "--to-destination", destination],
        )
        await ensure_iptables_rule(
            ["iptables", "-w", "-t", "nat", "-C", "PREROUTING", "-i", uplink, "-p", "udp", "--dport", str(port), "-j", "DNAT", "--to-destination", destination],
            ["iptables", "-w", "-t", "nat", "-A", "PREROUTING", "-i", uplink, "-p", "udp", "--dport", str(port), "-j", "DNAT", "--to-destination", destination],
        )


async def remove_nat_rules(settings: Settings, lease: NatLease) -> None:
    uplink = await detect_uplink_interface(settings)
    await delete_iptables_rule(
        ["iptables", "-w", "-t", "nat", "-D", "PREROUTING", "-i", uplink, "-p", "tcp", "--dport", str(lease.ssh_port), "-j", "DNAT", "--to-destination", f"{lease.address}:22"],
    )
    for port in range(lease.ssh_port + 1, lease.port_end + 1):
        destination = f"{lease.address}:{port}"
        await delete_iptables_rule(
            ["iptables", "-w", "-t", "nat", "-D", "PREROUTING", "-i", uplink, "-p", "tcp", "--dport", str(port), "-j", "DNAT", "--to-destination", destination],
        )
        await delete_iptables_rule(
            ["iptables", "-w", "-t", "nat", "-D", "PREROUTING", "-i", uplink, "-p", "udp", "--dport", str(port), "-j", "DNAT", "--to-destination", destination],
        )


async def ensure_bridge(bridge: str) -> None:
    result = await run_cmd(["ip", "link", "show", "dev", bridge], check=False)
    if result["returncode"] != 0:
        await run_cmd(["ip", "link", "add", bridge, "type", "bridge"])


async def ensure_address(device: str, cidr: str) -> None:
    result = await run_cmd(["ip", "-4", "addr", "show", "dev", device], check=False)
    if cidr not in result["stdout"]:
        await run_cmd(["ip", "addr", "add", cidr, "dev", device])


async def detect_uplink_interface(settings: Settings) -> str:
    if settings.nat_uplink_interface:
        return settings.nat_uplink_interface
    result = await run_cmd(["ip", "route", "show", "default"])
    for line in result["stdout"].splitlines():
        parts = line.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    raise NatError("Unable to detect NAT uplink interface")


async def ensure_iptables_rule(check_cmd: list[str], add_cmd: list[str]) -> None:
    result = await run_cmd(check_cmd, check=False)
    if result["returncode"] != 0:
        await run_cmd(add_cmd)


async def delete_iptables_rule(delete_cmd: list[str]) -> None:
    while True:
        result = await run_cmd(delete_cmd, check=False)
        if result["returncode"] != 0:
            return


async def run_cmd(args: list[str], *, check: bool = True) -> dict[str, object]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace").strip()
    if check and proc.returncode != 0:
        raise NatError(f"command failed ({proc.returncode}): {' '.join(args)}\n{output}")
    return {"returncode": proc.returncode, "stdout": output}
