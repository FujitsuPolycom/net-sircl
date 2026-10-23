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


if __name__ == "__main__":
    unittest.main()
