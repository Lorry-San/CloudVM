#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


NAT_CHAIN = "CLOUDVM_CMI_DNAT"
FWD_CHAIN = "CLOUDVM_CMI_FWD"
JUMP_COMMENT = "cloudvm-cmi-entry"


@dataclass
class NatLease:
    vmid: int
    address: str
    ssh_port: int
    port_start: int
    port_end: int


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def ensure_chain(table: str, chain: str) -> None:
    result = run(["iptables", "-w", "-t", table, "-S", chain], check=False)
    if result.returncode != 0:
        run(["iptables", "-w", "-t", table, "-N", chain])
    run(["iptables", "-w", "-t", table, "-F", chain])


def ensure_rule(table: str, chain: str, rule: list[str], insert: bool = False) -> None:
    check_cmd = ["iptables", "-w", "-t", table, "-C", chain, *rule]
    if run(check_cmd, check=False).returncode == 0:
        return
    op = "-I" if insert else "-A"
    run(["iptables", "-w", "-t", table, op, chain, *rule])


def delete_managed_jumps(table: str, chain: str, target: str) -> None:
    result = run(["iptables", "-w", "-t", table, "-S", chain], check=False)
    if result.returncode != 0:
        return
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith(f"-A {chain} "):
            continue
        if f"-j {target}" not in line:
            continue
        rule = line[len(f"-A {chain} ") :].split()
        run(["iptables", "-w", "-t", table, "-D", chain, *rule], check=False)


def load_leases(db_path: Path) -> list[NatLease]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT vmid, address, ssh_port, port_start, port_end FROM nat_leases ORDER BY address"
        ).fetchall()
    finally:
        conn.close()
    return [
        NatLease(
            vmid=int(row["vmid"]),
            address=str(row["address"]),
            ssh_port=int(row["ssh_port"]),
            port_start=int(row["port_start"]),
            port_end=int(row["port_end"]),
        )
        for row in rows
    ]


def build_prerouting_jump(ingress: str, public_ip: str | None) -> list[str]:
    rule = [
        "-i",
        ingress,
        "-m",
        "comment",
        "--comment",
        JUMP_COMMENT,
    ]
    if public_ip:
        rule.extend(["-d", public_ip])
    rule.extend(["-j", NAT_CHAIN])
    return rule


def apply_dnat_rules(leases: list[NatLease]) -> None:
    ensure_chain("nat", NAT_CHAIN)
    for lease in leases:
        ssh_rule = [
            "-p",
            "tcp",
            "--dport",
            str(lease.ssh_port),
            "-j",
            "DNAT",
            "--to-destination",
            f"{lease.address}:22",
        ]
        run(["iptables", "-w", "-t", "nat", "-A", NAT_CHAIN, *ssh_rule])

        for port in range(lease.ssh_port + 1, lease.port_end + 1):
            destination = f"{lease.address}:{port}"
            tcp_rule = [
                "-p",
                "tcp",
                "--dport",
                str(port),
                "-j",
                "DNAT",
                "--to-destination",
                destination,
            ]
            udp_rule = [
                "-p",
                "udp",
                "--dport",
                str(port),
                "-j",
                "DNAT",
                "--to-destination",
                destination,
            ]
            run(["iptables", "-w", "-t", "nat", "-A", NAT_CHAIN, *tcp_rule])
            run(["iptables", "-w", "-t", "nat", "-A", NAT_CHAIN, *udp_rule])


def apply_forward_rule(ingress: str, nat_bridge: str, nat_network: str) -> None:
    ensure_chain("filter", FWD_CHAIN)
    rule = [
        "-i",
        ingress,
        "-o",
        nat_bridge,
        "-d",
        nat_network,
        "-m",
        "conntrack",
        "--ctstate",
        "NEW,ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    ]
    run(["iptables", "-w", "-A", FWD_CHAIN, *rule])
    ensure_rule(
        "filter",
        "FORWARD",
        ["-i", ingress, "-o", nat_bridge, "-j", FWD_CHAIN],
        insert=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rewrite CloudVM NAT DNAT rules for a dedicated ingress."
    )
    parser.add_argument(
        "--db-path",
        default="/opt/CloudVM/data/platform.db",
        help="Path to CloudVM sqlite database.",
    )
    parser.add_argument(
        "--ingress-interface",
        required=True,
        help="Ingress interface for the dedicated CMI path, for example vmbr0.",
    )
    parser.add_argument(
        "--public-ip",
        default="",
        help="Optional public IPv4 to match with -d. Leave empty to match all traffic on the ingress interface.",
    )
    parser.add_argument(
        "--nat-bridge",
        default="nat0",
        help="CloudVM NAT bridge name.",
    )
    parser.add_argument(
        "--nat-network",
        default="192.168.0.0/24",
        help="CloudVM NAT network CIDR.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    try:
        ipaddress.ip_network(args.nat_network, strict=False)
        public_ip = args.public_ip.strip()
        if public_ip:
            ipaddress.ip_address(public_ip)
    except ValueError as exc:
        print(f"invalid IP/CIDR: {exc}", file=sys.stderr)
        return 1

    leases = load_leases(db_path)
    if not leases:
        print("no nat_leases found, nothing to rewrite")
        return 0

    delete_managed_jumps("nat", "PREROUTING", NAT_CHAIN)
    delete_managed_jumps("filter", "FORWARD", FWD_CHAIN)
    ensure_rule(
        "nat",
        "PREROUTING",
        build_prerouting_jump(args.ingress_interface, public_ip),
        insert=True,
    )
    apply_dnat_rules(leases)
    apply_forward_rule(args.ingress_interface, args.nat_bridge, args.nat_network)

    print(
        f"rewrote {len(leases)} NAT lease(s) for ingress {args.ingress_interface}"
        + (f" and public IP {public_ip}" if public_ip else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
