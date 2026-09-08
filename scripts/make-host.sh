#!/bin/sh
set -eu
IFS=$(printf '\n\t')

dusb_die() {
  printf 'make-host.sh: %s\n' "$*" >&2
  exit 1
}

dusb_note() (
  printf '%s\n' "$*"
)

dusb_note_kv() (
  printf '  %-16s %s\n' "$1" "$2"
)

dusb_assert_safe_path() (
  label="$1"
  path="${2%/}"
  stage_root="${DESTDIR:-}"
  stage_root="${stage_root%/}"

  [ -n "${path}" ] || dusb_die "${label} resolved to an empty path"
  [ "${path}" != "." ] || dusb_die "${label} resolved to the current directory"
  [ "${path}" != "/" ] || dusb_die "${label} resolved to /"
  if [ -n "${stage_root}" ] && [ "${path}" = "${stage_root}" ]; then
    dusb_die "${label} resolved to DESTDIR root: ${path}"
  fi
)

dusb_repo_root() (
  script_dir="$1"
  CDPATH='' cd "${script_dir}/.." && pwd
)

dusb_target_path() (
  path=$1
  if [ -n "${DESTDIR:-}" ]; then
    case ${path} in
      /*) printf '%s%s\n' "${DESTDIR}" "${path}"; return 0 ;;
    esac
  fi
  printf '%s\n' "${path}"
)

dusb_operation_mode() (
  if [ -n "${DESTDIR:-}" ]; then
    printf 'staged install\n'
  else
    printf 'live host install\n'
  fi
)

dusb_privilege_summary() (
  if [ -n "${DESTDIR:-}" ]; then
    printf 'no root required because DESTDIR is set\n'
  else
    printf 'root required for host writes; make will prompt through sudo\n'
  fi
)

dusb_remove_path() (
  label="$1"
  path="$2"

  dusb_assert_safe_path "${label}" "${path}"
  if [ -e "${path}" ] || [ -L "${path}" ]; then
    rm -rf -- "${path}"
    dusb_note_kv "Removed ${label}" "${path}"
  else
    dusb_note_kv "Absent ${label}" "${path}"
  fi
)

dusb_mount_targets_under_path() (
  path=${1%/}

  (findmnt -rn --output TARGET 2>/dev/null || true) |
    while IFS= read -r target; do
      [ -n "${target}" ] || continue
      case ${target} in
        "${path}"|"${path}"/*) printf '%s\n' "${target}" ;;
      esac
    done
)

dusb_release_mount_tree() (
  label=$1
  path=$2
  attempts=${3:-5}
  delay_s=${4:-0.5}
  try=1

  [ -e "${path}" ] || [ -L "${path}" ] || return 0
  cd / >/dev/null 2>&1 || true

  while [ "${try}" -le "${attempts}" ]; do
    mountpoints=$(dusb_mount_targets_under_path "${path}")
    [ -n "${mountpoints}" ] || return 0

    printf '%s\n' "${mountpoints}" | awk '{ paths[NR]=$0 } END { for (i=NR; i>0; i--) print paths[i] }' |
      while IFS= read -r mount_path; do
        [ -n "${mount_path}" ] || continue
        mountpoint -q -- "${mount_path}" || continue
        if ! umount -- "${mount_path}"; then
          dusb_note "Unmount retry ${try}/${attempts} could not release ${mount_path} under ${label}"
        fi
      done

    if [ "${try}" -lt "${attempts}" ]; then
      sleep "${delay_s}"
    fi
    try=$((try + 1))
  done

  mountpoints=$(dusb_mount_targets_under_path "${path}")
  if [ -n "${mountpoints}" ]; then
    mount_summary=$(printf '%s' "${mountpoints}" | tr '\n' ' ')
    dusb_die "failed to unmount managed mountpoints under ${label} ${path}: ${mount_summary}"
  fi
)
dusb_require_context() (
  if [ -n "${DESTDIR:-}" ]; then
    return 0
  fi
  if [ "$(id -u)" -ne 0 ]; then
    dusb_die "host operations require root privileges"
  fi
  if [ "${DUSB_INTERNAL_ROOT:-0}" != "1" ]; then
    dusb_die "host operations must run through the approved wrapper"
  fi
)

dusb_load_env() {
  script_dir="$1"
  env_file="${script_dir}/../configs/install.env"
  dusb_unset='__DUSB_UNSET__'
  prefix_override="${PREFIX-${dusb_unset}}"
  bindir_override="${BINDIR-${dusb_unset}}"
  etcdir_override="${ETCDIR-${dusb_unset}}"
  etc_dir_override="${ETC_DIR-${dusb_unset}}"
  libexecdir_override="${LIBEXECDIR-${dusb_unset}}"
  libexec_dir_override="${LIBEXEC_DIR-${dusb_unset}}"

  [ -r "${env_file}" ] || dusb_die "missing install env at ${env_file}"
  # shellcheck source=../configs/install.env
  # shellcheck disable=SC1091
  . "${env_file}"

  if [ "${prefix_override}" != "${dusb_unset}" ]; then
    PREFIX="${prefix_override}"
  fi
  if [ "${bindir_override}" != "${dusb_unset}" ]; then
    BINDIR="${bindir_override}"
  fi
  if [ "${etcdir_override}" != "${dusb_unset}" ]; then
    ETC_DIR="${etcdir_override}"
  fi
  if [ "${etc_dir_override}" != "${dusb_unset}" ]; then
    ETC_DIR="${etc_dir_override}"
  fi
  if [ "${libexecdir_override}" != "${dusb_unset}" ]; then
    LIBEXEC_DIR="${libexecdir_override}"
  fi
  if [ "${libexec_dir_override}" != "${dusb_unset}" ]; then
    LIBEXEC_DIR="${libexec_dir_override}"
  fi
}

dusb_ensure_binary() (
  repo_root="$1"
  build_dir="${repo_root}/build"
  binary_path="${build_dir}/${BIN_NAME}"

  if [ -x "${binary_path}" ]; then
    return 0
  fi
  command -v go >/dev/null 2>&1 || dusb_die "go is required to build ${BIN_NAME}"
  mkdir -p -- "${build_dir}"
  (
    cd "${repo_root}"
    go build -o "${binary_path}" "./cmd/${BIN_NAME}"
  )
)

dusb_install_tree() (
  src="$1"
  dst="$2"
  [ -d "${src}" ] || dusb_die "missing source tree: ${src}"
  dusb_assert_safe_path "install tree destination" "${dst}"
  rm -rf -- "${dst}"
  mkdir -p -- "${dst}"
  cp -a -- "${src}/." "${dst}/"
)

dusb_assign_runtime_owner() (
  path="$1"
  runtime_uid="${DUSB_RUNTIME_UID:-${SUDO_UID:-}}"
  runtime_gid="${DUSB_RUNTIME_GID:-${SUDO_GID:-}}"

  case ${runtime_uid} in ''|*[!0-9]*) dusb_die "missing numeric runtime user id for ${path}" ;; esac
  case ${runtime_gid} in ''|*[!0-9]*) dusb_die "missing numeric runtime group id for ${path}" ;; esac
  [ "${runtime_uid}" -gt 0 ] || dusb_die "refusing to assign runtime state to root"
  chown "${runtime_uid}:${runtime_gid}" -- "${path}" || dusb_die "failed to assign runtime owner for ${path}"
)

dusb_install() (
  script_dir="$1"
  repo_root="$(dusb_repo_root "${script_dir}")"
  binary_path="${repo_root}/build/${BIN_NAME}"
  dusb_ensure_binary "${repo_root}"

  bindir="$(dusb_target_path "${BINDIR}")"
  etcdir="$(dusb_target_path "${ETC_DIR}")"
  libexecdir="$(dusb_target_path "${LIBEXEC_DIR}")"
  specdir="$(dusb_target_path "${SPEC_DIR}")"
  preseeddir="$(dusb_target_path "${PRESEED_DIR}")"
  initrddir="$(dusb_target_path "${INITRD_DIR}")"
  persistencedir="$(dusb_target_path "${PERSISTENCE_DIR}")"
  pythondir="$(dusb_target_path "${PYTHON_INSTALL_DIR}")"
  hooksdir="${libexecdir}/config-hooks"
  statedir="$(dusb_target_path "${STATE_DIR}")"
  cachedir="$(dusb_target_path "${CACHE_DIR}")"
  logdir="$(dusb_target_path "${LOG_DIR}")"
  workdir="$(dusb_target_path "${WORK_DIR}")"
  docdir="$(dusb_target_path "${DOC_DIR}")"
  plannedexecutiondir="$(dusb_target_path "${PLANNED_EXECUTION_DIR}")"

  dusb_assert_safe_path "binary directory" "${bindir}"
  dusb_assert_safe_path "config directory" "${etcdir}"
  dusb_assert_safe_path "libexec directory" "${libexecdir}"
  dusb_assert_safe_path "spec directory" "${specdir}"
  dusb_assert_safe_path "preseed directory" "${preseeddir}"
  dusb_assert_safe_path "initrd directory" "${initrddir}"
  dusb_assert_safe_path "persistence directory" "${persistencedir}"
  dusb_assert_safe_path "python directory" "${pythondir}"
  dusb_assert_safe_path "config hooks directory" "${hooksdir}"
  dusb_assert_safe_path "state directory" "${statedir}"
  dusb_assert_safe_path "log directory" "${logdir}"
  dusb_assert_safe_path "work directory" "${workdir}"
  dusb_assert_safe_path "doc directory" "${docdir}"
  dusb_assert_safe_path "planned execution directory" "${plannedexecutiondir}"

  dusb_note "Installing managed debian-usb assets"
  dusb_note_kv "Mode" "$(dusb_operation_mode)"
  dusb_note_kv "Repo root" "${repo_root}"

  install -d -- "${bindir}" "${etcdir}" "${libexecdir}" "${specdir}" "${preseeddir}" "${initrddir}" "${persistencedir}" "${pythondir}" "${statedir}" "${logdir}" "${workdir}" "${docdir}"
  install -d -m 0755 -- "${plannedexecutiondir}"
  if [ -z "${DESTDIR:-}" ]; then
    dusb_assign_runtime_owner "${plannedexecutiondir}"
  fi
  install -m 0755 -- "${binary_path}" "${bindir}/${BIN_NAME}"
  install -m 0755 -- "${script_dir}/debian-usb-python" "${libexecdir}/${PYTHON_HELPER_NAME}"
  install -m 0755 -- "${script_dir}/write_usb.sh" "${libexecdir}/${WRITE_HELPER_NAME}"
  install -m 0755 -- "${script_dir}/build_iso.sh" "${libexecdir}/${BUILD_ISO_HELPER_NAME}"
  install -m 0644 -- "${repo_root}/configs/install.env" "${libexecdir}/install.env"
  dusb_install_tree "${repo_root}/configs/spec" "${specdir}"
  dusb_install_tree "${repo_root}/configs/preseed" "${preseeddir}"
  dusb_install_tree "${repo_root}/initrd" "${initrddir}"
  for family in debian kali; do
    if [ -f "${initrddir}/${family}/live/live.env" ]; then
      chmod 0600 "${initrddir}/${family}/live/live.env" || dusb_die "failed to protect Live environment file"
    fi
  done
  install -m 0644 -- "${repo_root}/configs/persistence-debian.conf" "${persistencedir}/debian.conf"
  install -m 0644 -- "${repo_root}/configs/persistence-kali.conf" "${persistencedir}/kali.conf"
  dusb_install_tree "${repo_root}/src/python/debian_usb" "${pythondir}/debian_usb"
  dusb_install_tree "${repo_root}/config-hooks" "${hooksdir}"
  chmod 0644 \
    "${specdir}/live/admin-tools.json" \
    "${pythondir}/debian_usb/live_tools.py" \
    "${pythondir}/debian_usb/live_hooks.py" || dusb_die "failed to set Live administration tool runtime file modes"
  chmod 0755 \
    "${hooksdir}/0500-apt-live-medium.sh" \
    "${hooksdir}/live-apt-repository.py" \
    "${hooksdir}/1000-network-wifi.sh" || dusb_die "failed to set live config hook modes"
  install -m 0644 -- "${repo_root}/configs/debian-usb.conf" "${etcdir}/debian-usb.conf"
  install -m 0644 -- "${repo_root}/README.md" "${docdir}/README.md"
  if [ -d "${repo_root}/docs" ]; then
    dusb_install_tree "${repo_root}/docs" "${docdir}/docs"
  fi
  dusb_note "Install complete"
  dusb_note_kv "Binary" "${bindir}/${BIN_NAME}"
  dusb_note_kv "Config" "${etcdir}/debian-usb.conf"
  dusb_note_kv "Helpers" "${libexecdir}/${PYTHON_HELPER_NAME}, ${libexecdir}/${WRITE_HELPER_NAME}, ${libexecdir}/${BUILD_ISO_HELPER_NAME}"
  dusb_note_kv "Spec tree" "${specdir}"
  dusb_note_kv "Preseed tree" "${preseeddir}"
  dusb_note_kv "Initrd overlay tree" "${initrddir}"
  dusb_note_kv "Persistence" "${persistencedir}"
  dusb_note_kv "Python tree" "${pythondir}/debian_usb"
  dusb_note_kv "Config hooks" "${hooksdir}"
  dusb_note_kv "State dir" "${statedir}"
  dusb_note_kv "Download root" "${cachedir} (runtime-managed; install leaves it untouched)"
  dusb_note_kv "Log dir" "${logdir}"
  dusb_note_kv "Work dir" "${workdir}"
  dusb_note_kv "Planned executions" "${plannedexecutiondir}"
  dusb_note_kv "Docs" "${docdir}/README.md"
)

dusb_uninstall() (
  bindir="$(dusb_target_path "${BINDIR}")"
  etcdir="$(dusb_target_path "${ETC_DIR}")"
  libexecdir="$(dusb_target_path "${LIBEXEC_DIR}")"
  specdir="$(dusb_target_path "${SPEC_DIR}")"
  preseeddir="$(dusb_target_path "${PRESEED_DIR}")"
  initrddir="$(dusb_target_path "${INITRD_DIR}")"
  persistencedir="$(dusb_target_path "${PERSISTENCE_DIR}")"
  pythondir="$(dusb_target_path "${PYTHON_INSTALL_DIR}")"
  docdir="$(dusb_target_path "${DOC_DIR}")"

  dusb_assert_safe_path "binary directory" "${bindir}"
  dusb_assert_safe_path "config directory" "${etcdir}"
  dusb_assert_safe_path "libexec directory" "${libexecdir}"
  dusb_assert_safe_path "spec directory" "${specdir}"
  dusb_assert_safe_path "preseed directory" "${preseeddir}"
  dusb_assert_safe_path "initrd directory" "${initrddir}"
  dusb_assert_safe_path "persistence directory" "${persistencedir}"
  dusb_assert_safe_path "python directory" "${pythondir}"
  dusb_assert_safe_path "doc directory" "${docdir}"

  dusb_note "Removing managed debian-usb runtime files"
  dusb_note_kv "Mode" "$(dusb_operation_mode)"
  dusb_remove_path "binary" "${bindir}/${BIN_NAME}"
  dusb_remove_path "python helper" "${libexecdir}/${PYTHON_HELPER_NAME}"
  dusb_remove_path "write helper" "${libexecdir}/${WRITE_HELPER_NAME}"
  dusb_remove_path "build-iso helper" "${libexecdir}/${BUILD_ISO_HELPER_NAME}"
  dusb_remove_path "helper install env" "${libexecdir}/install.env"
  dusb_remove_path "config file" "${etcdir}/debian-usb.conf"
  dusb_remove_path "documentation" "${docdir}/README.md"
  dusb_remove_path "spec tree" "${specdir}"
  dusb_remove_path "preseed tree" "${preseeddir}"
  dusb_remove_path "initrd overlay tree" "${initrddir}"
  dusb_remove_path "persistence tree" "${persistencedir}"
  dusb_remove_path "python tree" "${pythondir}/debian_usb"
  dusb_remove_path "config hooks tree" "${libexecdir}/config-hooks"
  dusb_note "Uninstall phase complete"
)

dusb_nuke() (
  dusb_uninstall
  etcdir="$(dusb_target_path "${ETC_DIR}")"
  statedir="$(dusb_target_path "${STATE_DIR}")"
  cachedir="$(dusb_target_path "${CACHE_DIR}")"
  logdir="$(dusb_target_path "${LOG_DIR}")"
  workdir="$(dusb_target_path "${WORK_DIR}")"
  libexecdir="$(dusb_target_path "${LIBEXEC_DIR}")"
  docdir="$(dusb_target_path "${DOC_DIR}")"

  dusb_assert_safe_path "config directory" "${etcdir}"
  dusb_assert_safe_path "state directory" "${statedir}"
  dusb_assert_safe_path "log directory" "${logdir}"
  dusb_assert_safe_path "work directory" "${workdir}"
  dusb_assert_safe_path "libexec directory" "${libexecdir}"
  dusb_assert_safe_path "doc directory" "${docdir}"

  dusb_note "Removing managed debian-usb directories"
  dusb_remove_path "config tree" "${etcdir}"
  dusb_remove_path "state tree" "${statedir}"
  dusb_note_kv "Preserved download root" "${cachedir}"
  dusb_remove_path "log tree" "${logdir}"
  dusb_release_mount_tree "work tree" "${workdir}"
  dusb_remove_path "work tree" "${workdir}"
  dusb_remove_path "libexec tree" "${libexecdir}"
  dusb_remove_path "doc tree" "${docdir}"
  dusb_note "Nuke complete"
)

dusb_describe_install() (
  cat <<EOF
Install plan
  Mode: $(dusb_operation_mode)
  Privileges: $(dusb_privilege_summary)
  Binary: $(dusb_target_path "${BINDIR}")/${BIN_NAME}
  Config: $(dusb_target_path "${ETC_DIR}")/debian-usb.conf
  Helpers: $(dusb_target_path "${LIBEXEC_DIR}")/${PYTHON_HELPER_NAME}, $(dusb_target_path "${LIBEXEC_DIR}")/${WRITE_HELPER_NAME}
  Spec tree: $(dusb_target_path "${SPEC_DIR}")
  Preseed tree: $(dusb_target_path "${PRESEED_DIR}")
  Initrd overlay tree: $(dusb_target_path "${INITRD_DIR}")
  Python tree: $(dusb_target_path "${PYTHON_INSTALL_DIR}")/debian_usb
  Config hooks: $(dusb_target_path "${LIBEXEC_DIR}")/config-hooks
  State dir: $(dusb_target_path "${STATE_DIR}")
  Download root: $(dusb_target_path "${CACHE_DIR}") (runtime-managed; install does not create or chown it)
  Log dir: $(dusb_target_path "${LOG_DIR}")
  Work dir: $(dusb_target_path "${WORK_DIR}")
  Planned executions: $(dusb_target_path "${PLANNED_EXECUTION_DIR}") (owned by the invoking non-root user)
  Docs: $(dusb_target_path "${DOC_DIR}")/README.md
  Result: managed paths are created or replaced from the repository copy
EOF
)

dusb_describe_nuke() (
  cat <<EOF
Nuke plan
  Mode: $(dusb_operation_mode)
  Privileges: $(dusb_privilege_summary)
  Binary: $(dusb_target_path "${BINDIR}")/${BIN_NAME}
  Config tree: $(dusb_target_path "${ETC_DIR}")
  Libexec tree: $(dusb_target_path "${LIBEXEC_DIR}")
  Download root: $(dusb_target_path "${CACHE_DIR}") (preserved)
  Log tree: $(dusb_target_path "${LOG_DIR}")
  State tree: $(dusb_target_path "${STATE_DIR}")
  Work tree: $(dusb_target_path "${WORK_DIR}")
  Docs tree: $(dusb_target_path "${DOC_DIR}")
  Result: managed runtime files, logs, and state are permanently removed; the download root is preserved
EOF
)

main() {
  script_dir="$(CDPATH='' cd "$(dirname -- "$0")" && pwd)"
  command="${1:-}"

  case "${command}" in
    describe-install)
      dusb_load_env "${script_dir}"
      dusb_describe_install
      ;;
    install)
      dusb_require_context
      dusb_load_env "${script_dir}"
      dusb_install "${script_dir}"
      ;;
    uninstall)
      dusb_require_context
      dusb_load_env "${script_dir}"
      dusb_uninstall
      ;;
    describe-nuke)
      dusb_load_env "${script_dir}"
      dusb_describe_nuke
      ;;
    nuke)
      dusb_require_context
      dusb_load_env "${script_dir}"
      dusb_nuke
      ;;
    *) dusb_die "usage: $0 {describe-install|install|uninstall|describe-nuke|nuke}" ;;
  esac
}

main "$@"
