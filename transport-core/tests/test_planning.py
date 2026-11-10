"""CPU fixtures for the public discovery API; no network or device access."""
import ctypes
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="sircl-core-cpu-")
        cls.root = Path(cls.temp.name)
        out = cls.root / "core.so"
        subprocess.run(["cc", "-std=c11", "-O2", "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "include"), str(ROOT / "src/sysfs.c"),
                        str(ROOT / "src/topology.c"), "-o", str(out)], check=True)
        cls.lib = ctypes.CDLL(str(out))
        cls.lib.sircl_core_gid_index.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        cls.lib.sircl_core_sysfs_guid.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        cls.lib.sircl_core_sysfs_guid.restype = ctypes.c_uint64
        cls.lib.sircl_core_sysfs_netdev.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        cls.lib.sircl_core_topology_parse.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_gid_chooses_first_nonzero_ipv4_rocev2(self):
        base = self.root / "gid" / "mlx0" / "ports/1"
        (base / "gid_attrs/types").mkdir(parents=True)
        (base / "gids").mkdir()
        for i, (kind, addr) in enumerate([
            ("IB/RoCE v1", "0000:0000:0000:0000:0000:ffff:0a00:0001"),
            ("RoCE v2", "0000:0000:0000:0000:0000:ffff:0000:0000"),
            ("RoCE v2", "fe80:0000:0000:0000:0000:0000:0000:0001"),
            ("RoCE v2", "0000:0000:0000:0000:0000:ffff:0a00:0001")]):
            (base / f"gid_attrs/types/{i}").write_text(kind + "\n")
            (base / f"gids/{i}").write_text(addr + "\n")
        self.assertEqual(self.lib.sircl_core_gid_index(str(self.root / "gid").encode(), b"mlx0", 1), 3)

    def test_missing_gid_is_explicit(self):
        self.assertEqual(self.lib.sircl_core_gid_index(str(self.root).encode(), b"absent", 1), -1)

    def test_guid_and_netdev(self):
        base = self.root / "devices/mlx1"
        (base / "device/net/eth-test").mkdir(parents=True)
        (base / "sys_image_guid").write_text("0123:4567:89ab:cdef\n")
        root = str(self.root / "devices").encode()
        self.assertEqual(self.lib.sircl_core_sysfs_guid(root, b"mlx1"), 0x0123456789ABCDEF)
        out = ctypes.create_string_buffer(64)
        self.lib.sircl_core_sysfs_netdev(root, b"mlx1", out, len(out))
        self.assertEqual(out.value, b"eth-test")

    def test_topology_rejects_cable_capacity_overflow(self):
        f = self.root / "overflow.topo"
        f.write_text("hosts a b\ncabling ring\n" + "cable 0.port0 1.port1\n" * 128)
        # Oversized scratch is sufficient for any declared v1 structure; only error path is exercised.
        out = ctypes.create_string_buffer(16384)
        error = ctypes.create_string_buffer(256)
        self.assertEqual(self.lib.sircl_core_topology_parse(str(f).encode(), out, error, len(error)), -1)
        self.assertIn(b"too many cables", error.value)

    def test_topology_rejects_ambiguous_ports_and_duplicate_hosts(self):
        for text, expected in (
                ("hosts a b c\ncabling explicit\ncable 0.port0 1.port1\ncable 0.port0 2.port1\n", b"port more than once"),
                ("hosts a a\ncabling path\n", b"host twice")):
            path = self.root / "invalid.topology"
            path.write_text(text)
            out = ctypes.create_string_buffer(16384)
            error = ctypes.create_string_buffer(256)
            self.assertEqual(self.lib.sircl_core_topology_parse(str(path).encode(), out, error, len(error)), -1)
            self.assertIn(expected, error.value)

    def parse(self, text):
        path = self.root / "case.topology"
        path.write_bytes(text.encode())
        out = ctypes.create_string_buffer(16384)
        error = ctypes.create_string_buffer(256)
        rc = self.lib.sircl_core_topology_parse(str(path).encode(), out, error, len(error))
        return rc, error.value.decode()

    def test_topology_refuses_what_it_would_have_guessed(self):
        long_name = "h" * 64
        many = " ".join(f"h{i}" for i in range(65))
        for text, expected in (
                ("hosts a b\n", "cabling is ring, path or explicit"),              # no cabling: no cables, no routes
                ("hosts a b\ncabling mesh\n", "cabling is ring, path or explicit"),
                ("hosts a b\ncabling ring path\n", "cabling takes one of"),
                ("hosts a b\ncabling explicit\n", "without a cable"),
                ("hosts a b c\ncabling explicit\ncable 0.port0 1.port1\n", "leave host 2 (c) without a cable"),
                ("hosts a b c d\ncabling explicit\ncable 0.port0 1.port1\ncable 2.port0 3.port1\n",
                 "do not join the hosts into one path or cycle"),
                ("hosts a b\ncabling explicit\ncable 0.port0 1.port1x\n", "a cable is"),   # trailing text
                ("hosts a b\ncabling explicit\ncable -0.port0 1.port1\n", "a cable is"),   # a sign
                ("hosts a b\ncabling explicit\ncable 0.port0 1.port1 2.port0\n", "a cable is"),
                ("hosts a b\ncabling explicit\ncable 0.port0 7.port1\n", "outside 0-1"),
                ("hosts a b\ncabling path\nport0 only_one\n", "requires exactly two"),
                ("hosts a b\ncabling path\nport1 a b c\n", "requires exactly two"),
                ("hosts a b\ncabling path\nport0 ok bad/name\n", "invalid device identifier"),
                ("hosts a b\ncabling path\nport0 ok " + "f" * 64 + "\n", "invalid device identifier"),
                (f"hosts a {long_name}\ncabling path\n", "longer than 63 characters"),   # was silently cut short
                (f"hosts {many}\ncabling path\n", "more than 64 hosts"),                 # was silently dropped
                ("hosts a b\ncabling path\nrelay_budget 65536 extra\n", "positive byte count"),
                ("hosts a b\ncabling path\nrelay_budget -65536\n", "positive byte count"),
                ("hosts a b\ncabling path\nrelay_budget 0x10000\n", "positive byte count"),
                ("hosts a b\ncabling path\nrelay_budget 99999999999\n", "positive byte count"),
                ("hosts a b\ncabling path\nnet-sircl-topology 2\n", "unknown words"),
                ("hosts a b\ncabling path\n# " + "x" * 1100 + "\n", "longer than 1022 characters")):
            with self.subTest(text=text[:60]):
                rc, error = self.parse(text)
                self.assertEqual(rc, -1, text[:80])
                self.assertIn(expected, error)

    def test_topology_accepts_the_documented_forms(self):
        sixty_three = "h" * 63
        many = " ".join(f"h{i}" for i in range(64))
        for text in (
                "hosts a b\ncabling path\n",
                "hosts a b\ncabling ring\n",                                # a pair: one cable, as path
                "# comment\nnet-sircl-topology\nhosts a b c  # trailing comment\n\tcabling\tring\r\n",
                "hosts a b\nhosts c d\ncabling path\nrelay_budget 196608\n",   # hosts lines append
                "hosts a b\ncabling explicit\ncable 0.port0 1.port1\ncable 1.port0 0.port1\n",   # two cables
                "hosts a b c\ncabling explicit\ncable 2.port0 0.port1\ncable 1.port1 0.port0\n",
                "hosts a b\ncabling path\nport0 emu0 emu1\nport1 emu0 emu1\n",   # aliased names: emulation
                "hosts a b\ncabling path\nport0 mlx5_0 mlx5-1.a:b\nport1 _x y\n",
                f"hosts a {sixty_three}\ncabling path\n",
                f"hosts {many}\ncabling ring\n",
                "hosts a b\ncabling path\n# " + "x" * 1019,                    # a full last line without a line end
        ):
            with self.subTest(text=text[:60]):
                rc, error = self.parse(text)
                self.assertEqual(rc, 0, error)


if __name__ == "__main__":
    unittest.main()
