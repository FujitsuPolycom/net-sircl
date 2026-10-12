import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("adapt_proxy", ROOT / "tools/adapt_proxy.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
LIB = ROOT.parent / "libsircl/src/transport"


@unittest.skipUnless(LIB.is_dir(), "optional libsircl compatibility consumer absent")
class AdaptationTests(unittest.TestCase):
    def test_both_views_use_shared_setup_and_keep_wire_features(self):
        for name in ("sircl_roce_proxy.c", "sircl_p2p_proxy.c"):
            src = LIB / name
            before = src.read_bytes()
            result = adapter.adapt(src)
            self.assertEqual(src.read_bytes(), before)
            self.assertEqual(result, adapter.adapt(src))
            self.assertIn("sircl_core_create_rc_qp", result)
            self.assertIn("sircl_core_qp_rtr", result)
            self.assertIn("sircl_core_post_send", result)
            self.assertIn("sircl_core_poll_cq", result)
            self.assertIn("local_features(void)", result)
            self.assertNotIn("struct ibv_qp_attr rtr;", result)

    def test_drift_and_crlf_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("sircl_roce_proxy.c", "sircl_p2p_proxy.c"):
                source = (LIB / name).read_bytes()
                candidate = Path(tmp) / name
                for bad in (source + b"\n", source.replace(b"\n", b"\r\n")):
                    candidate.write_bytes(bad)
                    with self.assertRaisesRegex(ValueError, "unreviewed proxy bytes"):
                        adapter.adapt(candidate)

    def test_missing_or_ambiguous_replacement_fails(self):
        for text in ("none", "x x"):
            with self.assertRaises(ValueError):
                adapter.one(text, "x", "y")


if __name__ == "__main__":
    unittest.main()
