#!/usr/bin/env bash
set -Eeuo pipefail

PHASE="${1:-all}"
APP_DIR="${APP_DIR:-/opt/PVETrafficManager}"
BRANCH="${BRANCH:-beta-2.0.0-nat}"
SERVICE_NAME="${SERVICE_NAME:-pvetrafficmanager}"
PANEL_PORT="${PANEL_PORT:-8080}"
REPO_URL="${REPO_URL:-https://github.com/Lorry-San/PVETrafficManager.git}"

info() { echo -e "\033[1;34m[INFO]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err() { echo -e "\033[1;31m[ERR ]\033[0m $*" >&2; }
die() { err "$*"; exit 1; }

require_root() {
  [[ "${EUID:-0}" -eq 0 ]] || die "请用 root 运行此脚本"
}

detect_codename() {
  source /etc/os-release
  echo "${VERSION_CODENAME:-}"
}

ensure_supported_debian() {
  source /etc/os-release
  [[ "${ID:-}" == "debian" ]] || die "当前系统不是 Debian"
  [[ "${VERSION_CODENAME:-}" == "bookworm" || "${VERSION_CODENAME:-}" == "trixie" ]] || \
    die "只支持 Debian 12(bookworm) 或 Debian 13(trixie)"
}

primary_ip() {
  ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'
}

ensure_hosts_mapping() {
  local host short ip
  short="$(hostname -s)"
  host="$(hostname -f 2>/dev/null || hostname)"
  ip="$(primary_ip)"
  [[ -n "$ip" ]] || die "无法检测宿主机主 IPv4"

  if ! grep -Eq "^[[:space:]]*${ip//./\\.}[[:space:]]+.*\b${short}\b" /etc/hosts; then
    info "写入 /etc/hosts: $ip $host $short"
    cp /etc/hosts "/etc/hosts.bak.$(date +%Y%m%d%H%M%S)"
    {
      echo "127.0.0.1 localhost"
      echo "$ip $host $short"
      echo "::1 localhost ip6-localhost ip6-loopback"
      echo "ff02::1 ip6-allnodes"
      echo "ff02::2 ip6-allrouters"
    } >/etc/hosts
  fi

  hostname --ip-address | grep -vq '^127\.' || die "hostname 仍未解析到非 loopback IP，请先修复 /etc/hosts 或 DNS"
}

setup_pve_repo() {
  local codename
  codename="$(detect_codename)"
  apt-get update
  apt-get install -y wget curl ca-certificates gnupg

  if [[ "$codename" == "bookworm" ]]; then
    info "配置 Debian 12 / Proxmox VE 8 仓库"
    wget -qO /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg \
      https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg
    echo "deb [arch=amd64] http://download.proxmox.com/debian/pve bookworm pve-no-subscription" \
      >/etc/apt/sources.list.d/pve-install-repo.list
  elif [[ "$codename" == "trixie" ]]; then
    info "配置 Debian 13 / Proxmox VE 9 仓库"
    install -d /usr/share/keyrings
    wget -qO /usr/share/keyrings/proxmox-archive-keyring.gpg \
      https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg
    cat >/etc/apt/sources.list.d/pve-install-repo.sources <<'EOF'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
  else
    die "不支持的 Debian 版本代号: $codename"
  fi
}

phase_kernel() {
  local codename
  codename="$(detect_codename)"
  info "阶段 1：配置 Proxmox 仓库并安装 PVE 内核"
  setup_pve_repo
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
  DEBIAN_FRONTEND=noninteractive apt-get install -y proxmox-default-kernel
  info "当前 Debian 代号: $codename"
  warn "PVE 内核已安装。现在必须重启后再执行阶段 2。"
  warn "执行：reboot"
  warn "重启回来后执行：bash $0 phase2"
}

remove_debian_kernel() {
  local codename
  codename="$(detect_codename)"
  if [[ "$codename" == "bookworm" ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get remove -y linux-image-amd64 'linux-image-6.1*' || true
  elif [[ "$codename" == "trixie" ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get remove -y linux-image-amd64 'linux-image-6.12*' || true
  fi
  update-grub || true
  DEBIAN_FRONTEND=noninteractive apt-get remove -y os-prober || true
}

install_pve_stack() {
  info "阶段 2：安装 Proxmox VE 组件"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    proxmox-ve postfix open-iscsi chrony ifupdown2 bridge-utils vlan jq sqlite3 git python3 python3-venv python3-pip iptables curl
  DEBIAN_FRONTEND=noninteractive apt-get remove -y systemd-timesyncd || true
  remove_debian_kernel
}

prompt_if_empty() {
  local var_name="$1"
  local prompt_text="$2"
  local secret="${3:-0}"
  if [[ -z "${!var_name:-}" ]]; then
    if [[ "$secret" == "1" ]]; then
      read -r -s -p "$prompt_text: " "$var_name"; echo
    else
      read -r -p "$prompt_text: " "$var_name"
    fi
    export "$var_name"
  fi
}

write_env_file() {
  local public_ip
  public_ip="$(primary_ip)"

  prompt_if_empty PLATFORM_API_TOKEN "输入面板 Token" 1
  prompt_if_empty PVE_NODE "输入 PVE 节点名(例如 pve01)"
  prompt_if_empty PVE_API_TOKEN_ID "输入 PVE API Token ID(例如 root@pam!platform)"
  prompt_if_empty PVE_API_TOKEN_SECRET "输入 PVE API Token Secret" 1
  prompt_if_empty PVE_IMAGE_TEMPLATES '输入镜像映射 JSON(例如 {"debian-12":9000})'

  : "${PUBLIC_BASE_URL:=http://${public_ip}:${PANEL_PORT}}"
  : "${PLATFORM_DB_PATH:=./data/platform.db}"
  : "${PVE_SNIPPET_DIR:=/var/lib/vz/snippets}"
  : "${PVE_SNIPPET_STORAGE:=local}"
  : "${PVE_HOST:=https://127.0.0.1:8006}"
  : "${PVE_VERIFY_SSL:=false}"
  : "${PVE_DEFAULT_STORAGE:=local}"
  : "${PVE_DEFAULT_BRIDGE:=vmbr0}"
  : "${PVE_NAT_ENABLED:=true}"
  : "${PVE_NAT_BRIDGE:=nat0}"
  : "${PVE_NAT_NETWORK:=192.168.0.0/24}"
  : "${PVE_NAT_HOST_IP:=192.168.0.254}"
  : "${PVE_NAT_PORT_START:=30001}"
  : "${PVE_NAT_PORTS_PER_VM:=10}"
  : "${PVE_NAT_NAMESERVER:=8.8.8.8}"
  prompt_if_empty PVE_NAT_UPLINK_INTERFACE "输入 NAT 外网出口网卡(例如 vmbr0 或 ens18)"

  cat >"${APP_DIR}/.env" <<EOF
PLATFORM_API_TOKEN=${PLATFORM_API_TOKEN}
PUBLIC_BASE_URL=${PUBLIC_BASE_URL}
PLATFORM_DB_PATH=${PLATFORM_DB_PATH}
PVE_SNIPPET_DIR=${PVE_SNIPPET_DIR}
PVE_SNIPPET_STORAGE=${PVE_SNIPPET_STORAGE}

PVE_HOST=${PVE_HOST}
PVE_NODE=${PVE_NODE}
PVE_VERIFY_SSL=${PVE_VERIFY_SSL}
PVE_API_TOKEN_ID=${PVE_API_TOKEN_ID}
PVE_API_TOKEN_SECRET=${PVE_API_TOKEN_SECRET}

PVE_DEFAULT_STORAGE=${PVE_DEFAULT_STORAGE}
PVE_DEFAULT_BRIDGE=${PVE_DEFAULT_BRIDGE}
PVE_NAT_ENABLED=${PVE_NAT_ENABLED}
PVE_NAT_BRIDGE=${PVE_NAT_BRIDGE}
PVE_NAT_NETWORK=${PVE_NAT_NETWORK}
PVE_NAT_HOST_IP=${PVE_NAT_HOST_IP}
PVE_NAT_PORT_START=${PVE_NAT_PORT_START}
PVE_NAT_PORTS_PER_VM=${PVE_NAT_PORTS_PER_VM}
PVE_NAT_NAMESERVER=${PVE_NAT_NAMESERVER}
PVE_NAT_UPLINK_INTERFACE=${PVE_NAT_UPLINK_INTERFACE}
PVE_IMAGE_TEMPLATES=${PVE_IMAGE_TEMPLATES}
EOF
}

deploy_app() {
  info "部署 NAT 分支面板"
  mkdir -p "$(dirname "$APP_DIR")"
  if [[ -d "${APP_DIR}/.git" ]]; then
    git -C "$APP_DIR" fetch origin
    git -C "$APP_DIR" checkout "$BRANCH"
    git -C "$APP_DIR" pull origin "$BRANCH"
  else
    git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
  fi

  cd "$APP_DIR"
  python3 -m venv .venv
  . .venv/bin/activate
  python -m pip install -U pip
  pip install -r requirements.txt
  write_env_file
}

write_systemd_service() {
  cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=PVETrafficManager NAT Branch
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PANEL_PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}"
}

show_summary() {
  local ip
  ip="$(primary_ip)"
  cat <<EOF

安装完成。

面板地址：
  http://${ip}:${PANEL_PORT}

systemd 服务：
  ${SERVICE_NAME}

常用命令：
  systemctl status ${SERVICE_NAME} --no-pager -l
  journalctl -u ${SERVICE_NAME} -n 100 --no-pager
  ip addr show ${PVE_NAT_BRIDGE:-nat0}
  iptables -t nat -S
  iptables -S FORWARD

EOF
}

phase_app() {
  ensure_hosts_mapping
  install_pve_stack
  deploy_app
  write_systemd_service
  show_summary
}

main() {
  require_root
  ensure_supported_debian

  case "$PHASE" in
    phase1|kernel)
      phase_kernel
      ;;
    phase2|app)
      phase_app
      ;;
    all)
      phase_kernel
      echo
      warn "阶段 1 已完成。请先 reboot，再执行：bash $0 phase2"
      ;;
    *)
      die "用法: bash $0 [phase1|phase2|all]"
      ;;
  esac
}

main "$@"
