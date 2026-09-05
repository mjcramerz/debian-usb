#!/usr/bin/env python3
"""Manage debian-usb secret fields without exposing values in diagnostics."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
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
DIRECT_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<separator>\s*=\s*)"
    r"(?P<value>.*)$",
    re.DOTALL,
)
TOKEN_RE = re.compile(r"([^\s]+)|(\s+)", re.DOTALL)
ASSIGNMENT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
INITRD_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:"
    r"ACCESS_KEY|API_KEY|AUTHKEY|AUTH_KEY|BEARER|CHAT_ID|CLIENT_SECRET|"
    r"CONNECTION_STRING|COOKIE|CREDENTIAL|CREDENTIALS|DATABASE_URL|DB_URL|"
    r"DSN|ENCRYPTION_KEY|KEY|PASS|PASSCODE|PASSPHRASE|PASSWD|PASSWORD|"
    r"PRIVATE_KEY|PSK|SECRET|SECRET_KEY|SESSION_KEY|SIGNING_KEY|TOKEN|"
    r"USERNAME|WEBHOOK_URL"
    r")(?:$|_)",
    re.IGNORECASE,
)
INITRD_PUBLIC_KEY_RE = re.compile(
    r"(?:^|_)(?:GPG_KEY|KEY_ALGORITHM|KEY_BITS|KEY_FILE|KEY_ID|KEY_PATH|"
    r"KEY_TYPE|KEY_URL|KEYRING|PUBLIC_KEY)$",
    re.IGNORECASE,
)
GIT_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
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


def _value_has_content(value: str) -> bool:
    stripped = value.strip()
    quote = _value_quote(stripped)
    if quote:
        return bool(stripped[1:-1])
    return bool(stripped)


def _split_assignment_value(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        escaped = False
        for index, character in enumerate(value[1:], start=1):
            if character == quote and not escaped:
                return value[: index + 1], value[index + 1 :]
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
    comment = re.search(r"(?:^|\s)(?:#|;)", value)
    if comment:
        return value[: comment.start()].rstrip(), value[comment.start() :]
    return value.rstrip(), value[len(value.rstrip()) :]


def _is_initrd_secret_key(key: str) -> bool:
    if key in CANONICAL_SET:
        return True
    if INITRD_PUBLIC_KEY_RE.search(key):
        return False
    return bool(INITRD_SECRET_KEY_RE.search(key))


def _is_initrd_secret_file(path: str | PurePosixPath) -> bool:
    normalized = PurePosixPath(path)
    if not normalized.parts or normalized.parts[0] != "initrd":
        return False
    name = normalized.name
    return (
        name.endswith(".env")
        or ".env." in name
        or name.endswith(".conf")
        or ".conf." in name
    )


def _render_tokens(
    content: str,
    replacements: dict[str, str] | None,
    *,
    detect_initrd_secrets: bool,
    cleared_keys: set[str],
) -> str:
    rendered: list[str] = []
    for match in TOKEN_RE.finditer(content):
        token = match.group(0)
        if token.isspace() or "=" not in token:
            rendered.append(token)
            continue
        key, current_value = token.split("=", 1)
        if not ASSIGNMENT_KEY_RE.fullmatch(key):
            rendered.append(token)
            continue
        if key in LEGACY_SET or key in RETIRED_CONFIG_SET:
            if _value_has_content(current_value):
                cleared_keys.add(key)
            continue
        if key in CANONICAL_SET or (detect_initrd_secrets and _is_initrd_secret_key(key)):
            quote = _value_quote(current_value)
            replacement = "" if replacements is None else replacements.get(key, "")
            if not replacement and _value_has_content(current_value):
                cleared_keys.add(key)
            rendered.append(f"{key}={quote}{replacement}{quote}")
        else:
            rendered.append(token)
    return "".join(rendered)


def _render_line(
    line: str,
    replacements: dict[str, str] | None,
    *,
    detect_initrd_secrets: bool,
    cleared_keys: set[str],
) -> str:
    if line.lstrip().startswith("#"):
        return line
    direct = DIRECT_ASSIGNMENT_RE.match(line)
    if direct:
        prefix = direct.group("prefix")
        key = direct.group("key")
        separator = direct.group("separator")
        value = direct.group("value")
        assignment_value, suffix = _split_assignment_value(value)
        if key in LEGACY_SET or key in RETIRED_CONFIG_SET:
            if _value_has_content(assignment_value):
                cleared_keys.add(key)
            return ""
        if key in CANONICAL_SET or (detect_initrd_secrets and _is_initrd_secret_key(key)):
            if key in PRESEED_SET:
                quote = "'"
            elif detect_initrd_secrets:
                quote = _value_quote(assignment_value.strip()) or "'"
            else:
                quote = '"'
            replacement = "" if replacements is None else replacements.get(key, "")
            if not replacement and _value_has_content(assignment_value):
                cleared_keys.add(key)
            return f"{prefix}{key}{separator}{quote}{replacement}{quote}{suffix}"
        outer_quote = _value_quote(value)
        if outer_quote:
            rendered = _render_tokens(
                value[1:-1],
                replacements,
                detect_initrd_secrets=detect_initrd_secrets,
                cleared_keys=cleared_keys,
            )
            return f"{prefix}{key}{separator}{outer_quote}{rendered}{outer_quote}"
    return _render_tokens(
        line,
        replacements,
        detect_initrd_secrets=detect_initrd_secrets,
        cleared_keys=cleared_keys,
    )


def _rewritable_optional(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and os.access(path, os.R_OK)
        and os.access(path.parent, os.W_OK)
    )


def _initrd_targets(repo_root: Path) -> list[Path]:
    initrd_root = repo_root / "initrd"
    try:
        metadata = initrd_root.lstat()
    except FileNotFoundError:
        return []
    if initrd_root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        return []

    targets: list[Path] = []
    for current_root, directory_names, file_names in os.walk(initrd_root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = sorted(
            name for name in directory_names if not (current / name).is_symlink()
        )
        for name in sorted(file_names):
            path = current / name
            relative = path.relative_to(repo_root)
            if _is_initrd_secret_file(relative) and _rewritable_optional(path):
                targets.append(path)
    return targets


def _optional_targets(repo_root: Path) -> list[Path]:
    candidates: set[Path] = set()
    for pattern in (
        "examples/**/debian-usb.conf*",
        "configs/debian-usb.conf.*",
        "examples/**/*.env",
    ):
        candidates.update(repo_root.glob(pattern))
    candidates.update(_initrd_targets(repo_root))
    candidates.difference_update(
        {
            repo_root / "initrd/debian/netinst/preseed.env",
            repo_root / "initrd/debian/live/live.env",
        }
    )
    return sorted(path for path in candidates if _rewritable_optional(path))


def _split_lines(text: str) -> list[str]:
    if not text:
        return []
    return text.splitlines()


def _render_text(
    original_text: str,
    *,
    replacements: dict[str, str] | None,
    append_keys: Iterable[str] = (),
    detect_initrd_secrets: bool,
) -> tuple[str, frozenset[str]]:
    output_lines: list[str] = []
    seen: set[str] = set()
    cleared_keys: set[str] = set()
    append_key_set = frozenset(append_keys)

    for line in _split_lines(original_text):
        direct = DIRECT_ASSIGNMENT_RE.match(line)
        if direct and direct.group("key") in append_key_set:
            seen.add(direct.group("key"))
        output_lines.append(
            _render_line(
                line,
                replacements,
                detect_initrd_secrets=detect_initrd_secrets,
                cleared_keys=cleared_keys,
            )
        )

    for key in append_keys:
        if key in seen:
            continue
        quote = "'" if key in PRESEED_SET else '"'
        replacement = "" if replacements is None else replacements.get(key, "")
        output_lines.append(f"{key}={quote}{replacement}{quote}")

    rendered = "\n".join(output_lines) + "\n"
    return rendered, frozenset(cleared_keys)


def _rewrite_file(
    path: Path,
    *,
    replacements: dict[str, str] | None,
    append_keys: Iterable[str] = (),
    forced_mode: int | None = None,
    detect_initrd_secrets: bool = False,
    write_only_if_cleared: bool = False,
    dry_run: bool,
) -> frozenset[str]:
    original_stat = path.stat(follow_symlinks=False)
    original_text = path.read_text(encoding="utf-8")
    rendered, cleared_keys = _render_text(
        original_text,
        replacements=replacements,
        append_keys=append_keys,
        detect_initrd_secrets=detect_initrd_secrets,
    )
    if dry_run or (write_only_if_cleared and not cleared_keys):
        return cleared_keys

    desired_mode = forced_mode if forced_mode is not None else stat.S_IMODE(original_stat.st_mode)
    if rendered == original_text and desired_mode == stat.S_IMODE(original_stat.st_mode):
        return cleared_keys

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, desired_mode)
        if (os.geteuid(), os.getegid()) != (original_stat.st_uid, original_stat.st_gid):
            os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return cleared_keys


def _git(repo_root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        env=environment,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SecretsError(f"git {args[0]} failed: {detail or 'unknown error'}")
    return completed.stdout


def _clear_initrd_index(repo_root: Path, *, dry_run: bool) -> set[tuple[str, str, str]]:
    changes: set[tuple[str, str, str]] = set()
    records = _git(repo_root, "ls-files", "--stage", "-z", "--", "initrd").split(b"\0")
    for record in records:
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise SecretsError("could not parse Git index entry")
        fields = metadata.decode("ascii", errors="strict").split()
        if len(fields) != 3:
            raise SecretsError("could not parse Git index metadata")
        mode, object_id, stage = fields
        path = os.fsdecode(raw_path)
        if stage != "0" or mode not in {"100644", "100755"} or not _is_initrd_secret_file(path):
            continue
        if not GIT_OID_RE.fullmatch(object_id):
            raise SecretsError("Git index contains an invalid object ID")

        original = _git(repo_root, "cat-file", "blob", object_id).decode("utf-8")
        rendered, cleared_keys = _render_text(
            original,
            replacements=None,
            detect_initrd_secrets=True,
        )
        changes.update(("index", path, key) for key in cleared_keys)
        if not cleared_keys or rendered == original or dry_run:
            continue

        new_object_id = _git(
            repo_root,
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=rendered.encode("utf-8"),
        ).decode("ascii", errors="strict").strip()
        if not GIT_OID_RE.fullmatch(new_object_id):
            raise SecretsError("git hash-object returned an invalid object ID")
        _git(repo_root, "update-index", "--cacheinfo", mode, new_object_id, path)
    return changes


def _clear_initrd(
    repo_root: Path,
    *,
    include_index: bool,
    dry_run: bool,
) -> set[tuple[str, str, str]]:
    changes: set[tuple[str, str, str]] = set()
    for path in _initrd_targets(repo_root):
        cleared_keys = _rewrite_file(
            path,
            replacements=None,
            detect_initrd_secrets=True,
            write_only_if_cleared=True,
            dry_run=dry_run,
        )
        relative = path.relative_to(repo_root).as_posix()
        changes.update(("worktree", relative, key) for key in cleared_keys)
    if include_index:
        changes.update(_clear_initrd_index(repo_root, dry_run=dry_run))
    return changes


def _report_cleared(
    changes: Iterable[tuple[str, str, str]],
    *,
    dry_run: bool,
) -> None:
    unique = sorted(set(changes))
    if not unique:
        print("No non-empty managed secret values found in applicable files.")
        return
    verb = "Would clear" if dry_run else "Cleared"
    for scope, path, key in unique:
        safe_path = path.encode("ascii", errors="backslashreplace").decode("ascii")
        print(f"{verb} {key} in {scope} {safe_path}.")


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
    modes.add_argument(
        "--clear-initrd",
        action="store_true",
        help="clear secret assignments in initrd .env and .conf files",
    )
    modes.add_argument("--set", dest="set_values", action="store_true")
    parser.add_argument(
        "--index",
        action="store_true",
        help="also clear matching Git index entries (requires --clear-initrd)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.index and not args.clear_initrd:
        parser.error("--index requires --clear-initrd")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    repo_root = args.repo_root.resolve()
    if args.clear_initrd:
        changes = _clear_initrd(
            repo_root,
            include_index=args.index,
            dry_run=args.dry_run,
        )
        _report_cleared(changes, dry_run=args.dry_run)
        if args.dry_run:
            print("Inspected applicable initrd files; no changes written.")
        elif changes:
            print("Cleared secret values from applicable initrd .env and .conf files.")
        else:
            print("No initrd .env or .conf changes were needed.")
        return 0

    replacements: dict[str, str] | None = None
    if args.set_values:
        replacements = {key: _read_value(key, sys.stdin) for key in CANONICAL_SECRET_KEYS}
    elif args.clear:
        replacements = {key: "" for key in CANONICAL_SECRET_KEYS}

    changes: set[tuple[str, str, str]] = set()
    for target in _optional_targets(repo_root):
        relative = target.relative_to(repo_root).as_posix()
        cleared_keys = _rewrite_file(
            target,
            replacements=None,
            detect_initrd_secrets=_is_initrd_secret_file(relative),
            dry_run=args.dry_run,
        )
        changes.update(("worktree", relative, key) for key in cleared_keys)

    if not args.clear_copies:
        config_path = _required_config(repo_root)
        cleared_keys = _rewrite_file(
            config_path,
            replacements=replacements,
            append_keys=(),
            dry_run=args.dry_run,
        )
        relative = config_path.relative_to(repo_root).as_posix()
        changes.update(("worktree", relative, key) for key in cleared_keys)
        for target, keys in (
            (repo_root / "initrd/debian/netinst/preseed.env", PRESEED_SECRET_KEYS),
            (repo_root / "initrd/debian/live/live.env", LIVE_SECRET_KEYS),
        ):
            if _rewritable_optional(target):
                cleared_keys = _rewrite_file(
                    target,
                    replacements=replacements,
                    append_keys=keys,
                    forced_mode=0o600,
                    detect_initrd_secrets=True,
                    dry_run=args.dry_run,
                )
                relative = target.relative_to(repo_root).as_posix()
                changes.update(("worktree", relative, key) for key in cleared_keys)

    if changes or not args.set_values:
        _report_cleared(changes, dry_run=args.dry_run)
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
