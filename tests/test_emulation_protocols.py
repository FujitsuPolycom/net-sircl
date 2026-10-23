"""CPU-staged emulation must not select LL128 without diagnostic opt-in."""
import importlib.util
from pathlib import Path
import unittest

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
