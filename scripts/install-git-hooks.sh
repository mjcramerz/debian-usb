#!/bin/sh
set -eu
IFS=$(printf '\n\t')
export LC_ALL=C

hooks_die() {
  printf 'install-git-hooks.sh: %s\n' "$*" >&2
  exit 1
}

script_dir=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
repo_root=$(git -C "${script_dir}/.." rev-parse --show-toplevel) || hooks_die "not inside a Git worktree"

for hook_name in pre-commit pre-push; do
  source_hook=${repo_root}/.githooks/${hook_name}
  [ -f "${source_hook}" ] && [ ! -L "${source_hook}" ] || hooks_die "missing regular tracked hook: ${source_hook}"
done

hooks_path=$(git -C "${repo_root}" rev-parse --git-path hooks) || hooks_die "unable to resolve Git hooks directory"
[ -n "${hooks_path}" ] && [ "${hooks_path}" != / ] && [ "${hooks_path}" != . ] || hooks_die "unsafe Git hooks directory: ${hooks_path}"
case ${hooks_path} in
  /*) ;;
  *) hooks_path=${repo_root}/${hooks_path} ;;
esac
[ ! -L "${hooks_path}" ] || hooks_die "refusing symlinked Git hooks directory: ${hooks_path}"
mkdir -p -- "${hooks_path}"
[ -d "${hooks_path}" ] && [ -w "${hooks_path}" ] || hooks_die "Git hooks directory is not writable: ${hooks_path}"

# Validate every target before replacing either hook.
for hook_name in pre-commit pre-push; do
  source_hook=${repo_root}/.githooks/${hook_name}
  target_hook=${hooks_path}/${hook_name}
  if [ -L "${target_hook}" ]; then
    hooks_die "refusing to replace symlinked hook: ${target_hook}"
  fi
  if [ -e "${target_hook}" ]; then
    [ -f "${target_hook}" ] || hooks_die "existing ${hook_name} hook is not a regular file: ${target_hook}"
    if ! cmp -s -- "${source_hook}" "${target_hook}" \
      && ! grep -Fqx "# debian-usb managed ${hook_name} hook" "${target_hook}"; then
      hooks_die "refusing to overwrite unrelated existing hook: ${target_hook}"
    fi
  fi
done

for hook_name in pre-commit pre-push; do
  source_hook=${repo_root}/.githooks/${hook_name}
  target_hook=${hooks_path}/${hook_name}
  if [ -e "${target_hook}" ] && cmp -s -- "${source_hook}" "${target_hook}"; then
    chmod 0755 -- "${target_hook}"
    printf 'debian-usb %s hook is already installed at %s\n' "${hook_name}" "${target_hook}"
    continue
  fi
  install -m 0755 -- "${source_hook}" "${target_hook}"
  printf 'Installed debian-usb %s hook at %s\n' "${hook_name}" "${target_hook}"
done
