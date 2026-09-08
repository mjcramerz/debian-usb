#!/usr/bin/python3
"""Shared, standalone Debian/Kali Live NetworkManager Wi-Fi runtime.

This module is copied into the Live root, not the initramfs. Only the validated
live.env and its hand-off script belong in the initramfs. Never source live.env,
put passwords on a command line, or start a second supplicant beside NM.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import ipaddress
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

KEYS = tuple('LIVE_WIFI_' + name for name in (
    'INTERFACE', 'ESSID', 'SECURITY', 'CIDR', 'GATEWAY', 'NAMESERVERS', 'PASSPHRASE'))
CONFIG_PATH = Path('/etc/debian-usb/live.env')
KEYFILE = Path('/etc/NetworkManager/system-connections/debian-usb-wifi.nmconnection')
CONNECTION_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, 'debian-usb/live-wifi'))
WIFI_METRIC = 50
WIRED_METRIC = 600
MAX_ENV_BYTES = 16 * 1024
LAUNCHER = '''#!/bin/sh
# Ask for a new network without putting credentials in shell history or argv.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo /usr/local/lib/debian-usb/live-wifi.py --interactive "$@"
fi
exec /usr/local/lib/debian-usb/live-wifi.py --interactive "$@"
'''


def validate_config(values: dict[str, str]) -> dict[str, str]:
    unknown = set(values) - set(KEYS)
    if unknown:
        raise ValueError('unsupported Live Wi-Fi keys: ' + ', '.join(sorted(unknown)))
    result = {key: values.get(key, '') for key in KEYS}
    for key, value in result.items():
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(key + ' must be text without control characters')
    interface = result['LIVE_WIFI_INTERFACE'] or 'auto'
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', interface):
        raise ValueError('LIVE_WIFI_INTERFACE must be auto or a Linux interface name (up to 15 bytes)')
    result['LIVE_WIFI_INTERFACE'] = interface
    essid = result['LIVE_WIFI_ESSID']
    if len(essid.encode('utf-8')) > 32:
        raise ValueError('LIVE_WIFI_ESSID must not exceed 32 UTF-8 bytes')
    security = result['LIVE_WIFI_SECURITY'] or 'wpa'
    if security not in {'open', 'wpa', 'sae'}:
        raise ValueError('LIVE_WIFI_SECURITY must be open, wpa (WPA2-Personal), or sae (WPA3-Personal)')
    result['LIVE_WIFI_SECURITY'] = security
    cidr = result['LIVE_WIFI_CIDR']
    if cidr:
        if '/' not in cidr:
            raise ValueError('LIVE_WIFI_CIDR requires an IPv4 address/prefix')
        try:
            result['LIVE_WIFI_CIDR'] = str(ipaddress.IPv4Interface(cidr))
        except ValueError as exc:
            raise ValueError('LIVE_WIFI_CIDR must be an IPv4 address/prefix') from exc
    gateway = result['LIVE_WIFI_GATEWAY']
    if gateway:
        try:
            result['LIVE_WIFI_GATEWAY'] = str(ipaddress.IPv4Address(gateway))
        except ValueError as exc:
            raise ValueError('LIVE_WIFI_GATEWAY must be an IPv4 address') from exc
        if not cidr:
            raise ValueError('LIVE_WIFI_GATEWAY requires a static LIVE_WIFI_CIDR; leave both empty for DHCP')
    servers = []
    for server in re.split(r'[\s,]+', result['LIVE_WIFI_NAMESERVERS'].strip()):
        if not server:
            continue
        try:
            address = str(ipaddress.IPv4Address(server))
        except ValueError as exc:
            raise ValueError('LIVE_WIFI_NAMESERVERS must contain IPv4 addresses') from exc
        if address not in servers:
            servers.append(address)
    result['LIVE_WIFI_NAMESERVERS'] = ','.join(servers)
    password = result['LIVE_WIFI_PASSPHRASE']
    length = len(password.encode('utf-8'))
    if not essid or security == 'open':
        result['LIVE_WIFI_PASSPHRASE'] = ''
    elif security == 'wpa' and not (8 <= length <= 63 or re.fullmatch(r'[0-9A-Fa-f]{64}', password)):
        raise ValueError('LIVE_WIFI_PASSPHRASE must be 8 to 63 UTF-8 bytes or a 64-digit hexadecimal PSK for WPA2')
    elif security == 'sae' and not 1 <= length <= 63:
        raise ValueError('LIVE_WIFI_PASSPHRASE must be 1 to 63 UTF-8 bytes for SAE')
    return result


def load_config(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError('Live environment must be a regular file, not a symlink: ' + str(path))
    if path.stat().st_size > MAX_ENV_BYTES:
        raise ValueError('Live environment exceeds 16384 bytes')
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Z][A-Z0-9_]*)=(.*)', line)
        if not match:
            raise ValueError(f'invalid Live environment assignment at line {number}')
        key, value = match.groups()
        if key in values:
            raise ValueError('duplicate Live environment key: ' + key)
        value = value.strip()
        if value[:1] in {'"', "'"}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f'unterminated Live environment value at line {number}')
            value = value[1:-1]
        elif any(c.isspace() for c in value):
            raise ValueError(f'unquoted whitespace at line {number}')
        values[key] = value
    return validate_config(values)


def render_config(config: dict[str, str]) -> str:
    values = validate_config(config)
    # Quotes delimit literal data. Neither shell escapes nor expansions execute.
    return '# Literal Live Wi-Fi data; NEVER source this file.\n' + ''.join(
        f"{key}='{values[key]}'\n" for key in KEYS)


def keyfile_string(value: str) -> str:
    # GLib keyfile escaping (not shell escaping). Escape all spaces so trailing
    # and leading whitespace in passphrases round-trip without interpretation.
    return value.replace('\\', '\\\\').replace(' ', r'\s')


def render_keyfile(config: dict[str, str], interface: str) -> str:
    c = validate_config(config)
    lines = ['[connection]', 'id=debian-usb-wifi', 'uuid=' + CONNECTION_UUID,
             'type=wifi', 'interface-name=' + interface, 'autoconnect=true',
             'autoconnect-priority=999', 'autoconnect-retries=2', '', '[wifi]',
             'mode=infrastructure',
             # A byte array avoids ambiguous strings such as "1;2;" and preserves
             # arbitrary UTF-8, spaces, punctuation and backslashes in an SSID.
             'ssid=' + ''.join(str(b) + ';' for b in c['LIVE_WIFI_ESSID'].encode('utf-8'))]
    if c['LIVE_WIFI_SECURITY'] != 'open':
        lines += ['', '[wifi-security]', 'key-mgmt=' + ('sae' if c['LIVE_WIFI_SECURITY'] == 'sae' else 'wpa-psk'),
                  'proto=rsn;', 'psk=' + keyfile_string(c['LIVE_WIFI_PASSPHRASE']), 'psk-flags=0']
        if c['LIVE_WIFI_SECURITY'] == 'sae':
            lines += ['pmf=3']
    lines += ['', '[ipv4]', 'method=' + ('manual' if c['LIVE_WIFI_CIDR'] else 'auto'),
              f'route-metric={WIFI_METRIC}', 'dns-priority=-50']
    if c['LIVE_WIFI_CIDR']:
        lines += ['address1=' + c['LIVE_WIFI_CIDR']]
        if c['LIVE_WIFI_GATEWAY']:
            lines += ['gateway=' + c['LIVE_WIFI_GATEWAY']]
    if c['LIVE_WIFI_NAMESERVERS']:
        lines += ['dns=' + c['LIVE_WIFI_NAMESERVERS'].replace(',', ';') + ';', 'ignore-auto-dns=true']
    lines += ['dns-search=~.;', '', '[ipv6]', 'method=auto', f'route-metric={WIFI_METRIC}', 'dns-priority=-50']
    if c['LIVE_WIFI_NAMESERVERS']:
        lines += ['ignore-auto-dns=true']
    return '\n'.join(lines) + '\n'


def write_private(path: Path, text: str) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('refusing non-regular configuration destination: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def nmcli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(['nmcli', *args], stdin=subprocess.DEVNULL, text=True,
                            encoding='utf-8', capture_output=True, timeout=45,
                            env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
    if check and result.returncode:
        # Do not propagate stdout/stderr: a backend error might echo secrets.
        raise RuntimeError('NetworkManager could not complete ' + args[0])
    return result


def wifi_interfaces() -> list[str]:
    result = nmcli('-t', '--escape', 'no', '-f', 'DEVICE,TYPE', 'device', 'status')
    return [line.rsplit(':', 1)[0] for line in result.stdout.splitlines()
            if line.rsplit(':', 1)[-1] == 'wifi']


def prioritize_wifi() -> None:
    result = nmcli('-t', '--escape', 'no', '-f', 'UUID,TYPE,DEVICE', 'connection', 'show', '--active')
    records = [line.split(':', 2) for line in result.stdout.splitlines()]
    if not any(len(row) == 3 and row[0] == CONNECTION_UUID for row in records):
        return
    # Raise only Ethernet metrics, never VPN, tunnel or manually routed links.
    # Temporary NM changes survive DHCP renewals for this Live session without
    # altering the user's stored wired connection profile on disk.
    for row in records:
        if len(row) != 3 or row[1] not in {'802-3-ethernet', 'ethernet'} or row[2] in {'', '--'}:
            continue
        uid, _, device = row
        metrics = nmcli('-g', 'ipv4.route-metric,ipv6.route-metric', 'connection', 'show', 'uuid', uid).stdout.splitlines()
        if metrics == [str(WIRED_METRIC), str(WIRED_METRIC)]:
            continue  # Avoid a dispatcher/reapply event loop.
        nmcli('connection', 'modify', '--temporary', 'uuid', uid,
              'ipv4.route-metric', str(WIRED_METRIC), 'ipv6.route-metric', str(WIRED_METRIC))
        nmcli('device', 'reapply', device)


def connect(config: dict[str, str], keyfile: Path = KEYFILE) -> bool:
    c = validate_config(config)
    if not c['LIVE_WIFI_ESSID']:
        return False
    for attempt in range(10):
        status = nmcli('-t', '-f', 'RUNNING', 'general', check=False)
        if status.returncode == 0 and status.stdout.strip() == 'running':
            break
        if attempt == 9:
            raise RuntimeError('NetworkManager is not ready; Ethernet is unchanged')
        time.sleep(1)
    nmcli('radio', 'wifi', 'on')
    interfaces = wifi_interfaces()
    requested = c['LIVE_WIFI_INTERFACE']
    if requested != 'auto':
        interfaces = [name for name in interfaces if name == requested]
    for interface in interfaces:
        # Exact ESSID match; no grep patterns, no escaping ambiguity, and no
        # attempt to disconnect the current network before seeing the new AP.
        scan = nmcli('-t', '--escape', 'no', '-f', 'SSID', 'device', 'wifi', 'list',
                     'ifname', interface, '--rescan', 'yes', check=False)
        if scan.returncode or c['LIVE_WIFI_ESSID'] not in scan.stdout.splitlines():
            continue
        previous = keyfile.read_text(encoding='utf-8') if keyfile.is_file() and not keyfile.is_symlink() else None
        write_private(keyfile, render_keyfile(c, interface))
        try:
            nmcli('connection', 'load', str(keyfile))
            nmcli('--wait', '30', 'connection', 'up', 'uuid', CONNECTION_UUID, 'ifname', interface)
        except (RuntimeError, subprocess.TimeoutExpired):
            if previous is None:
                nmcli('connection', 'delete', 'uuid', CONNECTION_UUID, check=False)
                keyfile.unlink(missing_ok=True)
            else:
                write_private(keyfile, previous)
                nmcli('connection', 'load', str(keyfile), check=False)
            raise RuntimeError('Wi-Fi activation failed; existing Ethernet connections were not disconnected') from None
        prioritize_wifi()
        return True
    return False


def interactive_config() -> dict[str, str]:
    print('Live Wi-Fi setup (WPA2/WPA3-Personal or open; IPv4 DHCP/static).')
    print('Existing Ethernet stays available as fallback. Wi-Fi DNS takes priority while connected.')
    available = wifi_interfaces()
    print('Wi-Fi interfaces: ' + (', '.join(available) or 'none detected'))
    values = {key: '' for key in KEYS}
    values['LIVE_WIFI_INTERFACE'] = input('Interface [auto]: ').strip() or 'auto'
    values['LIVE_WIFI_ESSID'] = input('ESSID (exact network name): ')
    if not values['LIVE_WIFI_ESSID']:
        raise ValueError('ESSID is required')
    security = input('Security: 1=open, 2=WPA2-Personal, 3=WPA3-Personal [2]: ').strip() or '2'
    values['LIVE_WIFI_SECURITY'] = {'1': 'open', '2': 'wpa', '3': 'sae'}.get(security, security)
    values['LIVE_WIFI_CIDR'] = input('IPv4 CIDR [empty for DHCP]: ').strip()
    if values['LIVE_WIFI_CIDR']:
        values['LIVE_WIFI_GATEWAY'] = input('IPv4 gateway [empty for no default route]: ').strip()
    values['LIVE_WIFI_NAMESERVERS'] = input('IPv4 nameservers, comma-separated [automatic]: ').strip()
    if values['LIVE_WIFI_SECURITY'] != 'open':
        values['LIVE_WIFI_PASSPHRASE'] = getpass.getpass('Wi-Fi passphrase (hidden): ')
    return validate_config(values)


def install_home_launchers() -> None:
    for account in pwd.getpwall():
        home = Path(account.pw_dir)
        if not (1000 <= account.pw_uid < 65534 and home.parent == Path('/home')):
            continue
        if home.is_symlink() or not home.is_dir():
            continue
        path = home / 'wifi-connect.sh'
        try:
            # Never overwrite or follow an existing file in a user-writable home.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
        except FileExistsError:
            continue
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(LAUNCHER)
            os.fchown(stream.fileno(), account.pw_uid, account.pw_gid)
            os.fchmod(stream.fileno(), 0o755)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--interactive', action='store_true')
    group.add_argument('--boot', action='store_true')
    group.add_argument('--priority', action='store_true')
    parser.add_argument('--env', type=Path)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('run through sudo or use ~/wifi-connect.sh')
    if not shutil.which('nmcli'):
        print('live-wifi: NetworkManager is missing; rebuild with required runtime packages', file=sys.stderr)
        return 1
    state = Path('/run/debian-usb-live-wifi')
    if state.is_symlink():
        raise ValueError('refusing symlinked Wi-Fi state directory')
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    with (state / 'lock').open('a', encoding='utf-8') as lock:
        # Dispatcher calls must not deadlock NM while connection activation waits
        # for its dispatcher to finish. The startup/manual caller holds this lock.
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (fcntl.LOCK_NB if args.priority else 0))
        except BlockingIOError:
            return 0
        try:
            if args.priority:
                prioritize_wifi()
                return 0
            install_home_launchers()
            if args.boot and (state / 'boot-complete').exists():
                return 0
            if args.interactive:
                config = interactive_config()
            else:
                source = args.env or next((p for p in (CONFIG_PATH, Path('/run/initramfs/debian-usb/live.env')) if p.is_file()), CONFIG_PATH)
                config = load_config(source)
            connected = connect(config)
            if connected:
                if args.interactive:
                    write_private(CONFIG_PATH, render_config(config))
                (state / 'boot-complete').touch(mode=0o600)
                print('live-wifi: connected; Wi-Fi is preferred, Ethernet remains available')
                return 0
            print('live-wifi: no configured visible ESSID; Ethernet and existing links are unchanged')
            return 1 if args.interactive else 0
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, EOFError) as exc:
            print('live-wifi: ' + str(exc), file=sys.stderr)
            return 1 if args.interactive or args.priority else 0
        except KeyboardInterrupt:
            print('\nlive-wifi: cancelled', file=sys.stderr)
            return 130


if __name__ == '__main__':
    raise SystemExit(main())
