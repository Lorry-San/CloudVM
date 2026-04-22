from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def bytes_to_gb(value: int | float) -> float:
    return round(float(value) / 1_000_000_000, 6)


def read_proc_net_dev() -> list[dict[str, Any]]:
    path = Path("/proc/net/dev")
    if not path.exists():
        return []
    interfaces: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[2:]:
        if ":" not in line:
            continue
        name, raw_values = line.split(":", 1)
        values = raw_values.split()
        if len(values) < 16:
            continue
        rx_bytes = int(values[0])
        tx_bytes = int(values[8])
        interfaces.append(
            {
                "name": name.strip(),
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "rx_gb": bytes_to_gb(rx_bytes),
                "tx_gb": bytes_to_gb(tx_bytes),
            }
        )
    return interfaces


def vm_tap_traffic(vmid: int) -> dict[str, Any]:
    prefix = f"tap{vmid}i"
    interfaces = [
        item for item in read_proc_net_dev() if str(item["name"]).startswith(prefix)
    ]
    rx_bytes = sum(int(item["rx_bytes"]) for item in interfaces)
    tx_bytes = sum(int(item["tx_bytes"]) for item in interfaces)
    return {
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
        "total_bytes": rx_bytes + tx_bytes,
        "rx_gb": bytes_to_gb(rx_bytes),
        "tx_gb": bytes_to_gb(tx_bytes),
        "total_gb": bytes_to_gb(rx_bytes + tx_bytes),
        "interfaces": interfaces,
    }


def is_whole_disk(name: str) -> bool:
    if name.startswith(("loop", "ram", "dm-", "md")):
        return False
    if re.match(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+)$", name):
        return True
    if re.match(r"^nvme\d+n\d+$", name):
        return True
    return False


def read_disk_io() -> dict[str, Any]:
    path = Path("/proc/diskstats")
    if not path.exists():
        return {"read_bytes": 0, "write_bytes": 0, "devices": []}
    devices: list[dict[str, Any]] = []
    total_read = 0
    total_write = 0
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        if not is_whole_disk(name):
            continue
        read_bytes = int(parts[5]) * 512
        write_bytes = int(parts[9]) * 512
        total_read += read_bytes
        total_write += write_bytes
        devices.append(
            {
                "name": name,
                "read_bytes": read_bytes,
                "write_bytes": write_bytes,
                "read_gb": bytes_to_gb(read_bytes),
                "write_gb": bytes_to_gb(write_bytes),
            }
        )
    return {
        "read_bytes": total_read,
        "write_bytes": total_write,
        "read_gb": bytes_to_gb(total_read),
        "write_gb": bytes_to_gb(total_write),
        "devices": devices,
    }


def host_network() -> dict[str, Any]:
    interfaces = [
        item for item in read_proc_net_dev() if item["name"] != "lo"
    ]
    rx_bytes = sum(int(item["rx_bytes"]) for item in interfaces)
    tx_bytes = sum(int(item["tx_bytes"]) for item in interfaces)
    return {
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
        "total_bytes": rx_bytes + tx_bytes,
        "rx_gb": bytes_to_gb(rx_bytes),
        "tx_gb": bytes_to_gb(tx_bytes),
        "total_gb": bytes_to_gb(rx_bytes + tx_bytes),
        "interfaces": interfaces,
    }


def normalize_vm_status(
    vmid: int,
    raw: dict[str, Any],
    traffic: dict[str, Any],
    node: str | None = None,
) -> dict[str, Any]:
    memory_used = int(raw.get("mem") or 0)
    memory_total = int(raw.get("maxmem") or 0)
    disk_used = int(raw.get("disk") or 0)
    disk_total = int(raw.get("maxdisk") or 0)
    netin = int(raw.get("netin") or 0)
    netout = int(raw.get("netout") or 0)
    diskread = int(raw.get("diskread") or 0)
    diskwrite = int(raw.get("diskwrite") or 0)
    return {
        "scope": "vm",
        "vmid": vmid,
        "node": node,
        "name": raw.get("name"),
        "status": raw.get("status"),
        "uptime": raw.get("uptime"),
        "cpu": {
            "usage": raw.get("cpu"),
            "cpus": raw.get("cpus"),
        },
        "memory": {
            "used_bytes": memory_used,
            "total_bytes": memory_total,
            "used_gb": bytes_to_gb(memory_used),
            "total_gb": bytes_to_gb(memory_total),
        },
        "disk": {
            "used_bytes": disk_used,
            "total_bytes": disk_total,
            "used_gb": bytes_to_gb(disk_used),
            "total_gb": bytes_to_gb(disk_total),
        },
        "io": {
            "read_bytes": diskread,
            "write_bytes": diskwrite,
            "read_gb": bytes_to_gb(diskread),
            "write_gb": bytes_to_gb(diskwrite),
        },
        "network": {
            "netin_bytes": netin,
            "netout_bytes": netout,
            "netin_gb": bytes_to_gb(netin),
            "netout_gb": bytes_to_gb(netout),
            "tap_traffic": traffic,
        },
        "raw": raw,
    }


def normalize_node_status(node: str, raw: dict[str, Any]) -> dict[str, Any]:
    memory = raw.get("memory") or {}
    rootfs = raw.get("rootfs") or {}
    return {
        "scope": "host",
        "node": node,
        "status": raw.get("status", "online"),
        "uptime": raw.get("uptime"),
        "cpu": {
            "usage": raw.get("cpu"),
            "iowait": raw.get("wait"),
            "loadavg": raw.get("loadavg"),
            "kversion": raw.get("kversion"),
        },
        "memory": {
            "used_bytes": int(memory.get("used") or 0),
            "total_bytes": int(memory.get("total") or 0),
            "used_gb": bytes_to_gb(memory.get("used") or 0),
            "total_gb": bytes_to_gb(memory.get("total") or 0),
        },
        "disk": {
            "used_bytes": int(rootfs.get("used") or 0),
            "total_bytes": int(rootfs.get("total") or 0),
            "used_gb": bytes_to_gb(rootfs.get("used") or 0),
            "total_gb": bytes_to_gb(rootfs.get("total") or 0),
        },
        "io": read_disk_io(),
        "network": host_network(),
        "raw": raw,
    }
