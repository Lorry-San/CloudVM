# PVETrafficManager `beta-2.0.0-nat`

这是 `PVETrafficManager` 的 NAT 分支版本。

这个分支在原有 PVE VM 管理平台基础上，增加了一套默认可用的 NAT 网络模式：

- 自动使用 `nat0` 作为 NAT 网桥
- 默认 NAT 网段 `192.168.0.0/24`
- 宿主机 NAT 地址 `192.168.0.254`
- 默认起始端口 `30001`
- 每台 VM 占用连续 `10` 个端口
- 第一个端口固定映射为 SSH

示例：

- `192.168.0.1`
  - SSH: `30001 -> 22`
  - 其余端口：`30002-30010 -> 192.168.0.1:30002-30010`
- `192.168.0.2`
  - SSH: `30011 -> 22`
  - 其余端口：`30012-30020 -> 192.168.0.2:30012-30020`

## 功能

- VM 新建、开关机、重启、删除
- 镜像模板克隆
- Cloud-Init 用户名、密码、DNS 下发
- VM 重装系统
- 流量统计和流量额度
- VNC 控制台
- NAT IP 自动分配
- NAT DNAT/SNAT 规则自动下发
- 服务重启后自动恢复 NAT 规则
- 如果检测到出口 IP 位于中国大陆，安装脚本会自动切换 Debian 和 Proxmox 到清华镜像

## 运行要求

- Proxmox VE 宿主机
- Debian 12/13 或 PVE 8/9 环境
- Python 3.11+
- `iptables`
- `git`
- `venv`

## 关键说明

### 1. NAT 网桥是运行时创建的

这个分支不会强依赖你手工提前把 `nat0` 写进 `/etc/network/interfaces`。

服务启动时会自动尝试：

- 创建 `nat0`
- 给 `nat0` 配置宿主机地址
- 启用 `net.ipv4.ip_forward`
- 写入 `iptables` 的 `FORWARD` / `MASQUERADE` / `DNAT`

### 2. 推荐显式指定外网出口网卡

如果宿主机默认路由比较复杂，建议在 `.env` 里显式设置：

```env
PVE_NAT_UPLINK_INTERFACE=vmbr0
```

或者填你的真实外网出口设备，例如：

```env
PVE_NAT_UPLINK_INTERFACE=ens18
```

### 3. NAT 和公网桥接可以并存

新建 VM 时前端默认使用 `NAT` 模式，但仍保留原有 `public` 模式：

- `nat`: 自动分配内网地址和端口映射
- `public`: 继续走原先公网桥接/IP 池逻辑

## 配置项

参考 `.env.example`。

NAT 相关配置：

```env
PVE_NAT_ENABLED=true
PVE_NAT_BRIDGE=nat0
PVE_NAT_NETWORK=192.168.0.0/24
PVE_NAT_HOST_IP=192.168.0.254
PVE_NAT_PORT_START=30001
PVE_NAT_PORTS_PER_VM=10
PVE_NAT_NAMESERVER=8.8.8.8
PVE_NAT_UPLINK_INTERFACE=
```

PVE 基础配置示例：

```env
PLATFORM_API_TOKEN=change-this-platform-token
PUBLIC_BASE_URL=https://panel.example.com
PLATFORM_DB_PATH=./data/platform.db

PVE_HOST=https://127.0.0.1:8006
PVE_NODE=pve01
PVE_VERIFY_SSL=false
PVE_API_TOKEN_ID=root@pam!platform
PVE_API_TOKEN_SECRET=change-this-pve-api-token-secret

PVE_DEFAULT_STORAGE=local
PVE_DEFAULT_BRIDGE=vmbr0
PVE_IMAGE_TEMPLATES={"debian-12":9000}
```

## 部署教程

下面是最直接的部署方式。

### 一键脚本

仓库里附带了完整脚本：

[`scripts/install-pve-nat-branch.sh`](G:/Codex/PVETrafficManager/scripts/install-pve-nat-branch.sh)

它分两阶段：

1. `phase1`：配置官方 Proxmox 仓库并安装 `proxmox-default-kernel`
2. `phase2`：安装 `proxmox-ve` 和 NAT 分支平台

用法：

```bash
bash scripts/install-pve-nat-branch.sh phase1
reboot
bash scripts/install-pve-nat-branch.sh phase2
```

### 1. 拉取代码

```bash
cd /opt
git clone -b beta-2.0.0-nat https://github.com/Lorry-San/PVETrafficManager.git
cd /opt/PVETrafficManager
```

如果目录已经存在：

```bash
cd /opt/PVETrafficManager
git fetch origin
git checkout beta-2.0.0-nat
git pull origin beta-2.0.0-nat
```

### 2. 安装依赖

```bash
apt update
apt install -y python3 python3-venv python3-pip git iptables
```

### 3. 创建虚拟环境

```bash
cd /opt/PVETrafficManager
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 4. 写入环境变量

```bash
cp .env.example .env
nano .env
```

你至少要改这些：

- `PLATFORM_API_TOKEN`
- `PVE_HOST`
- `PVE_NODE`
- `PVE_API_TOKEN_ID`
- `PVE_API_TOKEN_SECRET`
- `PVE_IMAGE_TEMPLATES`
- `PVE_NAT_UPLINK_INTERFACE`
- `PUBLIC_BASE_URL`

### 5. 配置 systemd

创建服务文件：

```bash
cat >/etc/systemd/system/pvetrafficmanager.service <<'EOF'
[Unit]
Description=PVETrafficManager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/PVETrafficManager
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/PVETrafficManager/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

启用并启动：

```bash
systemctl daemon-reload
systemctl enable --now pvetrafficmanager
systemctl status pvetrafficmanager --no-pager -l
```

### 6. 验证 NAT 是否工作

查看服务：

```bash
systemctl status pvetrafficmanager --no-pager -l
```

查看 `nat0`：

```bash
ip addr show nat0
```

查看转发：

```bash
sysctl net.ipv4.ip_forward
```

查看 NAT 规则：

```bash
iptables -t nat -S
iptables -S FORWARD
```

### 7. 打开面板

默认地址：

```text
http://你的服务器IP:8080
```

登录时填写：

```text
PLATFORM_API_TOKEN
```

## 使用说明

### 新建 NAT VM

在前端新建 VM 时：

- 网络模式选 `NAT`
- 镜像选模板
- 设置 CPU、内存、磁盘
- 设置 `ci_password`

创建后会自动得到：

- NAT 内网 IP
- SSH 端口
- NAT 端口段

这些信息在：

- 总 VM 列表
- VM 详情页

都能看到。

### 删除 VM

删除 VM 时会自动：

- 删除 PVE 虚拟机
- 释放 NAT 租约
- 删除 DNAT 规则
- 删除保存的凭据

### 重装系统

支持通过前端或 API 调用重装：

- 先关机
- 复制模板系统盘
- 更新 cloud-init
- 默认删除旧系统盘
- 自动开机

## 更新教程

以后更新 NAT 分支：

```bash
cd /opt/PVETrafficManager
git fetch origin
git checkout beta-2.0.0-nat
git pull origin beta-2.0.0-nat
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart pvetrafficmanager
systemctl status pvetrafficmanager --no-pager -l
```

## 当前已知限制

- 这版 NAT 逻辑按 `/24` 设计
- 每台 VM 固定占用一个连续 10 端口块
- 第一个端口固定映射 SSH
- 目前 DNAT 规则以 `iptables` 为主
- 如果宿主机同时有复杂防火墙策略，需要你自己做额外放行

## 建议

生产环境建议你额外做这几件事：

- 用 Nginx/Caddy 反代 8080
- 给面板挂 HTTPS
- 限制来源 IP
- 单独创建 PVE API Token
- 配置 systemd 开机自启
- 固化宿主机基础网络和 DNS
