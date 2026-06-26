#!/usr/bin/env bash
set -Eeuo pipefail

# Interactive qcow2/cloud-image builder.
#
# Required tools are installed automatically on supported builders.
# Set AUTO_INSTALL_DEPS=0 to only check dependencies.
#
# Usage:
#   ./build-qcow2-image.sh
#
# Non-interactive example:
#   IMAGE_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2" \
#   IMAGE_NAME="debian-12-lightcone.qcow2" \
#   BUILD_BRAND="LightCone Cloud" \
#   ./build-qcow2-image.sh

IMAGE_URL="${IMAGE_URL:-}"
IMAGE_SHA256="${IMAGE_SHA256:-}"
IMAGE_NAME="${IMAGE_NAME:-}"

WORK_DIR="${WORK_DIR:-./work/qcow2-build}"
OUTPUT_DIR="${OUTPUT_DIR:-/var/tmp/qcow2-build/dist}"
DOWNLOAD_NAME="${DOWNLOAD_NAME:-base-image}"

HOSTNAME="${HOSTNAME:-cloud-vm}"
TIMEZONE="${TIMEZONE:-Asia/Shanghai}"
INSTALL_PACKAGES="${INSTALL_PACKAGES:-qemu-guest-agent,curl,wget,vim,htop,ca-certificates,cloud-init}"
AUTO_INSTALL_DEPS="${AUTO_INSTALL_DEPS:-1}"
GUEST_APT_MIRROR="${GUEST_APT_MIRROR:-official}" # official, ustc, or none
DEBIAN_APT_MIRROR="${DEBIAN_APT_MIRROR:-https://mirrors.ustc.edu.cn/debian}"
DEBIAN_SECURITY_MIRROR="${DEBIAN_SECURITY_MIRROR:-https://mirrors.ustc.edu.cn/debian-security}"
UBUNTU_APT_MIRROR="${UBUNTU_APT_MIRROR:-https://mirrors.ustc.edu.cn/ubuntu}"

BUILD_BRAND="${BUILD_BRAND:-LightCone Cloud}"
BUILD_TIME_UTC="${BUILD_TIME_UTC:-$(date -u '+%Y-%m-%d %H:%M:%S UTC')}"
ENABLE_ROOT_SSH_PASSWORD_LOGIN="${ENABLE_ROOT_SSH_PASSWORD_LOGIN:-1}"
FIRST_BOOT_SCRIPT="${FIRST_BOOT_SCRIPT:-}"  # Optional local script path, copied into image and run once.
CUSTOM_SCRIPT="${CUSTOM_SCRIPT:-}"          # Optional local script path, run during image customization.
SYSPREP="${SYSPREP:-0}"                     # Set to 1 to run virt-sysprep before output.
LIBGUESTFS_BACKEND="${LIBGUESTFS_BACKEND:-direct}"

RAW_IMAGE=""
OUTPUT_IMAGE=""
MOTD_SNIPPET=""
APT_MIRROR_SCRIPT=""

MIRROR_NAMES=(
  "Debian official cloud latest"
  "Debian USTC cloud latest"
  "Ubuntu official cloud current"
  "Ubuntu USTC cloud current"
  "USTC debian-cdimage OpenStack"
  "Custom image URL"
)

MIRROR_URLS=(
  "https://cloud.debian.org/images/cloud"
  "https://mirrors.ustc.edu.cn/debian-cloud/images/cloud"
  "https://cloud-images.ubuntu.com"
  "https://mirrors.ustc.edu.cn/ubuntu-cloud-images"
  "https://mirrors.ustc.edu.cn/debian-cdimage/openstack"
  ""
)

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

run_as_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif has_cmd sudo; then
    sudo "$@"
  else
    die "root permission is required to install dependencies; rerun as root or install sudo"
  fi
}

missing_runtime_commands() {
  local missing=()
  has_cmd curl || missing+=("curl")
  has_cmd qemu-img || missing+=("qemu-img")
  has_cmd virt-customize || missing+=("virt-customize")
  has_cmd xz || missing+=("xz")
  ((${#missing[@]} == 0)) || printf '%s\n' "${missing[@]}"
}

install_dependencies() {
  local os_id="" os_like=""

  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    os_id="${ID:-}"
    os_like="${ID_LIKE:-}"
  fi

  if has_cmd apt-get; then
    log "installing dependencies with apt-get"
    run_as_root apt-get update
    run_as_root apt-get install -y curl qemu-utils libguestfs-tools xz-utils
  elif has_cmd dnf; then
    log "installing dependencies with dnf"
    run_as_root dnf install -y curl qemu-img libguestfs-tools-c xz
  elif has_cmd yum; then
    log "installing dependencies with yum"
    run_as_root yum install -y curl qemu-img libguestfs-tools-c xz
  elif has_cmd zypper; then
    log "installing dependencies with zypper"
    run_as_root zypper --non-interactive install curl qemu-tools libguestfs-tools xz
  elif has_cmd pacman; then
    log "installing dependencies with pacman"
    run_as_root pacman -Sy --needed --noconfirm curl qemu-img libguestfs xz
  else
    die "unsupported builder OS (${os_id:-unknown}${os_like:+, like: $os_like}); install curl, qemu-img, virt-customize, and xz manually"
  fi
}

trim_slashes() {
  local value="$1"
  value="${value%/}"
  value="${value#./}"
  printf '%s' "$value"
}

url_join() {
  local base="$1"
  local path="$2"
  printf '%s/%s' "$(trim_slashes "$base")" "${path#/}"
}

prompt_default() {
  local prompt="$1"
  local default_value="$2"
  local answer

  read -r -p "${prompt} [${default_value}]: " answer
  printf '%s' "${answer:-$default_value}"
}

select_from_array() {
  local title="$1"
  shift
  local options=("$@")
  local choice

  printf '\n%s\n' "$title" >&2
  local i
  for i in "${!options[@]}"; do
    printf '  %2d) %s\n' "$((i + 1))" "${options[$i]}" >&2
  done

  while true; do
    read -r -p "Select [1-${#options[@]}]: " choice
    [[ "$choice" =~ ^[0-9]+$ ]] || continue
    (( choice >= 1 && choice <= ${#options[@]} )) || continue
    printf '%s' "${options[$((choice - 1))]}"
    return
  done
}

fetch_links() {
  local url="$1"
  curl -fsSL "$url" |
    sed -nE 's/.*href="([^"#?]+)".*/\1/p' |
    sed -E 's/&amp;/\&/g' |
    awk '!seen[$0]++'
}

fetch_image_links() {
  local url="$1"
  fetch_links "$url" |
    grep -E '\.(qcow2|img)(\.xz)?$' |
    grep -Ev '(SHA|SUMS|manifest|json|torrent|zsync|vagrant|lxd|rootfs|kernel|initrd)' || true
}

fetch_directory_links() {
  local url="$1"
  fetch_links "$url" |
    grep '/$' |
    grep -Ev '^(\.\./|daily/|current/|latest/)$' || true
}

filter_images_for_arch() {
  local arch="$1"
  grep -Ei "(${arch}|amd64|x86_64)" | grep -Ei '(generic|genericcloud|server|cloud|nocloud|openstack)' || true
}

choose_image_from_directory() {
  local directory_url="$1"
  local arch="${2:-amd64}"
  local images

  mapfile -t images < <(fetch_image_links "$directory_url" | filter_images_for_arch "$arch")
  if (( ${#images[@]} == 0 )); then
    mapfile -t images < <(fetch_image_links "$directory_url")
  fi
  (( ${#images[@]} > 0 )) || die "no qcow2/img images found at $directory_url"

  local selected
  selected="$(select_from_array "Available images from ${directory_url}" "${images[@]}")"
  url_join "$directory_url" "$selected"
}

choose_debian_release() {
  select_from_array "Debian release" \
    "trixie" \
    "bookworm" \
    "bullseye" \
    "sid"
}

choose_ubuntu_release() {
  select_from_array "Ubuntu release" \
    "noble" \
    "jammy" \
    "focal"
}

configure_interactively() {
  local mirror
  mirror="$(select_from_array "Image source" "${MIRROR_NAMES[@]}")"

  case "$mirror" in
    "Debian official cloud latest")
      local release
      release="$(choose_debian_release)"
      IMAGE_URL="$(choose_image_from_directory "$(url_join "${MIRROR_URLS[0]}" "${release}/latest")" "amd64")"
      ;;
    "Debian USTC cloud latest")
      local release
      release="$(choose_debian_release)"
      IMAGE_URL="$(choose_image_from_directory "$(url_join "${MIRROR_URLS[1]}" "${release}/latest")" "amd64")"
      ;;
    "Ubuntu official cloud current")
      local release
      release="$(choose_ubuntu_release)"
      IMAGE_URL="$(choose_image_from_directory "$(url_join "${MIRROR_URLS[2]}" "${release}/current")" "amd64")"
      ;;
    "Ubuntu USTC cloud current")
      local release
      release="$(choose_ubuntu_release)"
      IMAGE_URL="$(choose_image_from_directory "$(url_join "${MIRROR_URLS[3]}" "${release}/current")" "amd64")"
      ;;
    "USTC debian-cdimage OpenStack")
      local release
      release="$(choose_debian_release)"
      IMAGE_URL="$(choose_image_from_directory "$(url_join "${MIRROR_URLS[4]}" "${release}/latest")" "amd64")"
      ;;
    "Custom image URL")
      read -r -p "Image URL: " IMAGE_URL
      [[ -n "$IMAGE_URL" ]] || die "IMAGE_URL is required"
      ;;
  esac

  BUILD_BRAND="$(prompt_default "Build brand" "$BUILD_BRAND")"
  GUEST_APT_MIRROR="$(select_from_array "Guest apt mirror" "official" "ustc" "none")"
  HOSTNAME="$(prompt_default "Default hostname" "$HOSTNAME")"
  TIMEZONE="$(prompt_default "Timezone" "$TIMEZONE")"
  OUTPUT_DIR="$(prompt_default "Output directory" "$OUTPUT_DIR")"
  IMAGE_NAME="$(prompt_default "Output image name" "$(default_image_name "$IMAGE_URL")")"
}

default_image_name() {
  local url="$1"
  local file
  file="${url##*/}"
  file="${file%.xz}"

  case "$file" in
    *.qcow2) printf '%s' "$file" ;;
    *.img) printf '%s.qcow2' "${file%.img}" ;;
    *) printf '%s.qcow2' "${file:-custom-cloud-image}" ;;
  esac
}

resolve_paths() {
  [[ -n "$IMAGE_URL" ]] || configure_interactively
  [[ -n "$IMAGE_NAME" ]] || IMAGE_NAME="$(default_image_name "$IMAGE_URL")"

  mkdir -p "$WORK_DIR" "$OUTPUT_DIR"

  local base_name
  base_name="${IMAGE_URL##*/}"
  base_name="${base_name%%\?*}"
  [[ -n "$base_name" ]] || base_name="$DOWNLOAD_NAME"

  RAW_IMAGE="${WORK_DIR}/${base_name}"
  OUTPUT_IMAGE="${OUTPUT_DIR}/${IMAGE_NAME}"
  MOTD_SNIPPET="${WORK_DIR}/99-image-build-motd"
  APT_MIRROR_SCRIPT="${WORK_DIR}/setup-guest-apt-mirror.sh"

  case "$OUTPUT_IMAGE" in
    /root/*)
      log "warning: OUTPUT_IMAGE is under /root; libguestfs direct backend is enabled to avoid libvirt qemu permission errors"
      ;;
  esac
}

check_dependencies() {
  local missing
  missing="$(missing_runtime_commands || true)"
  if [[ -n "$missing" ]]; then
    if [[ "$AUTO_INSTALL_DEPS" == "1" ]]; then
      log "missing dependencies: $(printf '%s' "$missing" | paste -sd ',' -)"
      install_dependencies
    else
      printf 'Missing dependencies:\n%s\n' "$missing" >&2
      die "install dependencies manually or set AUTO_INSTALL_DEPS=1"
    fi
  fi

  need_cmd curl
  need_cmd qemu-img
  need_cmd virt-customize
}

download_file() {
  local url="$1"
  local dest="$2"

  if [[ -s "$dest" ]]; then
    log "download exists, reusing: $dest"
    return
  fi

  log "downloading image: $url"
  curl -fL --retry 5 --retry-delay 3 --connect-timeout 20 -o "${dest}.tmp" "$url"
  mv "${dest}.tmp" "$dest"
}

verify_sha256() {
  local file="$1"
  local expected="$2"

  [[ -z "$expected" ]] && {
    log "IMAGE_SHA256 not set, skipping checksum verification"
    return
  }

  need_cmd sha256sum
  log "verifying sha256"
  printf '%s  %s\n' "$expected" "$file" | sha256sum -c -
}

copy_base_image() {
  log "creating output qcow2: $OUTPUT_IMAGE"
  if [[ "$RAW_IMAGE" == *.xz ]]; then
    need_cmd xz
    local decompressed="${RAW_IMAGE%.xz}"
    if [[ ! -s "$decompressed" ]]; then
      log "decompressing image: $RAW_IMAGE"
      xz -dc "$RAW_IMAGE" >"${decompressed}.tmp"
      mv "${decompressed}.tmp" "$decompressed"
    fi
    qemu-img convert -p -O qcow2 -c "$decompressed" "$OUTPUT_IMAGE"
  else
    qemu-img convert -p -O qcow2 -c "$RAW_IMAGE" "$OUTPUT_IMAGE"
  fi
}

create_motd_snippet() {
  cat >"$MOTD_SNIPPET" <<EOF
========================
This image built by ${BUILD_BRAND}.
Build time: ${BUILD_TIME_UTC}.
========================
EOF
}

create_apt_mirror_script() {
  cat >"$APT_MIRROR_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail

mode="${GUEST_APT_MIRROR}"
debian_mirror="${DEBIAN_APT_MIRROR}"
debian_security_mirror="${DEBIAN_SECURITY_MIRROR}"
ubuntu_mirror="${UBUNTU_APT_MIRROR}"

[[ "\$mode" != "none" ]] || exit 0
[[ -x /usr/bin/apt-get || -x /bin/apt-get ]] || exit 0
[[ -r /etc/os-release ]] || exit 0

. /etc/os-release
codename="\${VERSION_CODENAME:-\${UBUNTU_CODENAME:-}}"
[[ -n "\$codename" ]] || exit 0

mkdir -p /etc/apt/sources.list.d /etc/apt/sources.list.d.disabled
find /etc/apt/sources.list.d -maxdepth 1 -type f \\( -name '*.list' -o -name '*.sources' \\) \\
  -exec mv -f {} /etc/apt/sources.list.d.disabled/ \\; 2>/dev/null || true

case "\${ID:-}" in
  debian)
    if [[ "\$mode" == "official" ]]; then
      debian_mirror="https://deb.debian.org/debian"
      debian_security_mirror="https://deb.debian.org/debian-security"
    fi
    cat >/etc/apt/sources.list <<APT
deb \${debian_mirror} \${codename} main contrib non-free non-free-firmware
deb \${debian_mirror} \${codename}-updates main contrib non-free non-free-firmware
deb \${debian_mirror} \${codename}-backports main contrib non-free non-free-firmware
deb \${debian_security_mirror} \${codename}-security main contrib non-free non-free-firmware
APT
    ;;
  ubuntu)
    if [[ "\$mode" == "official" ]]; then
      ubuntu_mirror="https://archive.ubuntu.com/ubuntu"
    fi
    cat >/etc/apt/sources.list <<APT
deb \${ubuntu_mirror} \${codename} main restricted universe multiverse
deb \${ubuntu_mirror} \${codename}-updates main restricted universe multiverse
deb \${ubuntu_mirror} \${codename}-backports main restricted universe multiverse
deb \${ubuntu_mirror} \${codename}-security main restricted universe multiverse
APT
    ;;
  *)
    exit 0
    ;;
esac

apt-get clean
rm -rf /var/lib/apt/lists/*
EOF
  chmod +x "$APT_MIRROR_SCRIPT"
}

customize_image() {
  local args=(
    -a "$OUTPUT_IMAGE"
    --hostname "$HOSTNAME"
    --timezone "$TIMEZONE"
    --run "$APT_MIRROR_SCRIPT"
    --install "$INSTALL_PACKAGES"
    --run-command "systemctl enable qemu-guest-agent 2>/dev/null || true"
    --run-command "systemctl enable cloud-init 2>/dev/null || true"
    --run-command "mkdir -p /opt/image-hooks /etc/ssh/sshd_config.d /etc/cloud/cloud.cfg.d"
    --copy-in "$MOTD_SNIPPET:/opt/image-hooks"
    --run-command "touch /etc/motd && printf '\n' >> /etc/motd && cat /opt/image-hooks/$(basename "$MOTD_SNIPPET") >> /etc/motd"
    --write "/etc/sysctl.conf:fs.file-max = 6815744
net.ipv4.tcp_no_metrics_save = 1
net.ipv4.tcp_ecn = 0
net.ipv4.tcp_frto = 0
net.ipv4.tcp_mtu_probing = 0
net.ipv4.tcp_rfc1337 = 0
net.ipv4.tcp_sack = 1
net.ipv4.tcp_fack = 1
net.ipv4.tcp_window_scaling = 1
net.ipv4.tcp_adv_win_scale = 1
net.ipv4.tcp_moderate_rcvbuf = 1
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.ipv4.tcp_rmem = 4096 87380 33554432
net.ipv4.tcp_wmem = 4096 16384 33554432
net.ipv4.udp_rmem_min = 8192
net.ipv4.udp_wmem_min = 8192
net.ipv4.ip_forward = 1
net.ipv4.conf.all.route_localnet = 1
net.ipv4.conf.all.forwarding = 1
net.ipv4.conf.default.forwarding = 1
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv6.conf.all.forwarding = 1
net.ipv6.conf.default.forwarding = 1
"
    --write "/etc/security/limits.d/99-cloud-limits.conf:* soft nofile 1048576
* hard nofile 1048576
root soft nofile 1048576
root hard nofile 1048576
"
    --run-command "find /etc/ssh -maxdepth 2 -type f \\( -name 'sshd_config' -o -name '*.conf' \\) -exec sed -ri 's/^([[:space:]]*)(PermitRootLogin|PasswordAuthentication|KbdInteractiveAuthentication|ChallengeResponseAuthentication|UsePAM)[[:space:]]+/# \\1\\2 /' {} +"
    --run-command "grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config.d/\\*.conf' /etc/ssh/sshd_config || printf '\nInclude /etc/ssh/sshd_config.d/*.conf\n' >> /etc/ssh/sshd_config"
    --run-command "cloud-init clean --logs 2>/dev/null || true"
  )

  if [[ "$ENABLE_ROOT_SSH_PASSWORD_LOGIN" == "1" ]]; then
    args+=(
      --write "/etc/ssh/sshd_config.d/99-root-login.conf:PermitRootLogin yes
PasswordAuthentication yes
KbdInteractiveAuthentication yes
UsePAM yes
"
      --write "/etc/cloud/cloud.cfg.d/99-root-login.cfg:disable_root: false
ssh_pwauth: true
"
    )
  else
    args+=(
      --write "/etc/ssh/sshd_config.d/99-root-login.conf:PermitRootLogin prohibit-password
PasswordAuthentication no
"
      --write "/etc/cloud/cloud.cfg.d/99-root-login.cfg:disable_root: false
ssh_pwauth: false
"
    )
  fi

  if [[ -n "$CUSTOM_SCRIPT" ]]; then
    [[ -f "$CUSTOM_SCRIPT" ]] || die "CUSTOM_SCRIPT does not exist: $CUSTOM_SCRIPT"
    args+=(--run "$CUSTOM_SCRIPT")
  fi

  if [[ -n "$FIRST_BOOT_SCRIPT" ]]; then
    [[ -f "$FIRST_BOOT_SCRIPT" ]] || die "FIRST_BOOT_SCRIPT does not exist: $FIRST_BOOT_SCRIPT"
    args+=(--copy-in "$FIRST_BOOT_SCRIPT:/opt/image-hooks")
    args+=(--firstboot-command "chmod +x /opt/image-hooks/$(basename "$FIRST_BOOT_SCRIPT") && /opt/image-hooks/$(basename "$FIRST_BOOT_SCRIPT")")
  fi

  log "customizing image offline"
  virt-customize "${args[@]}"
}

run_sysprep() {
  [[ "$SYSPREP" == "1" ]] || return

  need_cmd virt-sysprep
  log "running virt-sysprep"
  virt-sysprep -a "$OUTPUT_IMAGE" \
    --operations machine-id,ssh-hostkeys,udev-persistent-net,logfiles,tmp-files,bash-history
}

show_config() {
  printf '\nBuild configuration\n'
  printf '  Image URL:     %s\n' "$IMAGE_URL"
  printf '  Output image:  %s\n' "$OUTPUT_IMAGE"
  printf '  Build brand:   %s\n' "$BUILD_BRAND"
  printf '  Build time:    %s\n' "$BUILD_TIME_UTC"
  printf '  Root SSH pass: %s\n' "$ENABLE_ROOT_SSH_PASSWORD_LOGIN"
  printf '  Guestfs:       %s\n' "$LIBGUESTFS_BACKEND"
  printf '  Guest apt:     %s\n' "$GUEST_APT_MIRROR"
  printf '\n'
}

show_result() {
  log "image build finished"
  qemu-img info "$OUTPUT_IMAGE"
}

main() {
  export LIBGUESTFS_BACKEND
  check_dependencies
  resolve_paths
  show_config
  download_file "$IMAGE_URL" "$RAW_IMAGE"
  verify_sha256 "$RAW_IMAGE" "$IMAGE_SHA256"
  copy_base_image
  create_motd_snippet
  create_apt_mirror_script
  customize_image
  run_sysprep
  show_result
}

main "$@"
