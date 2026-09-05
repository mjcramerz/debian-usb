#!/usr/bin/env python3
"""Manage debian-usb secret fields without exposing values in diagnostics."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Iterable, TextIO
import warnings


RETIRED_CONFIG_SECRET_KEYS = ("DEFAULT_LIVE_WIFI_PSK",)
PRESEED_SECRET_KEYS = (
    "PRESEED_WIFI_PASSPHRASE",
    "PRESEED_FRUUX_USERNAME",
    "PRESEED_FRUUX_PASSWORD",
    "PRESEED_PRIMARY_USERNAME",
    "PRESEED_PRIMARY_PASSWORD",
    "PRESEED_PRIMARY_GPG_PASSPHRASE",
    "PRESEED_ROOT_PASSWORD",
    "PRESEED_CROWDSEC_TOKEN",
    "PRESEED_TAILSCALE_TOKEN",
    "PRESEED_TELEGRAM_CHAT_ID",
    "PRESEED_TELEGRAM_API_KEY",
    "PRESEED_CF_APTLY_ACCESS_KEY",
    "PRESEED_CF_APTLY_SECRET_KEY",
    "PRESEED_OBS_USERNAME",
    "PRESEED_OBS_PASSWORD",
)
LIVE_SECRET_KEYS = ("PRESEED_WIFI_PASSPHRASE",)
LEGACY_GRUB_SECRET_KEYS = (
    "fruux_username",
    "fruux_password",
    "primary_user",
    "primary_password",
    "primary_gpg_passphrase",
    "root_password",
    "crowdsec_token",
    "tailscale_authkey",
    "telegram_chat_id",
    "telegram_api_key",
    "cf_r2_access_key",
    "cf_r2_secret_key",
    "obs_username",
    "obs_password",
)
CANONICAL_SECRET_KEYS = PRESEED_SECRET_KEYS
CANONICAL_SET = frozenset(CANONICAL_SECRET_KEYS)
RETIRED_CONFIG_SET = frozenset(RETIRED_CONFIG_SECRET_KEYS)
PRESEED_SET = frozenset(PRESEED_SECRET_KEYS)
LEGACY_SET = frozenset(LEGACY_GRUB_SECRET_KEYS)
DIRECT_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
TOKEN_RE = re.compile(r"([^\s]+)|(\s+)", re.DOTALL)
HIDDEN_KEYS = frozenset(CANONICAL_SET - {
    "PRESEED_FRUUX_USERNAME",
    "PRESEED_PRIMARY_USERNAME",
    "PRESEED_TELEGRAM_CHAT_ID",
    "PRESEED_OBS_USERNAME",
})


class SecretsError(RuntimeError):
    """Raised for a safe, value-free user-facing failure."""


def _value_quote(value: str) -> str:
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        return value[0]
    return ""


def _render_tokens(content: str, replacements: dict[str, str] | None) -> str:
    rendered: list[str] = []
    for match in TOKEN_RE.finditer(content):
        token = match.group(0)
        if token.isspace() or "=" not in token:
            rendered.append(token)
            continue
        key, current_value = token.split("=", 1)
        if key in LEGACY_SET or key in RETIRED_CONFIG_SET:
            continue
        if key in CANONICAL_SET:
            quote = _value_quote(current_value)
            replacement = "" if replacements is None else replacements.get(key, "")
            rendered.append(f"{key}={quote}{replacement}{quote}")
        else:
            rendered.append(token)
    return "".join(rendered)


def _render_line(line: str, replacements: dict[str, str] | None) -> str:
    if line.lstrip().startswith("#"):
        return line
    direct = DIRECT_ASSIGNMENT_RE.match(line)
    if direct:
        key, value = direct.groups()
        if key in LEGACY_SET or key in RETIRED_CONFIG_SET:
            return ""
        if key in CANONICAL_SET:
            quote = "'" if key in PRESEED_SET else '"'
            replacement = "" if replacements is None else replacements.get(key, "")
            return f"{key}={quote}{replacement}{quote}"
        outer_quote = _value_quote(value)
        if outer_quote:
            return f"{key}={outer_quote}{_render_tokens(value[1:-1], replacements)}{outer_quote}"
    return _render_tokens(line, replacements)


def _rewritable_optional(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not path.is_symlink() and os.access(path, os.R_OK) and os.access(path.parent, os.W_OK)


def _optional_targets(repo_root: Path) -> list[Path]:
    candidates: set[Path] = set()
    for pattern in (
        "examples/**/debian-usb.conf*",
        "configs/debian-usb.conf.*",
        "examples/**/*.env",
        "initrd/**/*.env.*",
    ):
        candidates.update(repo_root.glob(pattern))
    return sorted(path for path in candidates if _rewritable_optional(path))


def _split_lines(text: str) -> list[str]:
    if not text:
        return []
    return text.splitlines()


def _rewrite_file(
    path: Path,
    *,
    replacements: dict[str, str] | None,
    append_keys: Iterable[str] = (),
    forced_mode: int | None = None,
    dry_run: bool,
) -> None:
    original_stat = path.stat(follow_symlinks=False)
    original_text = path.read_text(encoding="utf-8")
    output_lines: list[str] = []
    seen: set[str] = set()
    append_key_set = frozenset(append_keys)

    for line in _split_lines(original_text):
        direct = DIRECT_ASSIGNMENT_RE.match(line)
        if direct and direct.group(1) in append_key_set:
            seen.add(direct.group(1))
        output_lines.append(_render_line(line, replacements))

    for key in append_keys:
        if key in seen:
            continue
        quote = "'" if key in PRESEED_SET else '"'
        replacement = "" if replacements is None else replacements.get(key, "")
        output_lines.append(f"{key}={quote}{replacement}{quote}")

    rendered = "\n".join(output_lines) + "\n"
    if dry_run:
        return

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, forced_mode if forced_mode is not None else stat.S_IMODE(original_stat.st_mode))
        if (os.geteuid(), os.getegid()) != (original_stat.st_uid, original_stat.st_gid):
            os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_value(key: str, input_stream: TextIO) -> str:
    if input_stream.isatty() and key in HIDDEN_KEYS:
        # Python 3.14 getpass can emit EncodingWarning while wrapping /dev/tty.
        # Limit suppression to that stdlib call so GetPassWarning remains visible.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", EncodingWarning)
            value = getpass.getpass(f"{key}: ")
    else:
        print(f"{key}: ", end="", file=sys.stderr, flush=True)
        raw = input_stream.readline()
        if raw == "":
            raise SecretsError(f"input ended while reading {key}")
        value = raw.rstrip("\n")
    if key != "PRESEED_WIFI_PASSPHRASE" and any(character.isspace() for character in value):
        raise SecretsError(f"{key} must not contain whitespace")
    if key == "PRESEED_WIFI_PASSPHRASE" and any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise SecretsError(f"{key} must not contain control characters")
    if "'" in value or '"' in value:
        raise SecretsError(f"{key} must not contain quote characters")
    return value


def _required_config(repo_root: Path) -> Path:
    path = repo_root / "configs/debian-usb.conf"
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise SecretsError(f"missing active config file: {path}") from exc
    if path.is_symlink():
        raise SecretsError(f"refusing to replace config symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretsError(f"active config path is not a regular file: {path}")
    if not os.access(path, os.R_OK) or not os.access(path.parent, os.W_OK):
        raise SecretsError(f"active config is not safely rewritable: {path}")
    return path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="secrets.sh",
        description="Clear or set managed debian-usb secret fields.",
    )
    parser.add_argument("--repo-root", type=Path, required=True, help=argparse.SUPPRESS)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--clear", action="store_true")
    modes.add_argument("--clear-copies", action="store_true")
    modes.add_argument("--set", dest="set_values", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    repo_root = args.repo_root.resolve()
    replacements: dict[str, str] | None = None
    if args.set_values:
        replacements = {key: _read_value(key, sys.stdin) for key in CANONICAL_SECRET_KEYS}
    elif args.clear:
        replacements = {key: "" for key in CANONICAL_SECRET_KEYS}

    for target in _optional_targets(repo_root):
        _rewrite_file(target, replacements=None, dry_run=args.dry_run)

    if not args.clear_copies:
        config_path = _required_config(repo_root)
        _rewrite_file(
            config_path,
            replacements=replacements,
            append_keys=(),
            dry_run=args.dry_run,
        )
        for path, keys in (
            (repo_root / "initrd/debian/netinst/preseed.env", PRESEED_SECRET_KEYS),
            (repo_root / "initrd/debian/live/live.env", LIVE_SECRET_KEYS),
        ):
            if _rewritable_optional(path):
                _rewrite_file(
                    path,
                    replacements=replacements,
                    append_keys=keys,
                    forced_mode=0o600,
                    dry_run=args.dry_run,
                )

    if args.dry_run:
        print("Rendered applicable managed-secret updates; no changes written.")
    elif args.clear_copies:
        print("Cleared managed values from optional examples and backups that were present.")
    elif args.set_values:
        print("Updated active managed-secret files and cleared optional copies that were present.")
    else:
        print("Cleared active managed-secret files and optional copies that were present.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SecretsError, UnicodeError) as exc:
        print(f"secrets.sh: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
