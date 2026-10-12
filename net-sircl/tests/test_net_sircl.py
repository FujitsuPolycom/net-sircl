#!/usr/bin/env python3
"""CPU tests of net-sircl (no NCCL, GPU, RoCE device or DGX Spark).

- Layer tests (tests/layer_test.c): the plugin built against the shared-memory verbs stand-in
  (tests/standin/), its entry points driven between two processes of one host: transfers, the shared
  relay window, lanes, routeless connections, teardown with requests outstanding, peer loss and the
  connect deadline.
- Topology files: the routes, relays and windows the plugin derives, against fixed expectations and,
  when SparkRing's SIRCL package is beside this directory, against SIRCL's own route planner.
- The plugin library builds against libibverbs and exports ncclNetPlugin_v12.
- The vendored stand-in matches libsircl's copy when both are present.
"""
import ctypes
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
STANDIN = HERE / "tests" / "standin"
CORE = next(p for p in (HERE.parents[1] / "transport-core", HERE.parent / "transport-core") if (p / "VERSION").is_file())
CORE_SOURCES = [str(CORE / "src" / f) for f in ("core.c", "topology.c", "sysfs.c")]
LIBSIRCL_TRANSPORT = HERE.parents[1] / "libsircl" / "src" / "transport"
SIRCL_PACKAGE = HERE.parents[1] / "sircl"
FLAGS = ["-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread"]
STANDIN_FLAGS = ["-I", str(CORE / "include"), "-DNET_SIRCL_STANDIN", "-I", str(HERE), "-I", str(STANDIN / "fake_verbs"), "-I", str(STANDIN)]


class NetSirclTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="net-sircl-")
        cls.layer = Path(cls.build.name) / "layer_test"
        cls.emu = Path(cls.build.name) / "libnccl-net-sircl-emu.so"
        cc = os.environ.get("CC", "cc")
        subprocess.run([cc, *FLAGS, *STANDIN_FLAGS, str(HERE / "tests" / "layer_test.c"), str(HERE / "net_sircl.c"),
                        str(STANDIN / "shm_verbs.c"), *CORE_SOURCES, "-o", str(cls.layer)], check=True)
        subprocess.run([cc, *FLAGS, *STANDIN_FLAGS, "-fPIC", "-shared", str(HERE / "net_sircl.c"),
                        str(STANDIN / "shm_verbs.c"), *CORE_SOURCES, "-o", str(cls.emu)], check=True)
        cls.lib = ctypes.CDLL(str(cls.emu))
        cls.lib.net_sircl_topology_routes.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    # -- layer tests --
    def run_layer(self, mode):
        fabric = f"/sircl-emu-net-sircl-{os.getpid()}-{mode}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
        env.update(SIRCL_EMU_FABRIC=fabric, LAYER_QUIET="1")
        try:
            result = subprocess.run([str(self.layer), mode], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=180)
        finally:
            path = Path("/dev/shm") / fabric.lstrip("/")
            if path.exists():
                path.unlink()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"net-sircl layer test {mode}:", result.stdout)
        return result.stdout

    def test_transfers_are_exact(self):
        self.assertIn("7 sizes and 32 pipelined messages exact", self.run_layer("plain"))

    def test_comms_toward_one_peer_share_one_window(self):
        out = self.run_layer("windowed")
        self.assertIn("2 comms x 8 MiB exact", out)
        in_flight = int(out.split("max in flight ")[1].split(" B")[0])
        self.assertLessEqual(in_flight, 65536)

    def test_lanes_keep_ncclx_device_and_both_ends_agree(self):
        self.assertIn("both ends agree", self.run_layer("lanes"))

    def test_without_routes_the_nccl_device_is_used(self):
        self.assertIn("the devices NCCL passed", self.run_layer("routeless"))

    def test_teardown_with_requests_outstanding_returns(self):
        self.assertIn("receiver closed with 2 receives outstanding", self.run_layer("teardown"))

    def test_a_vanished_peer_fails_the_waiting_request(self):
        self.assertIn("failed with ncclRemoteError", self.run_layer("peerloss"))

    def test_connect_gives_up_at_its_deadline(self):
        self.assertIn("connect failed after", self.run_layer("timeout"))

    # -- topology files --
    def routes(self, text, position):
        with tempfile.NamedTemporaryFile("w", suffix=".topo", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            out = ctypes.create_string_buffer(4096)
            rc = self.lib.net_sircl_topology_routes(path.encode(), position, out, len(out))
            text = out.value.decode()
        finally:
            os.unlink(path)
        if rc:
            return text
        return dict(item.split("=", 1) for item in text.split(";") if item)

    def test_topology_ring_of_four(self):
        ring4 = "hosts a b c d\ncabling ring\n"
        self.assertEqual(self.routes(ring4, 0), {
            "1": "rocep1s0f0/roceP2p1s0f0:0:0",
            "2": "rocep1s0f0/roceP2p1s0f1:1:393216",   # two equal directions: lane 0 port 0, lane 1 port 1
            "3": "rocep1s0f1/roceP2p1s0f1:0:0"})
        self.assertEqual(self.routes(ring4, 2)["0"], "rocep1s0f1/roceP2p1s0f0:1:393216")

    def test_topology_path_of_three_in_a_ring_of_four(self):
        # Ranks on hosts 0-2; host 3 relays. The closing pair 2-0 has two equal directions (through 1 and 3).
        routes = self.routes("hosts a b c d\ncabling ring\n", 2)
        self.assertEqual(routes["1"], "rocep1s0f1/roceP2p1s0f1:0:0")
        self.assertEqual(routes["0"], "rocep1s0f1/roceP2p1s0f0:1:393216")

    def test_topology_path_of_four(self):
        path4 = "hosts a b c d\ncabling path\nrelay_budget 196608\n"
        self.assertEqual(self.routes(path4, 3)["0"], "rocep1s0f1/roceP2p1s0f1:2:196608")
        self.assertEqual(self.routes(path4, 0)["3"], "rocep1s0f0/roceP2p1s0f0:2:196608")

    def test_topology_pair_and_custom_functions(self):
        pair = "hosts x y\ncabling ring\nport0 eth0a eth0b\nport1 eth1a eth1b\n"
        self.assertEqual(self.routes(pair, 0), {"1": "eth0a/eth0b:0:0"})
        self.assertEqual(self.routes(pair, 1), {"0": "eth1a/eth1b:0:0"})

    def test_topology_explicit_cables(self):
        text = "hosts a b c\ncabling explicit\ncable 0.port1 2.port0\ncable 2.port1 1.port0\n"
        routes = self.routes(text, 0)
        self.assertEqual(routes["2"], "rocep1s0f1/roceP2p1s0f1:0:0")
        self.assertEqual(routes["1"], "rocep1s0f1/roceP2p1s0f1:1:393216")

    def test_topology_errors_are_named(self):
        self.assertIn("unknown key", self.routes("hosts a b\nflavour x\n", 0))
        self.assertIn("a cable is", self.routes("hosts a b\ncabling explicit\ncable 0.port2 1.port0\n", 0))
        self.assertIn("fewer than two hosts", self.routes("hosts a\n", 0))

    @unittest.skipUnless((SIRCL_PACKAGE / "sparkring_sircl" / "routes.py").exists(), "SIRCL is not beside this checkout")
    def test_topology_matches_sircls_planner(self):
        sys.path.insert(0, str(SIRCL_PACKAGE))
        try:
            from sparkring_sircl import routes as planner
        finally:
            sys.path.pop(0)
        for layout, cabling, n in (("ring:4", "ring", 4), ("path:0-3", "path", 4), ("ring:8", "ring", 8)):
            parsed = planner.Layout.parse(layout)
            derived = planner.derive_routes(parsed, 2)
            topo = f"hosts {' '.join(f'h{i}' for i in range(n))}\ncabling {cabling}\n"
            for rank in range(parsed.world):
                ours = self.routes(topo, rank)
                theirs = derived.route_map(rank)
                for peer, lanes in theirs.items():
                    with self.subTest(layout=layout, rank=rank, peer=peer):
                        self.assertEqual(ours[str(peer)].split(":")[0], "/".join(lanes))

    # -- packaging --
    @unittest.skipUnless(Path("/usr/include/infiniband/verbs.h").exists(), "libibverbs headers not installed")
    def test_plugin_library_exports_the_v12_symbol(self):
        cc = os.environ.get("CC", "cc")
        lib = Path(self.build.name) / "libnccl-net-sircl.so"
        subprocess.run([cc, *FLAGS, "-fPIC", "-shared", "-I", str(HERE), "-I", str(CORE / "include"), str(HERE / "net_sircl.c"), *CORE_SOURCES, "-o", str(lib),
                        "-libverbs"], check=True)
        nm = shutil.which("nm")
        if nm is None:
            self.skipTest("nm not installed")
        symbols = subprocess.run([nm, "-D", "--defined-only", str(lib)], stdout=subprocess.PIPE, text=True,
                                 check=True).stdout
        self.assertIn("ncclNetPlugin_v12", symbols)

    @unittest.skipUnless(LIBSIRCL_TRANSPORT.exists(), "libsircl is not beside this checkout")
    def test_vendored_standin_matches_libsircl(self):
        for name in ("shm_verbs.c", "shm_verbs.h", "proxy_names.h", "fake_verbs/infiniband/verbs.h"):
            with self.subTest(file=name):
                self.assertTrue(filecmp.cmp(STANDIN / name, LIBSIRCL_TRANSPORT / name, shallow=False), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
