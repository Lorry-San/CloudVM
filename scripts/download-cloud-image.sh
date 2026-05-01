#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE_NAME="debian-12"
if [[ $# -gt 0 && "${1:0:1}" != "-" ]]; then
  IMAGE_NAME="$1"
  shift
fi

OUTPUT_DIR="${OUTPUT_DIR:-/var/lib/vz/template/qemu}"
OUTPUT_FILE=""
ENABLE_PASSWORD_AUTH=0
ENABLE_ROOT_LOGIN=0
ENABLE_QEMU_AGENT=0
ROOT_PASSWORD=""
CUSTOMIZE=1
MOTD_FILE=""
AUTO_INSTALL_DEPS="${AUTO_INSTALL_DEPS:-1}"

info() { echo "[INFO] $*"; }
warn() { echo "[WARN] $*"; }
err() { echo "[ERR ] $*" >&2; }
die() { err "$*"; exit 1; }

usage() {
  cat <<EOF
Usage:
  bash $0 <image-name> [options]

Supported image names:
  debian-12
  debian-13
  ubuntu-22.04
  ubuntu-24.04

Options:
  --output-dir <dir>          Output directory
  --output-file <file>        Output filename
  --enable-password-auth      Enable SSH password authentication
  --enable-root-login         Enable root SSH login
  --set-root-password <pass>  Set root password inside image
  --enable-qemu-agent         Install qemu-guest-agent in the image
  --motd-file <path>          Replace /etc/motd with content from local file
  --no-customize              Download only, skip virt-customize
  --no-install-deps           Do not install missing host dependencies
  -h, --help                  Show this help
EOF
}

require_arg() {
  [[ $# -ge 2 && -n "${2:-}" && "${2:0:1}" != "-" ]] || die "Missing value for ${1}"
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

ensure_download_tool() {
  if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
    return
  fi
  install_packages curl ca-certificates || die "Need curl or wget"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      require_arg "$@"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --output-file)
      require_arg "$@"
      OUTPUT_FILE="$2"
      shift 2
      ;;
    --enable-password-auth)
      ENABLE_PASSWORD_AUTH=1
      shift
      ;;
    --enable-root-login)
      ENABLE_ROOT_LOGIN=1
      shift
      ;;
    --set-root-password)
      require_arg "$@"
      ROOT_PASSWORD="$2"
      shift 2
      ;;
    --enable-qemu-agent)
      ENABLE_QEMU_AGENT=1
      shift
      ;;
    --motd-file)
      require_arg "$@"
      MOTD_FILE="$2"
      shift 2
      ;;
    --no-customize)
      CUSTOMIZE=0
      shift
      ;;
    --no-install-deps)
      AUTO_INSTALL_DEPS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

case "${IMAGE_NAME}" in
  debian-12)
    IMAGE_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2"
    DEFAULT_FILE="debian-12-generic-amd64.qcow2"
    ;;
  debian-13)
    IMAGE_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2"
    DEFAULT_FILE="debian-13-generic-amd64.qcow2"
    ;;
  ubuntu-22.04)
    IMAGE_URL="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"
    DEFAULT_FILE="ubuntu-22.04-server-cloudimg-amd64.img"
    ;;
  ubuntu-24.04)
    IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
    DEFAULT_FILE="ubuntu-24.04-server-cloudimg-amd64.img"
    ;;
  *)
    die "Unsupported image name: ${IMAGE_NAME}"
    ;;
esac

if [[ -z "${OUTPUT_FILE}" ]]; then
  OUTPUT_FILE="${DEFAULT_FILE}"
fi

mkdir -p "${OUTPUT_DIR}"
IMAGE_PATH="${OUTPUT_DIR%/}/${OUTPUT_FILE}"

download() {
  ensure_download_tool
  if command -v curl >/dev/null 2>&1; then
    curl -fL --connect-timeout 15 --retry 3 --retry-delay 2 -o "${IMAGE_PATH}" "${IMAGE_URL}"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "${IMAGE_PATH}" "${IMAGE_URL}"
  else
    die "Need curl or wget"
  fi
}

require_virt_customize() {
  command -v virt-customize >/dev/null 2>&1 && return
  install_packages libguestfs-tools || die "virt-customize not found. Install libguestfs-tools first."
}

build_customize_args() {
  CUSTOMIZE_ARGS=()

  if [[ "${ENABLE_PASSWORD_AUTH}" -eq 1 ]]; then
    CUSTOMIZE_ARGS+=(--run-command "grep -q '^PasswordAuthentication' /etc/ssh/sshd_config && sed -i 's/^PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config || echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config")
    CUSTOMIZE_ARGS+=(--run-command "grep -q '^ssh_pwauth:' /etc/cloud/cloud.cfg && sed -i 's/^ssh_pwauth:.*/ssh_pwauth: true/' /etc/cloud/cloud.cfg || echo 'ssh_pwauth: true' >> /etc/cloud/cloud.cfg")
  fi

  if [[ "${ENABLE_ROOT_LOGIN}" -eq 1 ]]; then
    CUSTOMIZE_ARGS+=(--run-command "grep -q '^PermitRootLogin' /etc/ssh/sshd_config && sed -i 's/^PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config || echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config")
    CUSTOMIZE_ARGS+=(--run-command "grep -q '^disable_root:' /etc/cloud/cloud.cfg && sed -i 's/^disable_root:.*/disable_root: false/' /etc/cloud/cloud.cfg || echo 'disable_root: false' >> /etc/cloud/cloud.cfg")
  fi

  if [[ -n "${ROOT_PASSWORD}" ]]; then
    CUSTOMIZE_ARGS+=(--root-password "password:${ROOT_PASSWORD}")
    CUSTOMIZE_ARGS+=(--run-command "grep -q '^chpasswd:' /etc/cloud/cloud.cfg && true || printf '\nchpasswd:\n  expire: false\n' >> /etc/cloud/cloud.cfg")
  fi

  if [[ "${ENABLE_QEMU_AGENT}" -eq 1 ]]; then
    CUSTOMIZE_ARGS+=(--install qemu-guest-agent)
    CUSTOMIZE_ARGS+=(--run-command "systemctl enable qemu-guest-agent || true")
  fi

  if [[ -n "${MOTD_FILE}" ]]; then
    [[ -f "${MOTD_FILE}" ]] || die "MOTD file not found: ${MOTD_FILE}"
    CUSTOMIZE_ARGS+=(--upload "${MOTD_FILE}:/etc/motd")
    CUSTOMIZE_ARGS+=(--run-command "chmod 644 /etc/motd")
  fi
}

print_next_steps() {
  cat <<EOF

Downloaded image:
  ${IMAGE_PATH}

Suggested next steps for Proxmox:

  qm create 9000 --name ${IMAGE_NAME}-cloud --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0
  qm importdisk 9000 ${IMAGE_PATH} local
  qm set 9000 --scsihw virtio-scsi-single --virtio0 local:9000/vm-9000-disk-0
  qm set 9000 --ide2 local:cloudinit
  qm set 9000 --boot order=virtio0
  qm set 9000 --serial0 socket --vga serial0
  qm template 9000

If your storage is not 'local', replace it accordingly.
EOF
}

info "Downloading ${IMAGE_NAME} from official cloud image source"
download

if [[ "${CUSTOMIZE}" -eq 1 ]]; then
  if [[ "${ENABLE_PASSWORD_AUTH}" -eq 1 || "${ENABLE_ROOT_LOGIN}" -eq 1 || -n "${ROOT_PASSWORD}" || "${ENABLE_QEMU_AGENT}" -eq 1 || -n "${MOTD_FILE}" ]]; then
    require_virt_customize
    build_customize_args
    info "Customizing image ${IMAGE_PATH}"
    virt-customize -a "${IMAGE_PATH}" "${CUSTOMIZE_ARGS[@]}"
  else
    warn "No customization flags selected. Image downloaded as-is."
  fi
fi

print_next_steps
