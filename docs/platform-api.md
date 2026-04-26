# CloudVM API Docs

这份文档按当前代码整理，基于：

- `app/main.py`
- `app/schemas.py`
- `app/config.py`

默认情况下：

- FastAPI `docs/redoc/openapi` 关闭
- 所有业务接口都需要请求头：

```http
X-API-Token: <PLATFORM_API_TOKEN>
```

## Base URLs

- 面板首页：`GET /`
- 面板别名：`GET /dashboard`
- 单台 VM 页面：`GET /vm/{vmid}`
- 健康检查：`GET /health`
- Token 校验：`GET /api/v1/auth/check`

## Security Model

浏览器只应该访问 CloudVM，不应该直接拿到：

- PVE 后台地址
- PVE API token
- PVE VNC ticket
- PVE xterm ticket

控制台相关流程：

- 先调用平台接口拿短期 console token
- 浏览器访问 `/console?token=...`
- 页面再连平台自己的 WebSocket
- 平台在服务端代理到 PVE

## Image Templates

镜像名来自 `.env`：

```env
PVE_IMAGE_TEMPLATES={"debian-12":9000,"ubuntu-24.04":9001}
```

对外暴露的是 `image`，不是原始模板 VMID。

### List images

```http
GET /api/v1/images
GET /api/v1/reinstall/images
```

响应示例：

```json
[
  {
    "image": "debian-12",
    "template_vmid": 9000
  }
]
```

## IP Pool

### Add public IPs

```http
POST /api/v1/ip-pool
```

显式导入：

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

按 CIDR 导入：

```json
{
  "range": "203.0.113.16/29",
  "cidr": 29,
  "gateway": "203.0.113.17",
  "nameserver": "1.1.1.1",
  "bridge": "vmbr0"
}
```

### List IP pool

```http
GET /api/v1/ip-pool
GET /api/v1/ip-pool?status=available
```

### Release one public IP

```http
POST /api/v1/ip-pool/{address}/release
```

### List IPs bound to one VM

```http
GET /api/v1/vms/{vmid}/ips
```

如果该 VM 是 NAT 模式，这里还会附带 NAT lease 信息，`note` 里会写出 SSH 端口和端口段。

## VM Create

```http
POST /api/v1/vms
```

`VmCreateRequest` 主要字段：

```json
{
  "vmid": 101,
  "name": "vm-demo-001",
  "image": "debian-12",
  "template_vmid": null,
  "cores": 2,
  "memory_mb": 2048,
  "disk_gb": 40,
  "storage": "local",
  "bridge": "vmbr0",
  "network": {
    "mode": "public",
    "bridge": "vmbr0",
    "model": "virtio",
    "rate": 100,
    "vlan_tag": null,
    "firewall": true
  },
  "boot_order": "scsi0;ide2;net0",
  "ci_user": "root",
  "ci_password": "ChangeMe123!",
  "ssh_keys": "ssh-ed25519 AAAA...",
  "ip_config": "ip=dhcp",
  "nameserver": "8.8.8.8",
  "searchdomain": null,
  "allocate_ip": true,
  "owner": null,
  "expires_at": null,
  "traffic_limit_gb": 50,
  "traffic_reset_day": 1,
  "traffic_reset_hour": 0,
  "traffic_reset_timezone": "Asia/Shanghai",
  "start": true
}
```

说明：

- `image` 和 `template_vmid` 二选一即可
- `network.mode`:
  - `public`：走公网桥接，默认 `vmbr0`
  - `nat`：走 NAT，自动挂 `nat0`
- `allocate_ip=true` 且未提供 `ip_config` 时：
  - 公网模式会从 IP 池自动分配
  - NAT 模式会自动分配 NAT 地址和端口
- 若未传 `ci_user`，平台保存凭据时默认用户名为 `root`

创建成功响应：

```json
{
  "vmid": 101,
  "task": "UPID:pve01:...",
  "start_task": "UPID:pve01:...",
  "allocated_ip": "203.0.113.10",
  "nat_ip": null,
  "ssh_port": null,
  "port_range_start": null,
  "port_range_end": null,
  "network_mode": "public",
  "released_ip": null,
  "status": "accepted"
}
```

NAT 模式时：

```json
{
  "vmid": 101,
  "nat_ip": "192.168.0.1",
  "ssh_port": 30001,
  "port_range_start": 30001,
  "port_range_end": 30010,
  "network_mode": "nat",
  "status": "accepted"
}
```

## VM Query

### List VMs

```http
GET /api/v1/vms
```

返回 PVE VM 列表，并补充：

- `network_mode`
- NAT lease
- 流量配额汇总

### Get one VM

```http
GET /api/v1/vms/{vmid}
```

返回单台 VM 的状态信息，并补充：

- NAT 信息
- 流量计费信息
- 平台侧记录的凭据是否存在

## VM Config Update

```http
PUT /api/v1/vms/{vmid}/config
```

请求体：

```json
{
  "cores": 4,
  "memory_mb": 4096,
  "network_rate": 200,
  "reboot": true
}
```

说明：

- `cores`：修改 CPU
- `memory_mb`：修改内存
- `network_rate`：修改 `net0` 的 PVE 速率限制
- `reboot=true` 且 VM 当前运行中时，会自动触发一次重启

## VM Password Update

```http
POST /api/v1/vms/{vmid}/password
```

Request body:

```json
{
  "username": "root",
  "password": "NewPass123!",
  "reboot": true
}
```

Notes:
- This updates `ciuser` and `cipassword` on the VM config.
- The backend runs `qm cloudinit update <vmid>` after writing the config.
- If the VM is running and `reboot=true`, the platform triggers a reboot so the new password can apply more reliably.
- The platform also updates the saved credentials record used by the UI and VNC helper.
- This is a cloud-init based password update, not an in-guest `passwd` execution.

Response example:

```json
{
  "vmid": 100,
  "task": "UPID:pve01:...",
  "start_task": "UPID:pve01:...",
  "allocated_ip": null,
  "nat_ip": null,
  "ssh_port": null,
  "port_range_start": null,
  "port_range_end": null,
  "network_mode": null,
  "released_ip": null,
  "status": "password_updated"
}
```

## VM Reinstall

```http
POST /api/v1/vms/{vmid}/reinstall
```

请求体：

```json
{
  "image": "debian-12",
  "template_vmid": null,
  "slot": "virtio0",
  "template_slot": null,
  "storage": "local",
  "disk_size": "40G",
  "ci_user": "root",
  "password": "ChangeMe123!",
  "nameserver": "8.8.8.8",
  "start": true,
  "free_old": true,
  "dry_run": false
}
```

说明：

- `free_old=true`：默认删除旧系统盘
- `start=true`：重装结束后自动开机
- `dry_run=true`：只做检查，不实际执行
- 如果传了 `ci_user` 或 `password`，平台会更新这台 VM 的已保存凭据

响应：

```json
{
  "vmid": 101,
  "task": "reinstall-101-1714020000",
  "status": "reinstall_queued"
}
```

## VM Traffic Config

### Get traffic config

```http
GET /api/v1/vms/{vmid}/traffic
```

### Update traffic config

```http
PUT /api/v1/vms/{vmid}/traffic
```

请求体：

```json
{
  "quota_gb": 50,
  "reset_day": 1,
  "reset_hour": 0,
  "timezone": "Asia/Shanghai",
  "reset_usage": false
}
```

返回字段包括：

- `quota_gb`
- `used_gb`
- `remaining_gb`
- `percent`
- `next_reset_at`
- `baseline_at`

如果流量达到配额，平台在状态采样时会自动对该 VM 执行断网。

## Status and Metrics

### Node / VM status

```http
GET /api/v1/status
GET /api/v1/status?vmid=101
GET /status
GET /status?vmid=101
```

说明：

- 不带 `vmid`：返回宿主机状态
- 带 `vmid`：返回虚拟机状态

宿主机状态大致包含：

- CPU
- 内存
- 根盘
- 磁盘 IO
- 网卡统计
- NAT 总体配置摘要

虚拟机状态大致包含：

- PVE 原始运行状态
- 对应 tap 网卡流量
- NAT lease
- `traffic_billing`

### Metric history

```http
GET /api/v1/metrics/history
GET /api/v1/metrics/history?vmid=101&hours=24
```

说明：

- 不带 `vmid`：看宿主机最近采样
- 带 `vmid`：看单台 VM 最近采样
- `hours` 默认 `24`

### VM task logs

```http
GET /api/v1/vms/{vmid}/tasks
GET /api/v1/vms/{vmid}/tasks?limit=50
```

返回平台侧记录的任务日志，例如：

- create
- delete
- reinstall
- config_update
- traffic_config
- network_disconnect
- network_connect

## VM Actions

### Pause / stop

```http
POST /api/v1/vms/{vmid}/pause
```

当前实现实际调用的是 PVE `stop`，不是 hypervisor suspend。

### Resume / start

```http
POST /api/v1/vms/{vmid}/resume
```

### Disconnect network

```http
POST /api/v1/vms/{vmid}/network/disconnect
```

通过给 `net0` 打 `link_down=1` 实现。

### Connect network

```http
POST /api/v1/vms/{vmid}/network/connect
```

通过移除 `link_down=1` 实现。

### Delete VM

```http
DELETE /api/v1/vms/{vmid}
```

当前流程：

1. 尝试先 stop
2. 调 PVE delete
3. 释放公网 IP lease
4. 若是 NAT VM，删除 NAT 规则并释放 NAT lease
5. 删除生成的网络 snippet
6. 删除平台保存的 VM 凭据

### Expiration

```http
POST /api/v1/vms/{vmid}/expiration
```

请求体：

```json
{
  "expires_at": "2026-05-01T00:00:00Z",
  "action": "pause"
}
```

当前仅返回设置结果，调度和持久化还没真正接上。

## VM Credentials

### Get saved credentials

```http
GET /api/v1/vms/{vmid}/credentials
```

返回平台侧保存的 VM 控制台凭据，供受信任的上游系统如财务系统服务端调用：

```json
{
  "vmid": 101,
  "username": "root",
  "password": "ChangeMe123!",
  "username_saved": true,
  "password_saved": true
}
```

### Update saved credentials

```http
PUT /api/v1/vms/{vmid}/credentials
```

请求体：

```json
{
  "username": "root",
  "password": "ChangeMe123!"
}
```

返回：

```json
{
  "vmid": 101,
  "username_saved": true,
  "password_saved": true
}
```

这些凭据主要用于 VNC 页面上的“粘贴用户名 / 密码”按钮。

## Consoles

### Mint short-lived console token

```http
POST /api/v1/consoles/token
```

请求体：

```json
{
  "vmid": 101,
  "ttl_seconds": 600
}
```

说明：

- `ttl_seconds` 范围 `60-900`
- 实际上限 900 秒
- token 当前保存在内存里，服务重启后会失效

返回：

```json
{
  "token": "short-lived-token",
  "expires_at": "2026-04-25T10:00:00Z",
  "vmid": 101,
  "console_url": "https://panel.example.com/console?token=short-lived-token"
}
```

### VNC session meta

```http
POST /api/v1/consoles/vnc/{vmid}
```

### Xterm session meta

```http
POST /api/v1/consoles/xterm/{vmid}
```

这两个接口返回平台自己的 WebSocket 地址，不会返回 PVE 后端地址。

### Console pages

```http
GET /console?token=<CONSOLE_TOKEN>
GET /console/vnc/{vmid}?token=<CONSOLE_TOKEN>
```

注意：

- 这里用的是 **console token**
- 不是 `PLATFORM_API_TOKEN`

### WebSocket endpoints

```text
WS /ws/vnc?token=<CONSOLE_TOKEN>
WS /ws/vnc/{vmid}?token=<CONSOLE_TOKEN>
WS /ws/xterm/{vmid}?token=<CONSOLE_TOKEN>
```

说明：

- `/ws/vnc`：从 token 里解出 VMID
- `/ws/vnc/{vmid}`：额外校验 token 与 VMID 匹配
- `/ws/xterm/{vmid}`：代理到 PVE 的 `termproxy/vncwebsocket`

## Current Defaults

来自 `app/config.py` 的默认值：

```env
PVE_DEFAULT_STORAGE=local-lvm
PVE_DEFAULT_BRIDGE=vmbr0
PVE_NAT_ENABLED=true
PVE_NAT_BRIDGE=nat0
PVE_NAT_NETWORK=192.168.0.0/24
PVE_NAT_HOST_IP=192.168.0.254
PVE_NAT_PORT_START=30001
PVE_NAT_PORTS_PER_VM=10
PVE_NAT_NAMESERVER=8.8.8.8
```

NAT 端口规则：

- `192.168.0.1` -> `30001-30010`
- `192.168.0.2` -> `30011-30020`
- 每台 VM 第一端口映射 SSH `-> :22`
- 其余端口按“外部端口 = 内部端口”做 DNAT

## Notes

- `/api/v1/status` 与 `/status` 是别名
- `pause` 当前行为是 `stop`
- VNC 页面的账号/密码粘贴按钮依赖平台保存的凭据
- 文档如果再漂，以代码为准：
  - `app/main.py`
  - `app/schemas.py`
