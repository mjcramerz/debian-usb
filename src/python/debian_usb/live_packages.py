#!/usr/bin/python3
"""Resolve optional Live packages inside the target distro, never the build host.

Required packages are handled by the caller's strict APT transaction. A missing
optional package is reported, but signature, dependency and installation errors
are fatal. No repository or trust configuration is changed here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

PACKAGE_RE = re.compile(r'^[a-z0-9][a-z0-9+.-]*$')


def candidate_available(output: str) -> bool:
    match = re.search(r'^\s*Candidate:\s*(\S+)', output, re.MULTILINE)
    return bool(match and match.group(1) != '(none)')


def install_optional(packages: list[str], *, apt_options: list[str] | None = None,
                     update: bool = False, report_path: Path = Path('/var/log/debian-usb/optional-packages.json')) -> dict:
    requested = list(dict.fromkeys(packages))
    if any(not PACKAGE_RE.fullmatch(package) for package in requested):
        raise ValueError('invalid optional package name')
    options = apt_options or []
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'LANG': 'C',
           'DEBIAN_FRONTEND': 'noninteractive'}
    if update:
        subprocess.run(['apt-get', *options, '-o', 'APT::Update::Error-Mode=any', 'update'], env=env, check=True)
    available, unavailable = [], []
    for package in requested:
        result = subprocess.run(['apt-cache', *options, 'policy', package], env=env,
                                check=True, capture_output=True, text=True, encoding='utf-8')
        (available if candidate_available(result.stdout) else unavailable).append(package)
    report = {'requested': requested, 'available': available, 'unavailable': unavailable,
              'installed': [], 'status': 'resolved'}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    def save() -> None:
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    save()
    for package in unavailable:
        print('debian-usb: optional package unavailable in target repositories: ' + package, flush=True)
    try:
        if available:
            # An absent package may be skipped; unsatisfied dependencies may not.
            subprocess.run(['apt-get', *options, '-s', 'install', '--no-install-recommends', *available], env=env, check=True)
            subprocess.run(['apt-get', *options, 'install', '-y', '--no-install-recommends', *available], env=env, check=True)
            verified = subprocess.run(['dpkg-query', '-W', '-f=${binary:Package}\t${Status}\n', *available],
                                      env=env, check=True, capture_output=True, text=True, encoding='utf-8')
            installed = {line.split('\t', 1)[0].split(':')[0] for line in verified.stdout.splitlines()
                         if line.endswith('\tinstall ok installed')}
            if not set(available) <= installed:
                raise RuntimeError('APT did not fully configure all resolved optional packages')
            report['installed'] = available
        report['status'] = 'complete'
    except BaseException:
        report['status'] = 'failed'
        save()
        raise
    save()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', action='append', default=[])
    parser.add_argument('--apt-list', default='')
    parser.add_argument('--apt-parts', default='')
    parser.add_argument('--update', action='store_true')
    args = parser.parse_args()
    options = []
    if args.apt_list:
        options += ['-o', 'Dir::Etc::sourcelist=' + args.apt_list]
    if args.apt_parts:
        options += ['-o', 'Dir::Etc::sourceparts=' + args.apt_parts]
    install_optional(args.package, apt_options=options, update=args.update)


if __name__ == '__main__':
    main()
