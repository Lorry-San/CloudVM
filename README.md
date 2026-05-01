# CloudVM

CloudVM 是一个基于 Proxmox VE 的轻量 VM 管理平台，当前主分支已经包含：

- 公网桥接模式
- NAT 模式
- Web 控制台
- 流量统计
- 重装系统
- 控制台凭据保存
- 密码修改
- 镜像模板导入脚本
- Debian 12 / 13 的一键安装脚本

## 当前功能

### VM 生命周期

- 新建 VM
- 开机 / 关机 / 重启
- 删除 VM
- 删除前自动停机
- 支持从模板克隆

### 网络模式

- `public`
  - 默认桥接到 `vmbr0`
  - 可结合公网 IP 池自动分配
- `nat`
  - 默认使用 `nat0`
  - 默认网段 `192.168.0.0/24`
  - 宿主机地址 `192.168.0.254`
  - 默认起始端口 `30001`
  - 每台 VM 占用连续 10 个端口
  - 第一端口固定映射 SSH `-> :22`

示例：

- `192.168.0.1`
  - SSH: `30001 -> 22`
  - 其余：`30002-30010 -> 192.168.0.1:30002-30010`
- `192.168.0.2`
  - SSH: `30011 -> 22`
  - 其余：`30012-30020 -> 192.168.0.2:30012-30020`

### Cloud-Init

- 设置 `ciuser`
- 设置 `cipassword`
- 设置 `ipconfig0`
- 设置 DNS
- 设置 SSH 公钥
- 支持 `qm cloudinit update`

### 重装系统

- 基于模板盘重装
- 支持重新指定镜像 / 模板 VMID
- 支持调整系统盘大小
- 默认删除旧系统盘
- 重写 cloud-init
- 可选自动开机

### 密码与凭据

- 保存平台侧 VM 登录凭据
- 读取平台侧已保存凭据
- 单独修改 VM 密码
  - 更新 `ciuser` / `cipassword`
  - 执行 `qm cloudinit update`
  - 可选自动重启

### 流量统计

- 记录 VM 24 小时指标
- 查看当前周期流量
- 设置流量额度
- 设置重置日 / 小时 / 时区
- 手动重置流量统计

### 控制台

- VNC 控制台
- xterm.js 控制台
- 控制台短期 token
- 平台侧代理 WebSocket
- VNC 页面支持：
  - 粘贴用户名
  - 粘贴密码
  - 粘贴用户名+密码
  - 多行文本输入
  - 从浏览器剪贴板读取
  - 发送并回车

### 前端页面

- 总览页
- VM 列表页
- VM 详情页
- IP 池页面
- VNC 控制台页面

## 目录结构

```text
app/                      FastAPI 后端与前端静态页
docs/                     API 说明
integrations/zjmf/        魔方财务对接模块
scripts/                  安装、镜像下载、模板导入脚本
```

## 运行要求

- Proxmox VE 8 / 9
- Debian 12 / 13
- Python 3.11+
- `iptables`
- `git`
- `python3-venv`

## 核心配置

参考 `.env.example`。

最常用配置：

```env
PLATFORM_API_TOKEN=change-this-platform-token
PUBLIC_BASE_URL=http://your-ip:8080
PLATFORM_DB_PATH=./data/platform.db

PVE_HOST=https://127.0.0.1:8006
PVE_NODE=pve01
PVE_VERIFY_SSL=false
PVE_API_TOKEN_ID=root@pam!platform
PVE_API_TOKEN_SECRET=change-this-pve-api-token-secret

PVE_DEFAULT_STORAGE=local
PVE_DEFAULT_BRIDGE=vmbr0
PVE_IMAGE_TEMPLATES={"debian-12":9000}

PVE_NAT_ENABLED=true
PVE_NAT_BRIDGE=nat0
PVE_NAT_NETWORK=192.168.0.0/24
PVE_NAT_HOST_IP=192.168.0.254
PVE_NAT_PORT_START=30001
PVE_NAT_PORTS_PER_VM=10
PVE_NAT_NAMESERVER=8.8.8.8
PVE_NAT_UPLINK_INTERFACE=vmbr0
PVE_NAT_INGRESS_INTERFACES=vmbr0,vmbr1
```

## 安装

### Debian 12 / PVE 8

脚本：

```bash
bash scripts/install-pve-nat-branch.sh stage1
reboot
bash scripts/install-pve-nat-branch.sh stage2
```

### Debian 13 / PVE 9

脚本：

```bash
bash scripts/install-pve9-nat-branch.sh stage1
reboot
bash scripts/install-pve9-nat-branch.sh stage2
```

### 服务

当前默认目录和服务名：

- 目录：`/opt/CloudVM`
- 服务：`cloudvm`

常用命令：

```bash
systemctl restart cloudvm
systemctl status cloudvm --no-pager -l
journalctl -u cloudvm -n 100 --no-pager
```

## 模板镜像

### 1. 下载官方镜像

```bash
bash scripts/download-cloud-image.sh debian-12 \
  --enable-password-auth \
  --enable-root-login \
  --set-root-password 'ChangeMe123!' \
  --enable-qemu-agent
```

支持：

- `debian-12`
- `debian-13`
- `ubuntu-22.04`
- `ubuntu-24.04`

说明：

- Debian 使用官方 `generic-amd64.qcow2`
- 可选离线修改 SSH / root / cloud-init
- 缺少 `curl` / `wget` / `virt-customize` 时会自动用 `apt-get` 安装依赖；设置 `AUTO_INSTALL_DEPS=0` 或传 `--no-install-deps` 可关闭
- 可用 `--motd-file <path>` 写入镜像内 `/etc/motd`

### 2. 交互式导入模板

```bash
bash scripts/image-import.sh
```

脚本会：

- 选择官方镜像
- 可选修改镜像
- 下载镜像
- 将系统盘写入目录型存储
- 直接写 `/etc/pve/qemu-server/<vmid>.conf`
- 挂 cloud-init
- 转成模板

说明：

- 适用于目录型存储，如 `local`
- 不支持块存储，如 `local-lvm`
- `local` 直接按 `/var/lib/vz` 处理
- 不使用 `qm importdisk`
- 缺少 `curl` / `wget` / `qemu-img` / `python3` / `virt-customize` 时会自动用 `apt-get` 安装依赖；`qm` 仍要求在 Proxmox 节点上已有

## API 概览

当前主接口大致包括：

- `/api/v1/ip-pool`
- `/api/v1/images`
- `/api/v1/reinstall/images`
- `/api/v1/vms`
- `/api/v1/vms/{vmid}`
- `/api/v1/vms/{vmid}/config`
- `/api/v1/vms/{vmid}/reinstall`
- `/api/v1/vms/{vmid}/traffic`
- `/api/v1/vms/{vmid}/credentials`
- `/api/v1/vms/{vmid}/password`
- `/api/v1/vms/{vmid}/pause`
- `/api/v1/vms/{vmid}/resume`
- `/api/v1/vms/{vmid}/network/disconnect`
- `/api/v1/vms/{vmid}/network/connect`
- `/api/v1/consoles/token`
- `/api/v1/consoles/vnc/{vmid}`
- `/api/v1/consoles/xterm/{vmid}`
- `/api/v1/status`
- `/api/v1/metrics/history`

详细说明看：

- `docs/platform-api.md`

## 更新

```bash
cd /opt/CloudVM
git fetch origin
git checkout main
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
systemctl restart cloudvm
```

## 已知限制

- NAT 规则当前基于 `iptables`
- NAT 逻辑按 `/24` 网段设计
- 每台 NAT VM 固定占用连续 10 个端口
- 第一端口固定映射 SSH
- 镜像导入脚本当前只支持目录型存储
- 密码修改目前走 cloud-init 逻辑，不是 guest 内实时执行 `passwd`

## 对接

魔方财务模块在：

- `integrations/zjmf/cloudvmserver/`

## 建议

生产环境建议至少补这些：

- 反向代理 8080
- 配 HTTPS
- 限制来源 IP
- 单独创建 PVE API Token
- 固化宿主机网络和 DNS
- 定期备份平台数据库
