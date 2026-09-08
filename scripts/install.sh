#!/bin/sh
set -eu
IFS=$(printf '\n\t')

dusb_die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

dusb_have() {
  command -v "$1" >/dev/null 2>&1
}

dusb_note() {
  printf '%s\n' "$*"
}

dusb_refuse_direct_root() {
  if [ "$(id -u)" -eq 0 ] && [ "${DEBIAN_USB_INSTALL_ESCALATED:-0}" != 1 ]; then
    dusb_die "Run ./scripts/install.sh as a non-root user; it will use sudo when host writes are required."
  fi
}

dusb_reexec_with_sudo() {
  _dusb_script_path=$1
  shift
  if [ "$(id -u)" -eq 0 ]; then
    return 0
  fi
  dusb_have sudo || dusb_die "sudo is required for host installation."
  _dusb_runtime_uid=$(id -u)
  _dusb_runtime_gid=$(id -g)
  exec sudo env \
    DEBIAN_USB_INSTALL_ESCALATED=1 \
    DUSB_RUNTIME_UID="${_dusb_runtime_uid}" \
    DUSB_RUNTIME_GID="${_dusb_runtime_gid}" \
    sh "${_dusb_script_path}" "$@"
}

dusb_usage() {
  cat <<'EOF'
Usage:
  ./scripts/install.sh [--deps-only] [--no-deps]

Options:
  --deps-only   Install Debian package dependencies only.
  --no-deps     Skip apt dependency installation and only install repository assets.
EOF
}

dusb_verify_installed_commands() {
  _dusb_missing=
  for _dusb_command in \
    aria2c blkid blockdev cpio cryptsetup cert-to-efi-sig-list dd fakeroot findmnt git go gpg \
    grub-install gzip lsblk lsinitramfs make mkfs.ext4 mkfs.vfat mount mountpoint \
    openssl parted partprobe python3 sbsign sbverify sign-efi-sig-list timeout umount xorriso
  do
    if ! dusb_have "${_dusb_command}"; then
      _dusb_missing=${_dusb_missing}${_dusb_missing:+ }${_dusb_command}
    fi
  done
  [ -z "${_dusb_missing}" ] || dusb_die "dependency installation completed but required commands are still missing: ${_dusb_missing}"
}

dusb_install_deps() {
  dusb_note "Installing Debian package prerequisites for debian-usb"
  dusb_note "  Packages: ${APT_DEPS} ${BUILD_DEPS}"
  _dusb_old_ifs=${IFS}
  IFS=' '
  set -f
  # Intentional field splitting: install.env stores package names as a space-separated list.
  # shellcheck disable=SC2086
  set -- ${APT_DEPS} ${BUILD_DEPS}
  set +f
  IFS=${_dusb_old_ifs}
  apt-get update
  apt-get install -y --no-install-recommends "$@"
  dusb_verify_installed_commands
  dusb_note "Dependency installation complete"
}

main() {
  _dusb_deps_only=0
  _dusb_no_deps=0
  _dusb_script_dir=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
  _dusb_script_path=${_dusb_script_dir}/$(basename "$0")

  while [ "$#" -gt 0 ]; do
    case $1 in
      --deps-only) _dusb_deps_only=1 ;;
      --no-deps) _dusb_no_deps=1 ;;
      --help|-h) dusb_usage; exit 0 ;;
      *) dusb_die "unknown argument: $1" ;;
    esac
    shift
  done

  if [ "${_dusb_deps_only}" -eq 1 ] && [ "${_dusb_no_deps}" -eq 1 ]; then
    dusb_die "--deps-only and --no-deps cannot be used together"
  fi

  dusb_refuse_direct_root
  if [ "${_dusb_deps_only}" -eq 1 ]; then
    set -- --deps-only
  elif [ "${_dusb_no_deps}" -eq 1 ]; then
    set -- --no-deps
  else
    set --
  fi
  dusb_reexec_with_sudo "${_dusb_script_path}" "$@"

  [ -r "${_dusb_script_dir}/../configs/install.env" ] || dusb_die "missing install env at ${_dusb_script_dir}/../configs/install.env"
  # shellcheck source=../configs/install.env
  # shellcheck disable=SC1091
  . "${_dusb_script_dir}/../configs/install.env"

  dusb_have apt-get || dusb_die "apt-get is required on Debian-based systems"

  if [ "${_dusb_no_deps}" -eq 0 ]; then
    dusb_install_deps
  fi
  if [ "${_dusb_deps_only}" -eq 1 ]; then
    dusb_note "Dependency-only run complete"
    exit 0
  fi

  dusb_note "Applying managed debian-usb host install"
  DUSB_INTERNAL_ROOT=1 sh "${_dusb_script_dir}/make-host.sh" install
  dusb_note "Install workflow complete"
  dusb_note "  Run: debian-usb"
}

main "$@"
