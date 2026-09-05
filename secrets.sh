#!/bin/sh
set -eu
IFS=$(printf '\n\t')

script_dir=$(CDPATH='' cd -P "$(dirname "$0")" && pwd -P)
exec python3 "${script_dir}/scripts/manage-secrets.py" --repo-root "${script_dir}" "$@"
