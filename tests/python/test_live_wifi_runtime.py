"""Shared Live runtime regressions; never operates on real network devices."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from debian_usb import live_wifi as wifi
from debian_usb import live_hooks


def config(**changes):
    values = {'LIVE_WIFI_ESSID': 'Lab: secure', 'LIVE_WIFI_PASSPHRASE': 'test-only-passphrase'}
    values.update({'LIVE_WIFI_' + key: value for key, value in changes.items()})
    return wifi.validate_config(values)


class WifiDataTests(unittest.TestCase):
    def test_empty_template_is_valid_and_does_not_connect(self):
        with patch.object(wifi, 'nmcli') as nm:
            self.assertFalse(wifi.connect(wifi.validate_config({})))
        nm.assert_not_called()

    def test_literal_quotes_expansions_and_backslashes_round_trip(self):
        for name in ("owner's lab", 'Lab: secure', '1;2;3;', 'caf\u00e9', r'a\b'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                values = config(ESSID=name, PASSPHRASE="  a'\"$()`\\;valid password  ")
                path = Path(tmp) / 'live.env'
                path.write_text(wifi.render_config(values), encoding='utf-8')
                with patch('subprocess.run') as execute:
                    self.assertEqual(wifi.load_config(path), values)
                execute.assert_not_called()

    def test_rejects_duplicate_unknown_and_shell_statements(self):
        for text in ('LIVE_WIFI_ESSID=x\nLIVE_WIFI_ESSID=y\n', 'UNKNOWN=1\n',
                     'export LIVE_WIFI_ESSID=x\n', 'touch /tmp/never\n', 'LIVE_WIFI_ESSID="bad\n'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'live.env'
                path.write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    wifi.load_config(path)

    def test_rejects_symlink_and_oversize_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data'
            path.write_text('x' * (wifi.MAX_ENV_BYTES + 1), encoding='utf-8')
            link = Path(tmp) / 'link'
            link.symlink_to(path)
            for item in (path, link):
                with self.assertRaises(ValueError):
                    wifi.load_config(item)

    def test_security_address_and_control_validation(self):
        invalid = [dict(SECURITY='wep'), dict(PASSPHRASE='short'), dict(PASSPHRASE='z' * 64),
                   dict(ESSID='\u00e4' * 17), dict(ESSID='bad\x00name'), dict(INTERFACE='bad/name'),
                   dict(CIDR='10.0.0.2'), dict(CIDR='10.0.0.2/99'), dict(GATEWAY='10.0.0.1'),
                   dict(NAMESERVERS='8.8.8.8;echo'), dict(SECURITY='sae', PASSPHRASE='')]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                config(**values)

    def test_open_and_sae_profiles(self):
        opened = wifi.render_keyfile(config(SECURITY='open'), 'wlan0')
        self.assertNotIn('[wifi-security]', opened)
        sae = wifi.render_keyfile(config(SECURITY='sae', PASSPHRASE='x'), 'wlan0')
        self.assertIn('key-mgmt=sae\n', sae)
        self.assertIn('pmf=3\n', sae)
        self.assertEqual(config(PASSPHRASE='a' * 64)['LIVE_WIFI_PASSPHRASE'], 'a' * 64)

    def test_keyfile_static_dns_metrics_and_byte_ssid(self):
        values = config(ESSID='a:b', CIDR='192.0.2.50/24', GATEWAY='192.0.2.1',
                        NAMESERVERS='192.0.2.53, 192.0.2.53 192.0.2.54')
        text = wifi.render_keyfile(values, 'wlan0')
        for line in ('ssid=97;58;98;', 'address1=192.0.2.50/24', 'gateway=192.0.2.1',
                     'dns=192.0.2.53;192.0.2.54;', 'dns-search=~.;', 'method=manual'):
            self.assertIn(line + '\n', text)
        self.assertEqual(text.count('route-metric=50\n'), 2)
        self.assertEqual(text.count('dns-priority=-50\n'), 2)
        self.assertEqual(text.count('ignore-auto-dns=true\n'), 2)

    def test_private_write_is_atomic_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config'
            wifi.write_private(path, 'old')
            wifi.write_private(path, 'new')
            self.assertEqual(path.read_text(encoding='utf-8'), 'new')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            link = Path(tmp) / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                wifi.write_private(link, 'bad')
            self.assertEqual(path.read_text(encoding='utf-8'), 'new')

    def test_nmcli_sanitizes_backend_error(self):
        result = subprocess.CompletedProcess([], 10, 'password=secret', 'password=secret')
        with patch.object(wifi.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, '^NetworkManager could not complete connection$'):
                wifi.nmcli('connection', 'load', '/private/keyfile')

    def test_interactive_prompt_uses_hidden_passphrase(self):
        with patch.object(wifi, 'wifi_interfaces', return_value=['wlan0']), \
                patch('builtins.input', side_effect=['', 'New: lab', '2', '192.0.2.2/24', '192.0.2.1', '192.0.2.53']), \
                patch.object(wifi.getpass, 'getpass', return_value='secret-test-value') as password, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            values = wifi.interactive_config()
        password.assert_called_once()
        self.assertEqual(values['LIVE_WIFI_INTERFACE'], 'auto')
        self.assertEqual(values['LIVE_WIFI_ESSID'], 'New: lab')
        self.assertEqual(values['LIVE_WIFI_GATEWAY'], '192.0.2.1')
        self.assertNotIn('secret-test-value', output.getvalue())


class WifiConnectionTests(unittest.TestCase):
    def nm_backend(self, calls, ssid='Lab: secure', failure=False, metrics='100\n100\n'):
        def run(*args, **kwargs):
            calls.append(args)
            output = ''
            if args[-1:] == ('general',):
                output = 'running\n'
            elif 'DEVICE,TYPE' in args:
                output = 'eth0:ethernet\nwlan0:wifi\n'
            elif 'SSID' in args:
                output = ssid + '\n'
            elif args[:1] == ('--wait',) and failure:
                raise RuntimeError('backend secret must never leak')
            elif 'UUID,TYPE,DEVICE' in args:
                output = f'{wifi.CONNECTION_UUID}:802-11-wireless:wlan0\nwired-id:802-3-ethernet:eth0\nvpn-id:vpn:tun0\n'
            elif args[:1] == ('-g',):
                output = metrics
            return subprocess.CompletedProcess([], 0, output, '')
        return run

    def test_absent_ssid_does_not_write_or_disconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = Path(tmp) / 'wifi.nmconnection'
            calls = []
            with patch.object(wifi, 'nmcli', side_effect=self.nm_backend(calls, ssid='different')):
                self.assertFalse(wifi.connect(config(), keyfile))
            self.assertFalse(keyfile.exists())
            self.assertFalse(any('up' in call or 'down' in call or 'disconnect' in call for call in calls))

    def test_success_prefers_wifi_and_does_not_expose_passphrase_in_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = Path(tmp) / 'wifi.nmconnection'
            calls = []
            with patch.object(wifi, 'nmcli', side_effect=self.nm_backend(calls)):
                self.assertTrue(wifi.connect(config(), keyfile))
            self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('test-only-passphrase', repr(calls))
            self.assertIn(('connection', 'modify', '--temporary', 'uuid', 'wired-id',
                           'ipv4.route-metric', '600', 'ipv6.route-metric', '600'), calls)
            self.assertIn(('device', 'reapply', 'eth0'), calls)
            self.assertFalse(any('down' in c or 'disconnect' in c or 'vpn-id' in c for c in calls))

    def test_failed_activation_restores_existing_keyfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = Path(tmp) / 'wifi.nmconnection'
            wifi.write_private(keyfile, 'previous config\n')
            calls = []
            with patch.object(wifi, 'nmcli', side_effect=self.nm_backend(calls, failure=True)):
                with self.assertRaisesRegex(RuntimeError, 'Ethernet connections were not disconnected'):
                    wifi.connect(config(), keyfile)
            self.assertEqual(keyfile.read_text(encoding='utf-8'), 'previous config\n')
            self.assertFalse(any('delete' in call or 'down' in call or 'disconnect' in call for call in calls))

    def test_failed_new_activation_removes_new_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = Path(tmp) / 'wifi.nmconnection'
            calls = []
            with patch.object(wifi, 'nmcli', side_effect=self.nm_backend(calls, failure=True)):
                with self.assertRaises(RuntimeError):
                    wifi.connect(config(), keyfile)
            self.assertFalse(keyfile.exists())
            self.assertIn(('connection', 'delete', 'uuid', wifi.CONNECTION_UUID), calls)

    def test_dispatcher_idempotence(self):
        calls = []
        with patch.object(wifi, 'nmcli', side_effect=self.nm_backend(calls, metrics='600\n600\n')):
            wifi.prioritize_wifi()
        self.assertFalse(any('modify' in call or 'reapply' in call for call in calls))

    def test_no_active_managed_wifi_does_not_change_wired(self):
        with patch.object(wifi, 'nmcli', return_value=subprocess.CompletedProcess([], 0, 'wired:ethernet:eth0\n', '')) as nm:
            wifi.prioritize_wifi()
        self.assertEqual(nm.call_count, 1)

    def test_requested_missing_interface_does_not_activate_another(self):
        calls = []
        with patch.object(wifi, 'nmcli', side_effect=self.nm_backend(calls)):
            self.assertFalse(wifi.connect(config(INTERFACE='wlan9')))
        self.assertFalse(any('up' in call for call in calls))

    def test_nm_start_timeout_leaves_connections_alone(self):
        with patch.object(wifi, 'nmcli', return_value=subprocess.CompletedProcess([], 1, '', '')) as nm, \
                patch.object(wifi.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Ethernet is unchanged'):
                wifi.connect(config())
        self.assertEqual(nm.call_count, 10)


class WifiStagingTests(unittest.TestCase):
    def test_both_families_stage_own_env_runtime_initrd_hook_and_home_launcher(self):
        for profile in ('debian', 'kali-linux'):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'root'
                home = root / 'home' / profile
                home.mkdir(parents=True)
                env = Path(tmp) / 'live.env'
                values = config(ESSID=profile)
                env.write_text(wifi.render_config(values), encoding='utf-8')
                live_hooks.stage_live_wifi_runtime(root, env, profile)
                destination = root / 'etc/debian-usb/live.env'
                self.assertEqual(wifi.load_config(destination), values)
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
                for relative in ('etc/skel/wifi-connect.sh', f'home/{profile}/wifi-connect.sh',
                                 'usr/local/lib/debian-usb/live-wifi.py',
                                 'etc/NetworkManager/dispatcher.d/90-debian-usb-wifi-priority',
                                 'etc/initramfs-tools/hooks/zzzz-debian-usb-wifi'):
                    self.assertTrue(os.access(root / relative, os.X_OK), relative)
                hook = (root / 'etc/initramfs-tools/hooks/zzzz-debian-usb-wifi').read_text(encoding='utf-8')
                self.assertIn('cp /etc/debian-usb/live.env "${DESTDIR}/live.env"', hook)
                self.assertIn('chmod 0600', hook)
                self.assertTrue((root / 'etc/systemd/system/multi-user.target.wants/debian-usb-live-wifi.service').resolve().is_file())
                self.assertFalse((root / 'etc/apt/sources.list').exists())

    def test_default_env_paths_are_family_specific(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn('/initrd/debian/live/live.env', str(live_hooks.debian_live_env_path(profile='debian')))
            self.assertIn('/initrd/kali/live/live.env', str(live_hooks.debian_live_env_path(profile='kali-linux')))

    def test_kali_medium_hooks_never_include_debian_apt_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            staged = live_hooks.stage_debian_live_config_hooks(Path(tmp), 'kali-linux')
            self.assertEqual([p.name for p in staged], ['1000-network-wifi.sh'])

    def test_kali_core_is_strict_and_firmware_is_separately_resolved(self):
        required = live_hooks.live_hook_packages('kali-linux')
        optional = live_hooks.live_optional_firmware('kali-linux')
        self.assertIn('network-manager', required)
        self.assertIn('kali-archive-keyring', required)
        self.assertNotIn('debian-archive-keyring', required)
        self.assertNotIn('firmware-iwlwifi', required)
        self.assertIn('firmware-iwlwifi', optional)

    def test_runtime_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            target = root / 'usr/local/lib/debian-usb/live-wifi.py'
            target.parent.mkdir(parents=True)
            outside = Path(tmp) / 'outside'
            outside.write_text('untouched', encoding='utf-8')
            target.symlink_to(outside)
            with self.assertRaises(ValueError):
                live_hooks.stage_live_wifi_runtime(root)
            self.assertEqual(outside.read_text(encoding='utf-8'), 'untouched')
