"""Package resolution tests using only mocked target APT/dpkg processes."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from debian_usb.live_packages import candidate_available, install_optional


class OptionalLivePackageTests(unittest.TestCase):
    def test_candidate_detection(self):
        self.assertTrue(candidate_available('wifite:\n  Candidate: 2.7.0-1\n'))
        self.assertFalse(candidate_available('horst:\n  Candidate: (none)\n'))
        self.assertFalse(candidate_available(''))

    def test_invalid_packages_never_execute(self):
        for name in ('x;touch', '-oDebug=x', 'pkg\nnext', '../bad', ''):
            with self.subTest(name=name), patch('debian_usb.live_packages.subprocess.run') as run:
                with self.assertRaises(ValueError):
                    install_optional([name])
                run.assert_not_called()

    def runner(self, calls, fail_simulation=False, verified=True):
        def run(args, **kwargs):
            calls.append(args)
            if args[0] == 'apt-cache':
                text = '  Candidate: (none)\n' if args[-1] == 'missing-tool' else '  Candidate: 1.0\n'
            elif args[0] == 'dpkg-query':
                text = 'wifite\tinstall ok installed\n' if verified else 'wifite\tinstall ok unpacked\n'
            else:
                text = ''
            if '-s' in args and fail_simulation:
                raise subprocess.CalledProcessError(100, args)
            return subprocess.CompletedProcess(args, 0, text, '')
        return run

    def test_missing_candidate_is_reported_available_is_installed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / 'report.json'
            calls = []
            with patch('debian_usb.live_packages.subprocess.run', side_effect=self.runner(calls)):
                report = install_optional(['wifite', 'missing-tool', 'wifite'], report_path=report_path, update=True)
            self.assertEqual(report['requested'], ['wifite', 'missing-tool'])
            self.assertEqual(report['installed'], ['wifite'])
            self.assertEqual(report['unavailable'], ['missing-tool'])
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(json.loads(report_path.read_text(encoding='utf-8')), report)
            installs = [c for c in calls if 'install' in c]
            self.assertEqual(len(installs), 2)
            self.assertTrue(all('missing-tool' not in c for c in installs))
            self.assertIn('APT::Update::Error-Mode=any', calls[0])
            self.assertTrue(all('trusted=yes' not in ' '.join(c) for c in calls))

    def test_solver_error_is_fatal_and_reported_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            calls = []
            with patch('debian_usb.live_packages.subprocess.run', side_effect=self.runner(calls, fail_simulation=True)):
                with self.assertRaises(subprocess.CalledProcessError):
                    install_optional(['wifite'], report_path=path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'failed')
            self.assertFalse(any('-y' in c for c in calls))

    def test_unconfigured_package_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            with patch('debian_usb.live_packages.subprocess.run', side_effect=self.runner([], verified=False)):
                with self.assertRaisesRegex(RuntimeError, 'fully configure'):
                    install_optional(['wifite'], report_path=path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'failed')

    def test_all_unavailable_does_not_run_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            with patch('debian_usb.live_packages.subprocess.run', side_effect=self.runner(calls)):
                report = install_optional(['missing-tool'], report_path=Path(tmp) / 'report.json')
            self.assertEqual(report['installed'], [])
            self.assertFalse(any(c[0] == 'apt-get' for c in calls))
