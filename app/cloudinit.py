from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path

from app.config import Settings
from app.schemas import IpLease


def needs_onlink_network_config(lease: IpLease) -> bool:
    address = ip_address(lease.address)
    gateway = ip_address(lease.gateway)
    return address.version == 4 and gateway.version == 4 and lease.cidr == 32


def render_network_config(lease: IpLease, interface: str = "eth0") -> str:
    nameservers = [lease.nameserver] if lease.nameserver else ["8.8.8.8"]
    nameserver_lines = "\n".join(f"                - {item}" for item in nameservers)
    return f"""version: 2
ethernets:
    {interface}:
        dhcp4: false
        dhcp6: false
        addresses:
            - {lease.address}/{lease.cidr}
        routes:
            - to: {lease.gateway}/32
              scope: link
            - to: 0.0.0.0/0
              via: {lease.gateway}
              on-link: true
        nameservers:
            addresses:
{nameserver_lines}
"""


def write_network_snippet(settings: Settings, vmid: int, lease: IpLease) -> str:
    snippet_dir = Path(settings.snippet_dir)
    snippet_dir.mkdir(parents=True, exist_ok=True)
    filename = f"vm-{vmid}-network.yaml"
    path = snippet_dir / filename
    path.write_text(render_network_config(lease), encoding="utf-8")
    return f"{settings.snippet_storage}:snippets/{filename}"
