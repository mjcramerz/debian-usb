#!/bin/sh
set -eu
IFS=$(printf '\n\t')

dusb_die() {
  printf 'build_iso.sh: %s\n' "$*" >&2
  exit 1
}

dusb_have() {
  command -v "$1" >/dev/null 2>&1
}

dusb_resolve_python_helper() {
  _dusb_script_dir=$1

  if [ -n "${DEBIAN_USB_PYTHON_HELPER:-}" ]; then
    printf '%s\n' "${DEBIAN_USB_PYTHON_HELPER}"
    return 0
  fi
  for _dusb_candidate in \
    "${_dusb_script_dir}/debian-usb-python" \
    "${_dusb_script_dir}/../scripts/debian-usb-python" \
    /usr/lib/debian-usb/debian-usb-python
  do
    if [ -x "${_dusb_candidate}" ]; then
      printf '%s\n' "${_dusb_candidate}"
      return 0
    fi
  done
  dusb_die "could not resolve debian-usb python helper"
}

dusb_reexec_with_sudo() {
  _dusb_script_path=$1
  shift
  if [ "$(id -u)" -eq 0 ]; then
    return 0
  fi
  dusb_have sudo || dusb_die "sudo is required for Debian ISO build actions"
  exec sudo env DEBIAN_USB_PYTHON_HELPER="${DEBIAN_USB_PYTHON_HELPER:-}" sh "${_dusb_script_path}" "$@"
}

main() {
  _dusb_mode=
  _dusb_plan_path=
  _dusb_script_dir=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
  _dusb_script_path=${_dusb_script_dir}/$(basename "$0")

  # Parse before escalation so help and malformed invocations never prompt.
  while [ "$#" -gt 0 ]; do
    case $1 in
      --ensure-debian-deps)
        _dusb_mode=ensure-deps
        ;;
      --ensure-debian-rebuild-deps)
        _dusb_mode=ensure-rebuild-deps
        ;;
      --plan)
        shift
        [ "$#" -gt 0 ] || dusb_die "--plan requires a file path"
        _dusb_mode=build-plan
        _dusb_plan_path=$1
        ;;
      --rebuild-installer-plan)
        shift
        [ "$#" -gt 0 ] || dusb_die "--rebuild-installer-plan requires a file path"
        _dusb_mode=rebuild-installer-plan
        _dusb_plan_path=$1
        ;;
      --help|-h)
        cat <<'EOF'
Usage:
  ./scripts/build_iso.sh --ensure-debian-deps
  ./scripts/build_iso.sh --ensure-debian-rebuild-deps
  ./scripts/build_iso.sh --plan /absolute/path/to/plan.json
  ./scripts/build_iso.sh --rebuild-installer-plan /absolute/path/to/plan.json
EOF
        exit 0
        ;;
      *) dusb_die "unknown argument: $1" ;;
    esac
    shift
  done

  [ -n "${_dusb_mode}" ] || dusb_die "either --ensure-debian-deps, --ensure-debian-rebuild-deps, --plan, or --rebuild-installer-plan is required"

  case ${_dusb_mode} in
    ensure-deps) set -- --ensure-debian-deps ;;
    ensure-rebuild-deps) set -- --ensure-debian-rebuild-deps ;;
    build-plan) set -- --plan "${_dusb_plan_path}" ;;
    rebuild-installer-plan) set -- --rebuild-installer-plan "${_dusb_plan_path}" ;;
  esac
  dusb_reexec_with_sudo "${_dusb_script_path}" "$@"

  if [ -z "${DEBIAN_USB_DEBIAN_ISO_BUILD_APT_DEPS:-}" ] && [ -r "${_dusb_script_dir}/../configs/install.env" ]; then
    # shellcheck source=../configs/install.env
    # shellcheck disable=SC1091
    . "${_dusb_script_dir}/../configs/install.env"
    if [ -n "${DEBIAN_ISO_BUILD_APT_DEPS:-}" ]; then
      export DEBIAN_USB_DEBIAN_ISO_BUILD_APT_DEPS="${DEBIAN_ISO_BUILD_APT_DEPS}"
    fi
  fi
  _dusb_python_helper=$(dusb_resolve_python_helper "${_dusb_script_dir}")

  case ${_dusb_mode} in
    ensure-deps) exec "${_dusb_python_helper}" ensure-debian-build-deps ;;
    ensure-rebuild-deps) exec "${_dusb_python_helper}" ensure-debian-rebuild-deps ;;
    build-plan) exec "${_dusb_python_helper}" build-debian-iso --plan "${_dusb_plan_path}" ;;
    rebuild-installer-plan) exec "${_dusb_python_helper}" rebuild-debian-installer-iso --plan "${_dusb_plan_path}" ;;
    *) dusb_die "unsupported mode: ${_dusb_mode}" ;;
  esac
}

main "$@"
