#!/usr/bin/env python3
"""Fail closed when managed debian-usb secret fields would be pushed."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys
from typing import Iterable


CONFIG_SECRET_KEYS = frozenset({"DEFAULT_LIVE_WIFI_PSK"})
PRESEED_SECRET_KEYS = frozenset(
    {
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
    }
)
LEGACY_GRUB_SECRET_KEYS = frozenset(
    {
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
    }
)
CANONICAL_SECRET_KEYS = CONFIG_SECRET_KEYS | PRESEED_SECRET_KEYS
ALL_SECRET_KEYS = CANONICAL_SECRET_KEYS | LEGACY_GRUB_SECRET_KEYS
ASSIGNMENT_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<key>{'|'.join(re.escape(key) for key in sorted(ALL_SECRET_KEYS, key=len, reverse=True))})="
)
OID_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


@dataclass(frozen=True, order=True)
class Violation:
    scope: str
    path: str
    key: str


def _git(*args: str, input_text: str | None = None) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        input=input_text.encode("utf-8") if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {detail or 'unknown error'}")
    return completed.stdout


def _managed_secret_path(path: str) -> bool:
    normalized = PurePosixPath(path)
    parts = normalized.parts
    if not parts:
        return False
    rendered = normalized.as_posix()
    if rendered == "configs/debian-usb.conf" or rendered.startswith("configs/debian-usb.conf."):
        return True
    if parts[0] == "examples":
        return normalized.name.startswith("debian-usb.conf") or ".env" in normalized.name
    if parts[0] == "initrd":
        return ".env" in normalized.name
    return False


def _assignment_has_value(line: str, value_start: int) -> bool:
    remainder = line[value_start:]
    if not remainder:
        return False
    if remainder[0] in {"'", '"'}:
        quote = remainder[0]
        closing = remainder.find(quote, 1)
        if closing == -1:
            return bool(remainder[1:].strip())
        return bool(remainder[1:closing])
    token = remainder.split(maxsplit=1)[0]
    return bool(token)


def _secret_keys_in_blob(blob: bytes, path: str) -> frozenset[str]:
    if not _managed_secret_path(path):
        return frozenset()
    text = blob.decode("utf-8", errors="replace")
    found: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in ASSIGNMENT_RE.finditer(line):
            key = match.group("key")
            if key in LEGACY_GRUB_SECRET_KEYS or _assignment_has_value(line, match.end()):
                found.add(key)
    return frozenset(found)


def _blob(oid: str, cache: dict[str, bytes]) -> bytes:
    if oid not in cache:
        cache[oid] = _git("cat-file", "blob", oid)
    return cache[oid]


def _parse_tree_record(record: bytes) -> tuple[str, str, str, str]:
    metadata, separator, raw_path = record.partition(b"\t")
    if not separator:
        raise RuntimeError("could not parse Git tree entry")
    fields = metadata.decode("ascii", errors="strict").split()
    if len(fields) != 3:
        raise RuntimeError("could not parse Git tree metadata")
    mode, object_type, oid = fields
    return mode, object_type, oid, os.fsdecode(raw_path)


def _index_violations(blob_cache: dict[str, bytes]) -> list[Violation]:
    violations: list[Violation] = []
    for record in _git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise RuntimeError("could not parse Git index entry")
        fields = metadata.decode("ascii", errors="strict").split()
        if len(fields) != 3:
            raise RuntimeError("could not parse Git index metadata")
        _mode, oid, stage = fields
        path = os.fsdecode(raw_path)
        if not _managed_secret_path(path):
            continue
        scope = "index" if stage == "0" else f"index(stage {stage})"
        for key in _secret_keys_in_blob(_blob(oid, blob_cache), path):
            violations.append(Violation(scope, path, key))
    return violations


def _commit_violations(commits: Iterable[str], blob_cache: dict[str, bytes]) -> list[Violation]:
    violations: list[Violation] = []
    key_cache: dict[tuple[str, str], frozenset[str]] = {}
    for commit in commits:
        if not OID_RE.fullmatch(commit):
            raise RuntimeError("Git returned an invalid commit object ID")
        scope = f"outgoing {commit[:12]}"
        for record in _git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not record:
                continue
            _mode, object_type, oid, path = _parse_tree_record(record)
            if object_type != "blob" or not _managed_secret_path(path):
                continue
            cache_key = (oid, path)
            if cache_key not in key_cache:
                key_cache[cache_key] = _secret_keys_in_blob(_blob(oid, blob_cache), path)
            for key in key_cache[cache_key]:
                violations.append(Violation(scope, path, key))
    return violations


def _is_zero_oid(value: str) -> bool:
    return bool(value) and set(value) == {"0"}


def _outgoing_commits(updates: Iterable[str]) -> list[str]:
    commits: set[str] = set()
    for line_number, line in enumerate(updates, start=1):
        fields = line.split()
        if len(fields) != 4:
            raise RuntimeError(f"invalid pre-push update record at line {line_number}")
        _local_ref, local_oid, _remote_ref, remote_oid = fields
        if _is_zero_oid(local_oid):
            continue
        if not OID_RE.fullmatch(local_oid):
            raise RuntimeError(f"invalid local object ID at pre-push line {line_number}")
        if _is_zero_oid(remote_oid):
            revision_args = ("rev-list", local_oid)
        else:
            if not OID_RE.fullmatch(remote_oid):
                raise RuntimeError(f"invalid remote object ID at pre-push line {line_number}")
            revision_args = ("rev-list", local_oid, "--not", remote_oid)
        for commit in _git(*revision_args).decode("ascii", errors="strict").splitlines():
            if commit:
                commits.add(commit)
    return sorted(commits)


def _report(violations: Iterable[Violation]) -> int:
    unique = sorted(set(violations))
    if not unique:
        return 0
    print("debian-usb secret guard: refusing push; managed secrets are not empty", file=sys.stderr)
    for violation in unique:
        print(f"  {violation.scope} {violation.path}: {violation.key}", file=sys.stderr)
    print("Run ./secrets.sh --clear, stage the sanitized files, and remove secret-bearing outgoing commits.", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--index", action="store_true", help="scan the current Git index")
    mode.add_argument("--pre-push", action="store_true", help="scan the index and pre-push updates from stdin")
    mode.add_argument("--commit", action="append", default=[], metavar="OID", help="scan one commit snapshot")
    args = parser.parse_args()

    try:
        blob_cache: dict[str, bytes] = {}
        violations = _index_violations(blob_cache) if (args.index or args.pre_push) else []
        if args.pre_push:
            commits = _outgoing_commits(sys.stdin.read().splitlines())
            violations.extend(_commit_violations(commits, blob_cache))
        elif args.commit:
            violations.extend(_commit_violations(args.commit, blob_cache))
        return _report(violations)
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f"debian-usb secret guard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
