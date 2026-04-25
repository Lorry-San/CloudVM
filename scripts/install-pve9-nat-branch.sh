#!/usr/bin/env bash
set -Eeuo pipefail

STAGE="${1:-all}"

APP_DIR="${APP_DIR:-/opt/CloudVM}"
BRANCH="${BRANCH:-main}"
PANEL_PORT="${PANEL_PORT:-8080}"
SERVICE_NAME="${SERVICE_NAME:-cloudvm}"
GITHUB_PROXY="${GITHUB_PROXY:-https://hk.gh-proxy.org}"
DEBIAN_MIRROR="${DEBIAN_MIRROR:-https://mirrors.aliyun.com/debian}"
DEBIAN_SECURITY_MIRROR="${DEBIAN_SECURITY_MIRROR:-https://mirrors.aliyun.com/debian-security}"
PVE_MIRROR="${PVE_MIRROR:-http://download.proxmox.com/debian/pve}"
PVE_HOSTNAME="${PVE_HOSTNAME:-pve01}"
PVE_FQDN="${PVE_FQDN:-${PVE_HOSTNAME}.local}"
PVE_TOKEN_USER="${PVE_TOKEN_USER:-root@pam}"
PVE_TOKEN_ID="${PVE_TOKEN_ID:-platform}"
NAT_UPLINK_INTERFACE="${NAT_UPLINK_INTERFACE:-eno1}"
NAT_BRIDGE="${NAT_BRIDGE:-nat0}"
NAT_NETWORK="${NAT_NETWORK:-192.168.0.0/24}"
NAT_HOST_IP="${NAT_HOST_IP:-192.168.0.254}"
NAT_PORT_START="${NAT_PORT_START:-30001}"
NAT_PORTS_PER_VM="${NAT_PORTS_PER_VM:-10}"
NAT_NAMESERVER="${NAT_NAMESERVER:-8.8.8.8}"

info() { echo "[INFO] $*"; }
warn() { echo "[WARN] $*"; }
err() { echo "[ERR ] $*" >&2; }
die() { err "$*"; exit 1; }

require_root() {
  [[ "${EUID:-0}" -eq 0 ]] || die "Run as root."
}

check_debian13() {
  . /etc/os-release
  [[ "${ID:-}" == "debian" ]] || die "This script only supports Debian."
  [[ "${VERSION_CODENAME:-}" == "trixie" ]] || die "This script is written for Debian 13 (trixie)."
}

primary_ip() {
  ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'
}

download_file() {
  local url="$1"
  local output="$2"
  info "Downloading $url"
  curl -fL --connect-timeout 10 --retry 3 --retry-delay 2 -o "$output" "$url" || die "Download failed: $url"
}

github_proxy_url() {
  local url="$1"
  echo "${GITHUB_PROXY%/}/${url}"
}

configure_hostname_and_hosts() {
  local ip fqdn
  ip="$(primary_ip)"
  [[ -n "$ip" ]] || die "Cannot detect primary IPv4."
  fqdn="${PVE_FQDN}"

  info "Setting hostname to ${PVE_HOSTNAME}"
  hostnamectl set-hostname "${PVE_HOSTNAME}"

  info "Writing /etc/hosts"
  cp /etc/hosts "/etc/hosts.bak.$(date +%Y%m%d%H%M%S)" || true
  cat >/etc/hosts <<EOF
127.0.0.1 localhost
${ip} ${fqdn} ${PVE_HOSTNAME}
::1 localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
EOF
}

configure_apt_sources() {
  info "Configuring Debian 13 sources (Aliyun, deb822)"
  mkdir -p /etc/apt/sources.list.d
  rm -f /etc/apt/sources.list
  cat >/etc/apt/sources.list.d/debian.sources <<EOF
Types: deb
URIs: ${DEBIAN_MIRROR}
Suites: trixie trixie-updates
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: ${DEBIAN_SECURITY_MIRROR}
Suites: trixie-security
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
}

configure_pve_repo() {
  info "Configuring Proxmox VE 9 repository"
  mkdir -p /usr/share/keyrings
  download_file \
    "https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg" \
    "/usr/share/keyrings/proxmox-archive-keyring.gpg"
  cat >/etc/apt/sources.list.d/pve-install-repo.sources <<EOF
Types: deb
URIs: ${PVE_MIRROR}
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF
}

cleanup_old_kernel_packages() {
  info "Removing conflicting packages and old Debian kernels if present"
  apt-get remove -y vlan ifupdown || true
  apt-get remove -y 'linux-image-6.12*' 'linux-headers-6.12*' linux-image-amd64 || true
  apt-get autoremove -y || true
  apt-get clean
}

stage1() {
  configure_hostname_and_hosts
  configure_apt_sources
  apt-get clean
  rm -rf /var/lib/apt/lists/*
  apt-get update
  apt-get install -y curl wget ca-certificates gnupg lsb-release apt-transport-https
  configure_pve_repo
  apt-get update
  cleanup_old_kernel_packages
  DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
  DEBIAN_FRONTEND=noninteractive apt-get install -y proxmox-default-kernel
  warn "Stage1 completed. Reboot is required."
  warn "Run after reboot: bash $0 stage2"
}

remove_conflicting_packages() {
  info "Removing conflicting packages"
  apt-get remove -y vlan ifupdown || true
  apt-get autoremove -y || true
  apt-get --fix-broken install -y || true
  dpkg --configure -a || true
}

install_pve_stack() {
  info "Installing Proxmox VE 9 packages"
  remove_conflicting_packages
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y \
    proxmox-ve pve-manager postfix open-iscsi chrony ifupdown2 bridge-utils jq sqlite3 \
    git python3 python3-venv python3-pip iptables curl openssl
  DEBIAN_FRONTEND=noninteractive apt-get remove -y systemd-timesyncd os-prober linux-image-amd64 || true
  DEBIAN_FRONTEND=noninteractive apt-get remove -y 'linux-image-6.12*' 'linux-headers-6.12*' || true
  update-grub || true
  dpkg --configure -a
  apt-get --fix-broken install -y
}

generate_platform_token() {
  if [[ -z "${PLATFORM_API_TOKEN:-}" ]]; then
    PLATFORM_API_TOKEN="$(openssl rand -hex 24)"
    export PLATFORM_API_TOKEN
  fi
}

create_pve_api_token() {
  local token_json token_secret

  info "Creating or refreshing PVE API token ${PVE_TOKEN_USER}!${PVE_TOKEN_ID}"
  pveum user token remove "${PVE_TOKEN_USER}" "${PVE_TOKEN_ID}" >/dev/null 2>&1 || true
  token_json="$(pveum user token add "${PVE_TOKEN_USER}" "${PVE_TOKEN_ID}" --privsep 0 --expire 0 --output-format json)"
  token_secret="$(printf '%s\n' "${token_json}" | sed -n 's/.*"value"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
  [[ -n "${token_secret}" ]] || die "Failed to create PVE API token."

  PVE_API_TOKEN_ID="${PVE_TOKEN_USER}!${PVE_TOKEN_ID}"
  PVE_API_TOKEN_SECRET="${token_secret}"
  export PVE_API_TOKEN_ID PVE_API_TOKEN_SECRET
}

ensure_snippets_dir() {
  mkdir -p /var/lib/vz/snippets
}

persist_nat_bridge() {
  local marker iface_file
  marker="# CloudVM NAT bridge"
  iface_file="/etc/network/interfaces"

  [[ -f "${iface_file}" ]] || touch "${iface_file}"
  info "Persisting ${NAT_BRIDGE} into ${iface_file}"
  grep -qF "${marker}" "${iface_file}" 2>/dev/null || cat >>"${iface_file}" <<EOF

${marker}
auto ${NAT_BRIDGE}
iface ${NAT_BRIDGE} inet static
    address ${NAT_HOST_IP}/24
    bridge_ports none
    bridge_stp off
    bridge_fd 0
EOF

  if command -v ifreload >/dev/null 2>&1; then
    ifreload -a
  else
    systemctl restart networking
  fi
}

prompt_if_empty() {
  local var_name="$1"
  local prompt_text="$2"
  local secret="${3:-0}"

  if [[ -z "${!var_name:-}" ]]; then
    if [[ "${secret}" == "1" ]]; then
      read -r -s -p "${prompt_text}: " "${var_name}"
      echo
    else
      read -r -p "${prompt_text}: " "${var_name}"
    fi
    export "${var_name}"
  fi
}

deploy_repo() {
  local tarball_url archive_path extract_dir

  info "Deploying ${BRANCH}"
  mkdir -p "$(dirname "${APP_DIR}")"
  if [[ -d "${APP_DIR}/.git" ]]; then
    git -C "${APP_DIR}" fetch origin
    git -C "${APP_DIR}" checkout "${BRANCH}"
    git -C "${APP_DIR}" pull origin "${BRANCH}"
  else
    tarball_url="$(github_proxy_url "https://github.com/Lorry-San/CloudVM/archive/refs/heads/${BRANCH}.tar.gz")"
    archive_path="/tmp/CloudVM-${BRANCH}.tar.gz"
    extract_dir="/tmp/CloudVM-${BRANCH}"
    rm -rf "${extract_dir}" "${APP_DIR}"
    mkdir -p "${extract_dir}"
    download_file "${tarball_url}" "${archive_path}"
    tar -xzf "${archive_path}" -C "${extract_dir}" --strip-components=1
    mv "${extract_dir}" "${APP_DIR}"
  fi
}

install_python_env() {
  info "Installing Python dependencies"
  cd "${APP_DIR}"
  python3 -m venv .venv
  . .venv/bin/activate
  python -m pip install -U pip setuptools wheel
  pip install -r requirements.txt
}

write_env() {
  local public_ip
  public_ip="$(primary_ip)"
  generate_platform_token
  create_pve_api_token

  prompt_if_empty PVE_IMAGE_TEMPLATES 'Enter template map JSON (example: {"debian-13":9000})'

  cat >"${APP_DIR}/.env" <<EOF
PLATFORM_API_TOKEN=${PLATFORM_API_TOKEN}
PUBLIC_BASE_URL=http://${public_ip}:${PANEL_PORT}
PLATFORM_DB_PATH=./data/platform.db
PVE_SNIPPET_DIR=/var/lib/vz/snippets
PVE_SNIPPET_STORAGE=local

PVE_HOST=https://127.0.0.1:8006
PVE_NODE=${PVE_HOSTNAME}
PVE_VERIFY_SSL=false
PVE_API_TOKEN_ID=${PVE_API_TOKEN_ID}
PVE_API_TOKEN_SECRET=${PVE_API_TOKEN_SECRET}

PVE_DEFAULT_STORAGE=local
PVE_DEFAULT_BRIDGE=vmbr0
PVE_NAT_ENABLED=true
PVE_NAT_BRIDGE=${NAT_BRIDGE}
PVE_NAT_NETWORK=${NAT_NETWORK}
PVE_NAT_HOST_IP=${NAT_HOST_IP}
PVE_NAT_PORT_START=${NAT_PORT_START}
PVE_NAT_PORTS_PER_VM=${NAT_PORTS_PER_VM}
PVE_NAT_NAMESERVER=${NAT_NAMESERVER}
PVE_NAT_UPLINK_INTERFACE=${NAT_UPLINK_INTERFACE}
PVE_IMAGE_TEMPLATES=${PVE_IMAGE_TEMPLATES}
EOF
}

write_service() {
  info "Writing systemd service"
  cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=CloudVM
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

print_summary() {
  local public_ip
  public_ip="$(primary_ip)"
  cat <<EOF

Install complete.

Panel URL:
  http://${public_ip}:${PANEL_PORT}

Node:
  ${PVE_HOSTNAME}

Platform login token:
  ${PLATFORM_API_TOKEN}

PVE API token id:
  ${PVE_API_TOKEN_ID}

PVE API token secret:
  ${PVE_API_TOKEN_SECRET}

Service status:
  systemctl status ${SERVICE_NAME} --no-pager -l
EOF
}

stage2() {
  install_pve_stack
  persist_nat_bridge
  ensure_snippets_dir
  deploy_repo
  install_python_env
  write_env
  write_service
  print_summary
}

main() {
  require_root
  check_debian13

  case "${STAGE}" in
    stage1)
      stage1
      ;;
    stage2)
      stage2
      ;;
    all)
      stage1
      warn "Reboot first, then run: bash $0 stage2"
      ;;
    *)
      die "Usage: bash $0 [stage1|stage2|all]"
      ;;
  esac
}

main "$@"
