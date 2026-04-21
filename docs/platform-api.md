# Platform API plan

This project is being migrated from a single Telegram traffic monitor script into a
Proxmox VE VM management platform.

## Security model

Users must never receive the Proxmox VE backend URL, PVE login page, API token,
VNC ticket, or terminal ticket.

The browser talks only to PVETrafficManager:

- REST API: `https://panel.example.com/api/v1/...`
- VNC WebSocket: `wss://panel.example.com/ws/vnc/{vmid}`
- xterm.js WebSocket: `wss://panel.example.com/ws/xterm/{vmid}`

PVETrafficManager talks to Proxmox VE privately:

- `POST /nodes/{node}/qemu/{vmid}/vncproxy`
- `GET /nodes/{node}/qemu/{vmid}/vncwebsocket`
- `POST /nodes/{node}/qemu/{vmid}/termproxy`

The platform should be placed behind HTTPS reverse proxy, such as Nginx or Caddy.
Only the platform domain should be exposed publicly. Proxmox VE port `8006`
should stay private or firewall-restricted.

## Current API skeleton

All REST endpoints require:

```http
X-API-Token: <PLATFORM_API_TOKEN>
```

VM lifecycle endpoints:

- `POST /api/v1/ip-pool`
- `GET /api/v1/ip-pool`
- `POST /api/v1/ip-pool/{address}/release`
- `GET /api/v1/images`
- `POST /api/v1/vms`
- `GET /api/v1/vms/{vmid}`
- `POST /api/v1/vms/{vmid}/pause`
- `POST /api/v1/vms/{vmid}/resume`
- `DELETE /api/v1/vms/{vmid}`
- `POST /api/v1/vms/{vmid}/expiration`

Console endpoints:

- `POST /api/v1/consoles/vnc/{vmid}`
- `POST /api/v1/consoles/xterm/{vmid}`
- `WS /ws/vnc/{vmid}`
- `WS /ws/xterm/{vmid}`

## Next implementation steps

1. Add database tables for users, VMs, expiration policy, traffic quota, and audit log.
2. Persist VM creation metadata after PVE clone/create task is accepted.
3. Add a scheduler that pauses or deletes expired VMs.
4. Add noVNC and xterm.js frontend pages that connect only to platform WebSockets.
5. Add per-user authorization so users can open only their own VMs.
6. Add task polling for Proxmox UPID completion and failure reporting.

## IP pool

Import allocatable IP addresses before creating VMs. The platform stores them in
SQLite and marks an address as allocated when a VM is created.

Example: import explicit addresses:

```json
{
  "addresses": ["203.0.113.10", "203.0.113.11"],
  "cidr": 24,
  "gateway": "203.0.113.1",
  "nameserver": "1.1.1.1",
  "bridge": "vmbr0",
  "note": "public IPv4 block"
}
```

Example: import a CIDR range:

```json
{
  "range": "203.0.113.16/29",
  "cidr": 29,
  "gateway": "203.0.113.17",
  "nameserver": "1.1.1.1",
  "bridge": "vmbr0"
}
```

When creating a VM, `allocate_ip` defaults to `true`. If `ip_config` is omitted,
the backend takes the first available IP and writes this cloud-init value:

```text
ip=<address>/<cidr>,gw=<gateway>
```

Set `allocate_ip` to `false` or pass `ip_config` manually to bypass the pool.

The platform currently uses PVE native cloud-init `ipconfig0` for IP assignment.
This keeps the IP visible and editable in the PVE Cloud-Init panel. For `/32`
public IPs with off-subnet gateways, the guest image must be able to handle
`ip=<address>/32,gw=<gateway>` or include a template-side network hook.

Release an IP:

```text
POST /api/v1/ip-pool/203.0.113.10/release
```

## Image templates and cloud-init boot flow

Define public image names in `.env` and map them to PVE template VMIDs:

```env
PVE_IMAGE_TEMPLATES={"debian-12":9000,"ubuntu-24.04":9001,"windows-2022":9002}
```

The frontend should send `image`, not the raw template VMID. This keeps the user
away from Proxmox internals and lets the platform control which templates are
available.

Example create request:

```json
{
  "name": "vm-demo-001",
  "image": "debian-12",
  "cores": 2,
  "memory_mb": 2048,
  "disk_gb": 40,
  "storage": "local-lvm",
  "network": {
    "bridge": "vmbr0",
    "model": "virtio",
    "rate": 100,
    "vlan_tag": null,
    "firewall": true
  },
  "boot_order": "scsi0;ide2;net0",
  "ci_user": "debian",
  "ci_password": "change-after-login",
  "ssh_keys": "ssh-ed25519 AAAA...",
  "ip_config": "ip=dhcp",
  "nameserver": "1.1.1.1",
  "start": true
}
```

The backend flow is:

1. Resolve `image` to a PVE template VMID.
2. Clone the template.
3. Wait for the clone UPID to finish.
4. Apply CPU, memory, network model, bridge, VLAN, and rate config.
5. Resize `scsi0` when `disk_gb` is provided.
6. Apply cloud-init and boot order config.
7. Start the VM.

Deleting a VM through `DELETE /api/v1/vms/{vmid}` stops the VM, deletes it from
PVE, releases the IP pool lease attached to that VMID, and removes the generated
network snippet.

For Linux cloud images, make sure the PVE template already has a cloud-init drive
configured. Windows images usually require a different initialization path; do not
assume Linux cloud-init fields will work for every Windows template.
