#!/usr/bin/env bash
set -Eeuo pipefail

OUTPUT_DIR="${OUTPUT_DIR:-/var/lib/vz/template/qemu}"
DEFAULT_STORAGE="${DEFAULT_STORAGE:-local}"
DEFAULT_BRIDGE="${DEFAULT_BRIDGE:-vmbr0}"
DEFAULT_MEMORY="${DEFAULT_MEMORY:-2048}"
DEFAULT_CORES="${DEFAULT_CORES:-2}"
DEFAULT_SCSIHW="${DEFAULT_SCSIHW:-virtio-scsi-single}"
DEFAULT_DISK_SLOT="${DEFAULT_DISK_SLOT:-virtio0}"
DEFAULT_OSTYPE="${DEFAULT_OSTYPE:-l26}"
DEFAULT_CPU="${DEFAULT_CPU:-host}"
DEFAULT_MACHINE="${DEFAULT_MACHINE:-q35}"
DEFAULT_SOCKETS="${DEFAULT_SOCKETS:-1}"
DEFAULT_NUMA="${DEFAULT_NUMA:-0}"
DEFAULT_NET_FIREWALL="${DEFAULT_NET_FIREWALL:-1}"
DEFAULT_AGENT="${DEFAULT_AGENT:-1}"
DEFAULT_IOTHREAD="${DEFAULT_IOTHREAD:-1}"
DEFAULT_CIUSER="${DEFAULT_CIUSER:-root}"
AUTO_INSTALL_DEPS="${AUTO_INSTALL_DEPS:-1}"

info() { echo "[INFO] $*"; }
warn() { echo "[WARN] $*"; }
err() { echo "[ERR ] $*" >&2; }
die() { err "$*"; exit 1; }

require_root() {
  [[ "${EUID:-0}" -eq 0 ]] || die "Run as root."
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"
}

is_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]]
}

install_packages() {
  [[ "${AUTO_INSTALL_DEPS}" -eq 1 ]] || return 1
  is_root || die "Missing dependency. Re-run as root or install manually: $*"
  command -v apt-get >/dev/null 2>&1 || die "Missing dependency and apt-get is not available: $*"
  info "Installing missing dependencies: $*"
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

ensure_cmd() {
  local cmd="$1"
  local package="${2:-$1}"
  command -v "$cmd" >/dev/null 2>&1 && return
  install_packages "$package" || die "Missing command: ${cmd}. Install package manually: ${package}"
  command -v "$cmd" >/dev/null 2>&1 || die "Missing command after install: ${cmd}"
}

ensure_download_tool() {
  if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
    return
  fi
  install_packages curl ca-certificates || die "Missing command: curl or wget"
}

download() {
  local url="$1" output="$2"
  ensure_download_tool
  if command -v curl >/dev/null 2>&1; then
    curl -fL --connect-timeout 15 --retry 3 --retry-delay 2 -o "$output" "$url"
  else
    wget -O "$output" "$url"
  fi
}

load_image_meta() {
  case "$1" in
    debian-12)
      IMAGE_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2"
      IMAGE_FILE="debian-12-generic-amd64.qcow2"
      TEMPLATE_NAME="debian-12-cloud"
      ;;
    debian-13)
      IMAGE_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2"
      IMAGE_FILE="debian-13-generic-amd64.qcow2"
      TEMPLATE_NAME="debian-13-cloud"
      ;;
    ubuntu-22.04)
      IMAGE_URL="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"
      IMAGE_FILE="ubuntu-22.04-server-cloudimg-amd64.img"
      TEMPLATE_NAME="ubuntu-22.04-cloud"
      ;;
    ubuntu-24.04)
      IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
      IMAGE_FILE="ubuntu-24.04-server-cloudimg-amd64.img"
      TEMPLATE_NAME="ubuntu-24.04-cloud"
      ;;
    *)
      die "Unsupported image: $1"
      ;;
  esac
}

pick_image() {
  echo "Select image:"
  echo "  1) debian-12"
  echo "  2) debian-13"
  echo "  3) ubuntu-22.04"
  echo "  4) ubuntu-24.04"
  read -r -p "Choice [1-4]: " choice
  case "${choice:-1}" in
    1) IMAGE_NAME="debian-12" ;;
    2) IMAGE_NAME="debian-13" ;;
    3) IMAGE_NAME="ubuntu-22.04" ;;
    4) IMAGE_NAME="ubuntu-24.04" ;;
    *) die "Invalid choice" ;;
  esac
}

prompt() {
  local var_name="$1" text="$2" default="${3:-}"
  local value=""
  if [[ -n "$default" ]]; then
    read -r -p "${text} [${default}]: " value
    value="${value:-$default}"
  else
    read -r -p "${text}: " value
  fi
  printf -v "$var_name" '%s' "$value"
}

customize_image() {
  local image_path="$1"
  local root_password="$2"
  local password_auth="$3"
  local root_login="$4"
  local qga="$5"
  local args=()

  if [[ "$password_auth" == "y" ]]; then
    args+=(--run-command "grep -q '^PasswordAuthentication' /etc/ssh/sshd_config && sed -i 's/^PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config")
    args+=(--run-command "grep -q '^ssh_pwauth:' /etc/cloud/cloud.cfg && sed -i 's/^ssh_pwauth:.*/ssh_pwauth: true/' /etc/cloud/cloud.cfg || echo 'ssh_pwauth: true' >> /etc/cloud/cloud.cfg")
  fi

  if [[ "$root_login" == "y" ]]; then
    args+=(--run-command "grep -q '^PermitRootLogin' /etc/ssh/sshd_config && sed -i 's/^PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config")
    args+=(--run-command "grep -q '^disable_root:' /etc/cloud/cloud.cfg && sed -i 's/^disable_root:.*/disable_root: false/' /etc/cloud/cloud.cfg || echo 'disable_root: false' >> /etc/cloud/cloud.cfg")
  fi

  if [[ -n "$root_password" ]]; then
    args+=(--root-password "password:${root_password}")
    args+=(--run-command "grep -q '^chpasswd:' /etc/cloud/cloud.cfg && true || printf '\nchpasswd:\n  expire: false\n' >> /etc/cloud/cloud.cfg")
  fi

  if [[ "$qga" == "y" ]]; then
    args+=(--install qemu-guest-agent)
    args+=(--run-command "systemctl enable qemu-guest-agent || true")
  fi

  if [[ "${#args[@]}" -gt 0 ]]; then
    ensure_cmd virt-customize libguestfs-tools
    info "Customizing ${image_path}"
    virt-customize -a "${image_path}" "${args[@]}"
  fi
}

storage_path_for_dir() {
  local storage="$1"
  local path
  if [[ "$storage" == "local" ]]; then
    printf '%s\n' "/var/lib/vz"
    return 0
  fi
  path="$(awk -v target="$storage" '
    $1=="dir:" {current=$2}
    current==target && $1=="path" {print $2; exit}
  ' /etc/pve/storage.cfg)"
  [[ -n "$path" ]] || die "Storage ${storage} is not a dir storage or was not found in /etc/pve/storage.cfg"
  printf '%s\n' "$path"
}

image_format() {
  local image_path="$1"
  qemu-img info --output json "$image_path" | python3 -c "import json,sys; print(json.load(sys.stdin)['format'])"
}

image_virtual_size_gb() {
  local image_path="$1"
  qemu-img info --output json "$image_path" | python3 -c "import json,sys,math; print(max(1, math.ceil(json.load(sys.stdin)['virtual-size'] / 1024**3)))"
}

random_mac() {
  printf 'BC:24:11:%02X:%02X:%02X\n' $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256))
}

write_vm_config() {
  local vmid="$1" name="$2" storage="$3" bridge="$4" memory="$5" cores="$6" disk_ref="$7" disk_slot="$8" scsihw="$9" qga="${10}" disk_size_gb="${11}"
  local config_path mac boot_order

  config_path="/etc/pve/qemu-server/${vmid}.conf"
  mac="$(random_mac)"
  case "$disk_slot" in
    virtio0) boot_order="virtio0;ide2;net0" ;;
    scsi0) boot_order="scsi0;ide2;net0" ;;
    *) die "Unsupported disk slot: ${disk_slot}" ;;
  esac

  cat >"${config_path}" <<EOF
name: ${name}
memory: ${memory}
cores: ${cores}
cpu: ${DEFAULT_CPU}
machine: ${DEFAULT_MACHINE}
ostype: ${DEFAULT_OSTYPE}
scsihw: ${scsihw}
net0: virtio=${mac},bridge=${bridge},firewall=${DEFAULT_NET_FIREWALL}
numa: ${DEFAULT_NUMA}
sockets: ${DEFAULT_SOCKETS}
ciuser: ${DEFAULT_CIUSER}
${disk_slot}: ${disk_ref},iothread=${DEFAULT_IOTHREAD},size=${disk_size_gb}G
ide2: none,media=cdrom
boot: order=${boot_order}
serial0: socket
vga: serial0
EOF

  if [[ "$qga" == "y" || "${DEFAULT_AGENT}" == "1" ]]; then
    echo "agent: 1" >>"${config_path}"
  fi
}

create_template() {
  local vmid="$1" name="$2" storage="$3" bridge="$4" memory="$5" cores="$6" image_path="$7" disk_slot="$8" scsihw="$9" qga="${10}"
  local storage_root image_dir fmt ext target_disk target_name disk_ref disk_size_gb

  [[ -f "/etc/pve/qemu-server/${vmid}.conf" ]] && die "VMID ${vmid} already exists"
  qm status "$vmid" >/dev/null 2>&1 && die "VMID ${vmid} already exists"

  storage_root="$(storage_path_for_dir "$storage")"
  image_dir="${storage_root%/}/images/${vmid}"
  fmt="$(image_format "$image_path")"
  disk_size_gb="$(image_virtual_size_gb "$image_path")"

  case "$fmt" in
    qcow2) ext="qcow2" ;;
    raw) ext="raw" ;;
    *)
      warn "Unsupported source image format ${fmt}, converting to qcow2"
      ext="qcow2"
      ;;
  esac

  mkdir -p "$image_dir"
  target_name="base-${vmid}-disk-0.${ext}"
  target_disk="${image_dir%/}/${target_name}"

  if [[ "$fmt" == "$ext" ]]; then
    info "Copying image to ${target_disk}"
    cp -f "$image_path" "$target_disk"
  else
    info "Converting image to ${target_disk}"
    qemu-img convert -p -O "$ext" "$image_path" "$target_disk"
  fi

  disk_ref="${storage}:${vmid}/${target_name}"
  info "Writing VM config /etc/pve/qemu-server/${vmid}.conf"
  write_vm_config "$vmid" "$name" "$storage" "$bridge" "$memory" "$cores" "$disk_ref" "$disk_slot" "$scsihw" "$qga" "$disk_size_gb"
  info "Creating cloud-init disk via PVE"
  qm set "$vmid" --ide2 "${storage}:cloudinit" >/dev/null
  info "Converting VM ${vmid} to template"
  qm template "$vmid" >/dev/null
}

main() {
  local customize root_password password_auth root_login qga vmid storage bridge memory cores disk_slot image_path

  require_root
  need_cmd qm
  ensure_cmd qemu-img qemu-utils
  ensure_cmd python3 python3
  ensure_download_tool
  mkdir -p "${OUTPUT_DIR}"

  pick_image
  load_image_meta "${IMAGE_NAME}"

  echo
  info "Selected image: ${IMAGE_NAME}"
  prompt vmid "Template VMID" "9000"
  prompt TEMPLATE_NAME "Template name" "${TEMPLATE_NAME}"
  prompt storage "Storage" "${DEFAULT_STORAGE}"
  prompt bridge "Bridge" "${DEFAULT_BRIDGE}"
  prompt memory "Memory MB" "${DEFAULT_MEMORY}"
  prompt cores "CPU cores" "${DEFAULT_CORES}"
  prompt disk_slot "Disk slot (virtio0/scsi0)" "${DEFAULT_DISK_SLOT}"

  read -r -p "Customize image before import? [Y/n]: " customize
  customize="${customize:-Y}"
  root_password=""
  password_auth="n"
  root_login="n"
  qga="n"
  if [[ "${customize}" =~ ^[Yy]$ ]]; then
    read -r -p "Enable SSH password auth? [y/N]: " password_auth
    password_auth="${password_auth:-n}"
    read -r -p "Enable root SSH login? [y/N]: " root_login
    root_login="${root_login:-n}"
    read -r -p "Install qemu-guest-agent? [Y/n]: " qga
    qga="${qga:-Y}"
    read -r -s -p "Set root password (leave empty to skip): " root_password
    echo
  fi

  image_path="${OUTPUT_DIR%/}/${IMAGE_FILE}"
  info "Downloading ${IMAGE_URL}"
  download "${IMAGE_URL}" "${image_path}"
  customize_image "${image_path}" "${root_password}" "${password_auth}" "${root_login}" "${qga}"
  create_template "${vmid}" "${TEMPLATE_NAME}" "${storage}" "${bridge}" "${memory}" "${cores}" "${image_path}" "${disk_slot}" "${DEFAULT_SCSIHW}" "${qga}"

  cat <<EOF

Done.

Template VMID: ${vmid}
Template name: ${TEMPLATE_NAME}
Image file: ${image_path}

Check:
  qm config ${vmid}
EOF
}

main "$@"
