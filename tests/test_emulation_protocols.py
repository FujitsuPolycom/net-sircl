"""CPU-staged emulation must not select LL128 without diagnostic opt-in."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock
import subprocess

path = Path(__file__).resolve().parents[1] / 'net-sircl/tests/emulation/nccl_e2e.py'
spec = importlib.util.spec_from_file_location('nccl_emulation', path)
emulation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emulation)


class EmulationProtocols(unittest.TestCase):
    def test_supported_protocol_selection(self):
        cases = [(None, 'LL,Simple'), ('', 'LL,Simple'), ('LL', 'LL'), ('Simple', 'Simple'),
                 ('^LL128', '^LL128'), ('LL,Simple', 'LL,Simple'),
                 ('allreduce:Simple;broadcast:LL', 'LL,Simple;allreduce:Simple;broadcast:LL'),
                 ('^LL128;allreduce:Simple', '^LL128;allreduce:Simple')]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(emulation.select_protocols(value, allow_ll128=False), expected)

    def test_ll128_requires_explicit_diagnostic_opt_in(self):
        for value in ['LL128', 'LL,LL128,Simple', '^Simple', 'allreduce:LL128', '^LL128;broadcast:LL128']:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'LL128'):
                emulation.select_protocols(value, allow_ll128=False)

    def test_diagnostic_opt_in_preserves_protocol_request(self):
        self.assertEqual(emulation.select_protocols('LL128', allow_ll128=True), 'LL128')

    def test_channel_checker_reports_launcher_rejection(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/check_channels.py'
        spec = importlib.util.spec_from_file_location('channel_check', path)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        result = subprocess.CompletedProcess([], 2, stdout='', stderr='LL128 is unsafe for CPU-staged emulation')
        with mock.patch('sys.argv', ['check_channels.py']), mock.patch.object(checker.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(AssertionError, 'LL128 is unsafe'):
                checker.main()

    def test_teardown_checker_reports_launcher_rejection(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/check_teardown.py'
        spec = importlib.util.spec_from_file_location('teardown_check', path)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        result = subprocess.CompletedProcess([], 2, stdout='', stderr='LL128 is unsafe for CPU-staged emulation')
        with mock.patch('sys.argv', ['check_teardown.py']), mock.patch.object(checker.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'LL128 is unsafe'):
                checker.main()
