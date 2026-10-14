#!/usr/bin/env python3
"""CPU tests of net-sircl (no NCCL, GPU, RoCE device or DGX Spark).

- Layer tests (tests/layer_test.c): the plugin built against the shared-memory verbs stand-in
  (tests/standin/), its entry points driven between two processes of one host: transfers, the shared
  relay window, lanes, routeless connections, teardown with requests outstanding, peer loss and the
  connect deadline.
- Topology files: the routes, relays and windows the plugin derives, against fixed expectations and an independent queue-capacity oracle.
- The plugin library builds against libibverbs and exports ncclNetPlugin_v7 through v12.
"""
import json
import ctypes
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
FLAGS = ["-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread"]
STANDIN_FLAGS = ["-I", str(CORE / "include"), "-DNET_SIRCL_STANDIN", "-I", str(HERE), "-I", str(STANDIN / "fake_verbs"), "-I", str(STANDIN)]


class NetSirclTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="net-sircl-")
        cls.topology = Path(cls.build.name) / "path.topology"
        cls.topology.write_text("hosts a b c d e f\ncabling path\nport0 emu0 emu1\nport1 emu0 emu1\n")
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
    def test_short_request_provider_cost(self):
        topology = Path(self.build.name) / "short.topology"
        topology.write_text("hosts a b c d\ncabling path\nport0 emu0 emu1\nport1 emu2 emu3\n")
        for size in (32768, 65536, 131072):
            exe = Path(self.build.name) / f"short-{size}"
            subprocess.run([os.environ.get("CC", "cc"), *FLAGS, *STANDIN_FLAGS,
                            f"-DREQUEST_BYTES={size}", str(HERE / "tests/progress_cost_test.c"),
                            str(STANDIN / "shm_verbs.c"), *CORE_SOURCES,
                            "-Wl,--wrap=sccl_emu_ibv_post_send", "-Wl,--wrap=sccl_emu_ibv_poll_cq",
                            "-o", str(exe)], check=True)
            for budget in (327680, 65536):
                for profile in (False, True):
                    with self.subTest(size=size, budget=budget, profile=profile):
                        fabric = f"/sircl-short-{os.getpid()}"
                        env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
                        env.update(SIRCL_EMU_FABRIC=fabric, NET_SIRCL_RELAY_BUDGET=str(budget))
                        if profile:
                            env["NET_SIRCL_PROFILE"] = str(Path(self.build.name) / f"short-{size}-{budget}")
                        try:
                            result = subprocess.run([str(exe), str(topology), "measure"],
                                                    env=env, capture_output=True, text=True, timeout=60)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            print(result.stdout, end="")
                        finally:
                            (Path("/dev/shm") / fabric.lstrip("/")).unlink(missing_ok=True)

    def test_progress_provider_cost(self):
        exe = Path(self.build.name) / "progress_cost_test"
        subprocess.run([os.environ.get("CC", "cc"), *FLAGS, *STANDIN_FLAGS,
                        str(HERE / "tests" / "progress_cost_test.c"), str(STANDIN / "shm_verbs.c"),
                        *CORE_SOURCES, "-Wl,--wrap=sccl_emu_ibv_post_send",
                        "-Wl,--wrap=sccl_emu_ibv_poll_cq", "-o", str(exe)], check=True)
        topology = Path(self.build.name) / "cost.topology"
        topology.write_text("hosts a b c d\ncabling path\nport0 emu0 emu1\nport1 emu0 emu1\n")
        outputs = []
        for enabled in (False, True):
            for mode in ("idle", "fill", "bulk", "refill", "fair"):
                with self.subTest(mode=mode, profile=enabled):
                    fabric = f"/sircl-emu-cost-{os.getpid()}-{mode}"
                    env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
                    env["SIRCL_EMU_FABRIC"] = fabric
                    prefix = Path(self.build.name) / f"profile-{mode}"
                    if enabled:
                        env["NET_SIRCL_PROFILE"] = str(prefix)
                    try:
                        result = subprocess.run([str(exe), str(topology), mode], env=env,
                                                capture_output=True, text=True, timeout=60)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        print(result.stdout, end="")
                        files = list(prefix.parent.glob(prefix.name + ".*.json"))
                        if not enabled:
                            self.assertEqual(files, [])
                            outputs.append(result.stdout)
                        else:
                            self.assertEqual(result.stdout, outputs[("idle", "fill", "bulk", "refill", "fair").index(mode)])
                            self.assertEqual(len(files), 1)
                            doc = json.loads(files[0].read_text())
                            self.assertEqual(doc["lost_observations"], 0)
                            self.assertIsNone(doc["remote_completion_to_credit_ns"])
                            self.assertGreater(doc["wall_ns"], 0)
                            self.assertGreater(doc["thread_cpu_ns"], 0)
                            rows = doc["rows"]
                            self.assertEqual(len(rows), 64)
                            self.assertEqual(sum(r["data_bytes"] for r in rows), 64 * 1024**2)
                            self.assertEqual(sum(r["bytes"] for r in rows), 64 * (1024**2 + 8))
                            for r in rows:
                                self.assertEqual(r["errors"] + r["unmatched"] + r["pending"], 0)
                                self.assertEqual(r["sizes"]["count"], r["wrs"])
                                self.assertEqual(r["completion_ns"]["count"], r["data_wrs"])
                                self.assertEqual(r["wait_ns"]["count"], r["waits"])
                                self.assertEqual(sum(r["wait_samples_ns"]), r["wait_ns"]["sum"])
                                self.assertEqual(len(r["wait_samples_ns"]), r["waits"])
                                self.assertEqual(r["abandoned_waits"], 0)
                                self.assertGreater(r["waits"], 0)
                                self.assertEqual(r["credit_usable_ns"]["count"], r["data_wrs"] + 1)
                                self.assertEqual(r["cqes"]["count"], r["polls"] - r["empty"])
                                for value in r.values():
                                    if isinstance(value, dict):
                                        self.assertEqual(sum(value["log2_bins"]), value["count"])
                            summary = subprocess.run([sys.executable, str(HERE / "tools" / "summarize_profile.py"),
                                                      str(files[0])], capture_output=True, text=True, check=True)
                            self.assertIn("emu0", summary.stdout)
                            self.assertIn("emu1", summary.stdout)
                    finally:
                        (Path("/dev/shm") / fabric.lstrip("/")).unlink(missing_ok=True)

        for enabled in (False, True):
            env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
            fabric = f"/sircl-emu-batches-{os.getpid()}"
            env["SIRCL_EMU_FABRIC"] = fabric
            if enabled:
                env["NET_SIRCL_PROFILE"] = str(Path(self.build.name) / "batches")
            try:
                result = subprocess.run([str(exe), str(topology), "batch"], env=env,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("three bounded CQ batches", result.stdout)
            finally:
                (Path("/dev/shm") / fabric.lstrip("/")).unlink(missing_ok=True)

        topology.write_text("hosts a b c d\ncabling path\nport0 emu0 emu1\nport1 emu2 emu3\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
        fabric = f"/sircl-emu-route-fill-{os.getpid()}"
        env["SIRCL_EMU_FABRIC"] = fabric
        try:
            result = subprocess.run([str(exe), str(topology), "route"], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stdout, end="")
        finally:
            (Path("/dev/shm") / fabric.lstrip("/")).unlink(missing_ok=True)

    def test_independent_ack_admission(self):
        exe = Path(self.build.name) / "ack_test"
        subprocess.run([os.environ.get("CC", "cc"), *FLAGS, *STANDIN_FLAGS,
                        str(HERE / "tests" / "ack_test.c"), str(STANDIN / "shm_verbs.c"),
                        *CORE_SOURCES, "-Wl,--wrap=sccl_emu_ibv_post_send",
                        "-Wl,--wrap=sccl_emu_ibv_poll_cq", "-o", str(exe)], check=True)
        topology = Path(self.build.name) / "ack.topology"
        topology.write_text("hosts a b c d\ncabling path\nport0 emu0 emu1\nport1 emu2 emu3\n")
        for profile in (False, True):
            env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
            fabric = f"/sircl-emu-ack-{os.getpid()}"
            env.update(SIRCL_EMU_FABRIC=fabric, NET_SIRCL_STANDIN_DEVICES="4")
            if profile:
                env["NET_SIRCL_PROFILE"] = str(Path(self.build.name) / "ack-profile")
            try:
                result = subprocess.run([str(exe), str(topology)], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                print(result.stdout, end="")
            finally:
                (Path("/dev/shm") / fabric.lstrip("/")).unlink(missing_ok=True)

    def test_ib_settings_and_completion_hint(self):
        exe = Path(self.build.name) / "settings_test"
        subprocess.run([os.environ.get("CC", "cc"), *FLAGS, *STANDIN_FLAGS,
                        str(HERE / "tests" / "settings_test.c"), str(STANDIN / "shm_verbs.c"),
                        *CORE_SOURCES, "-Wl,--wrap=sccl_emu_ibv_modify_qp",
                        "-Wl,--wrap=sccl_emu_ibv_poll_cq", "-o", str(exe)], check=True)
        fabric = f"/sircl-emu-settings-{os.getpid()}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
        env.update(SIRCL_EMU_FABRIC=fabric, NET_SIRCL_TOPOLOGY=str(self.topology), NET_SIRCL_POSITION="0")
        try:
            result = subprocess.run([str(exe)], env=env, capture_output=True, text=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            (Path("/dev/shm") / fabric.lstrip("/")).unlink(missing_ok=True)

    def test_aggregate_relay_admission(self):
        exe = Path(self.build.name) / "relay_test"
        subprocess.run([os.environ.get("CC", "cc"), *FLAGS, *STANDIN_FLAGS,
                        str(HERE / "tests" / "relay_test.c"), str(STANDIN / "shm_verbs.c"),
                        *CORE_SOURCES, "-Wl,--wrap=sccl_emu_ibv_post_send",
                        "-Wl,--wrap=sccl_emu_ibv_poll_cq", "-o", str(exe)], check=True)
        topology = Path(self.build.name) / "relay.topology"
        topology.write_text("hosts a b c d\ncabling path\nport0 emu0 emu1\nport1 emu0 emu1\n")
        fabric = f"/sircl-emu-relay-{os.getpid()}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
        env["SIRCL_EMU_FABRIC"] = fabric
        try:
            result = subprocess.run([str(exe), str(topology)], env=env, capture_output=True,
                                    text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            (Path("/dev/shm") / fabric.lstrip("/")).unlink(missing_ok=True)

    def run_layer(self, mode):
        fabric = f"/sircl-emu-net-sircl-{os.getpid()}-{mode}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
        env.update(SIRCL_EMU_FABRIC=fabric, NET_SIRCL_TOPOLOGY=str(self.topology), LAYER_QUIET="1")
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

    def test_direct_topology_lanes_keep_nccl_device(self):
        self.assertIn("both ends agree", self.run_layer("directlanes"))

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
            "2": "rocep1s0f0/roceP2p1s0f1:1:393216",   # opposite directions
            "3": "rocep1s0f1/roceP2p1s0f1:0:0"})
        self.assertEqual(self.routes(ring4, 2)["0"], "rocep1s0f1/roceP2p1s0f0:1:393216")

    def test_topology_path_of_three_in_a_ring_of_four(self):
        # Ranks on hosts 0-2; host 3 relays. The closing pair 2-0 has two equal directions (through 1 and 3).
        routes = self.routes("hosts a b c d\ncabling ring\n", 2)
        self.assertEqual(routes["1"], "rocep1s0f1/roceP2p1s0f1:0:0")
        self.assertEqual(routes["0"], "rocep1s0f1/roceP2p1s0f0:1:393216")

    def test_topology_path_of_four(self):
        path4 = "hosts a b c d\ncabling path\nrelay_budget 196608\n"
        self.assertEqual(self.routes(path4, 3)["0"], "rocep1s0f1/roceP2p1s0f1:2:176896")
        self.assertEqual(self.routes(path4, 0)["3"], "rocep1s0f0/roceP2p1s0f0:2:176896")

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

    def test_source_shares_bound_all_path_and_ring_layouts(self):
        for n in (16, 64):  # Larger route sets retain the aggregate fallback.
            for cabling in ("path", "ring"):
                topo = f"hosts {' '.join(f'h{i}' for i in range(n))}\ncabling {cabling}\n"
                sources = max(1, n - 2)
                share = 327680 // sources // 64 * 64
                self.assertLessEqual(sources * share * 7 // 5, 524288 * 7 // 8)
                for source in range(n):
                    peers = self.routes(topo, source)
                    self.assertEqual(len(peers), n - 1)
                    for route in peers.values():
                        _, relays, cap = route.split(":")
                        self.assertEqual(int(cap), share if int(relays) else 0)

    def test_concurrent_progress_two_through_eight_ranks(self):
        exe = Path(self.build.name) / "topology_progress_test"
        subprocess.run([os.environ.get("CC", "cc"), *FLAGS, *STANDIN_FLAGS,
                        str(HERE / "tests" / "topology_progress_test.c"), str(STANDIN / "shm_verbs.c"),
                        *CORE_SOURCES, "-o", str(exe)], check=True)
        for n in range(2, 9):
            for cabling in ("path", "ring", "explicit"):
                for budget, profiled in ((65536, False), (327680, False), (65536, True), (327680, True)):
                    with self.subTest(ranks=n, cabling=cabling, budget=budget):
                        topology = Path(self.build.name) / "concurrent.topology"
                        text = f"hosts {' '.join(f'h{i}' for i in range(n))}\ncabling {cabling}\n"
                        if cabling == "explicit":
                            order = list(range(0, n, 2)) + list(range(1, n, 2))
                            text += ''.join(f"cable {a}.port0 {b}.port1\n" for a, b in zip(order, order[1:]))
                        topology.write_text(text + "port0 emu0 emu1\nport1 emu2 emu3\n")
                        fabric = f"/sircl-emu-progress-{os.getpid()}-{n}-{cabling}-{budget}"
                        env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
                        env.update(NET_SIRCL_TOPOLOGY=str(topology), SIRCL_EMU_FABRIC=fabric,
                                   NET_SIRCL_RELAY_BUDGET=str(budget))
                        profile_dir = Path(self.build.name) / f"topo-profile-{n}-{cabling}-{budget}"
                        profile_dir.mkdir(exist_ok=True)
                        if profiled:
                            env["NET_SIRCL_PROFILE"] = str(profile_dir / "rank")
                        try:
                            result = subprocess.run([str(exe), str(n)], env=env, capture_output=True,
                                                    text=True, timeout=55)
                            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                            self.assertIn("exact and bounded", result.stdout)
                            files = list(profile_dir.glob("*.json"))
                            if profiled:
                                self.assertGreaterEqual(len(files), n)
                                for file in files:
                                    doc = json.loads(file.read_text())
                                    self.assertEqual(doc["lost_observations"], 0)
                                    for row in doc["rows"]:
                                        self.assertEqual(row["errors"] + row["unmatched"] + row["pending"], 0)
                            else:
                                self.assertEqual(files, [])
                        finally:
                            (Path("/dev/shm") / fabric.lstrip("/")).unlink(missing_ok=True)

    def test_route_allowances_all_queues_and_reverse_acks(self):
        # Enumerate physical simple paths independently of the C port walker.
        # Bounding the complete all-pairs set also bounds EVERY subset of ring,
        # tree and P2P routes and every multiplicity of channels on those routes.
        libpath = Path(self.build.name) / "route_model.so"
        subprocess.run([os.environ.get("CC", "cc"), *FLAGS, *STANDIN_FLAGS, "-shared", "-fPIC",
                        str(HERE / "tests" / "route_model.c"), str(STANDIN / "shm_verbs.c"),
                        *CORE_SOURCES, "-o", str(libpath)], check=True)
        lib = ctypes.CDLL(str(libpath))
        lib.test_route_caps.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        names = ["emu0", "emu1", "emu2", "emu3"]
        reports = []
        for n in range(2, 9):
            for layout in ("path", "ring", "permuted-path", "flipped-path", "flipped-ring"):
                order = list(range(n)) if layout in ("path", "ring") else list(range(0, n, 2)) + list(range(1, n, 2))
                pairs = list(zip(order, order[1:]))
                if "ring" in layout and n > 2:
                    pairs.append((order[-1], order[0]))
                flip = [i % 2 if "flipped" in layout else 0 for i in range(n)]
                edges = [(a, flip[a], b, 1 ^ flip[b]) for a, b in pairs]
                topo = f"hosts {' '.join(f'h{i}' for i in range(n))}\ncabling explicit\n"
                topo += ''.join(f"cable {a}.port{pa} {b}.port{pb}\n" for a, pa, b, pb in edges)
                topo += "port0 emu0 emu1\nport1 emu2 emu3\n"
                path = Path(self.build.name) / "queue-model.topology"
                path.write_text(topo)
                adj = {s: {} for s in range(n)}
                for a, pa, b, pb in edges:
                    adj[a][b], adj[b][a] = pa, pb
                def paths(prefix, target):
                    if prefix[-1] == target:
                        return [prefix]
                    return [p for v in adj[prefix[-1]] if v not in prefix for p in paths(prefix + [v], target)]
                routes = {s: self.routes(topo, s) for s in range(n)}
                queues, reverse_ok = {}, True
                for s in range(n):
                    for d in range(n):
                        if s == d:
                            continue
                        for lane, name in enumerate(routes[s][str(d)].split(":")[0].split("/")):
                            f = names.index(name)
                            choices = paths([s], d)
                            shortest = min(map(len, choices))
                            selected = [p for p in choices if len(p) == shortest and adj[s][p[1]] == f // 2]
                            self.assertEqual(len(selected), 1)
                            p = selected[0]
                            back = routes[d][str(s)].split(":")[0].split("/")[lane]
                            reverse_ok &= back == names[adj[d][p[-2]] * 2 + f % 2]
                            for prev, relay, nxt in zip(p, p[1:], p[2:]):
                                q = (relay, adj[relay][nxt], f % 2)
                                queues.setdefault(q, {}).setdefault((s, f), set()).add(d)
                for budget in (65536, 262144, 327680):
                    caps = (ctypes.c_uint32 * (n * 4))()
                    derived = lib.test_route_caps(os.fsencode(path), budget, caps)
                    self.assertEqual(derived, int(reverse_ok), (n, layout))
                    # Non-reciprocal tie routes deliberately keep the aggregate fallback.
                    if not derived:
                        self.assertEqual(set(caps), {budget // max(1, n - 2) // 64 * 64})
                        continue
                    forward = (ctypes.c_uint32 * (n * 4))()
                    acks = (ctypes.c_uint32 * (n * 4))()
                    split = lib.test_split_caps(os.fsencode(path), budget, forward, acks)
                    self.assertEqual(split, int(min(caps) >= 1280))
                    rows = []
                    for (relay, port, lane), users in sorted(queues.items()):
                        fw = sum(caps[s * 4 + f] for s, f in users)
                        rev_users = queues.get((relay, 1 - port, lane), {})
                        rev = sum(caps[s * 4 + f] for s, f in rev_users)
                        self.assertLessEqual(fw, budget)
                        self.assertLessEqual(5 * fw + 2 * rev, 5 * 458752)
                        for s, f in users:
                            self.assertGreaterEqual(caps[s * 4 + f], 1024)
                        split_fw = sum(forward[s*4+f] for s, f in users)
                        split_ack = sum(acks[s*4+f] for s, f in rev_users)
                        split_bound = split_fw + (split_ack if split else (2*rev+4)//5)
                        self.assertLessEqual(split_bound, 458752)
                        if split:
                            self.assertLessEqual(5*split_fw, 6*budget)
                            self.assertLessEqual(5*split_ack, budget)
                            self.assertGreaterEqual(min(acks), 256)
                        rows.append(dict(split=bool(split), split_forward=split_fw,
                                         split_reverse_ack=split_ack, split_envelope=split_bound,
                                         relay=relay, port=port, lane=lane, forward=fw, reverse=rev,
                                         envelope=fw + (2 * rev + 4) // 5,
                                         pairs=[dict(source=s, function=f, destinations=sorted(ds), cap=caps[s*4+f])
                                                for (s, f), ds in sorted(users.items())]))
                    if budget == 327680 and layout in ("path", "ring"):
                        reports.append(dict(hosts=n, layout=layout, caps=list(caps), forward_caps=list(forward), ack_caps=list(acks), queues=rows))
                    if n == 4 and layout == "path":
                        self.assertEqual(caps[0], budget * 3 // 4 // 64 * 64)
                        self.assertEqual(caps[4], budget // 4 // 64 * 64)
                # Duplicate function names cannot identify physical queue directions.
                path.write_text(topo.replace("port1 emu2 emu3", "port1 emu0 emu1"))
                self.assertEqual(lib.test_route_caps(os.fsencode(path), 327680, caps), 0)
                self.assertEqual(set(caps), {327680 // max(1, n - 2) // 64 * 64})
        # The 2/5 ACK bound holds for tiny control writes and every payload size
        # up to a default chunk at every supported verbs MTU.
        for mtu in (256, 512, 1024, 2048, 4096):
            for size in range(1, 65537):
                packets = (size + mtu - 1) // mtu
                charge = (size + 63) // 64 * 64 + 256 * packets
                self.assertLessEqual(5 * 128 * packets, 2 * charge)
        if os.environ.get("NET_SIRCL_TEST_ROUTE_REPORT"):
            Path(os.environ["NET_SIRCL_TEST_ROUTE_REPORT"]).write_text(json.dumps(reports, indent=2) + "\n")

    def test_directional_queue_origin_bound(self):
        # Enumerate all shortest paths (including both equal ring paths). A
        # source reaches a directed queue through one first port, regardless
        # of destination/channel count. Count origins, not independent flows.
        for n in range(2, 9):
            for ring in (False, True):
                adjacency = [set() for _ in range(n)]
                for a in range(n if ring and n > 2 else n - 1):
                    b = (a + 1) % n
                    adjacency[a].add(b)
                    adjacency[b].add(a)
                queues = {}
                def paths(path, target):
                    if path[-1] == target:
                        return [path]
                    return [p for nxt in adjacency[path[-1]] if nxt not in path
                            for p in paths(path + [nxt], target)]
                for source in range(n):
                    for target in range(n):
                        if source == target:
                            continue
                        choices = paths([source], target)
                        shortest = min(map(len, choices))
                        for path in choices:
                            if len(path) != shortest:
                                continue
                            for i in range(1, len(path) - 1):
                                origins = queues.setdefault((path[i - 1], path[i], path[i + 1]), {})
                                origins.setdefault(source, set()).add(path[1])
                for origins in queues.values():
                    self.assertLessEqual(len(origins), n - 2)
                    self.assertTrue(all(len(ports) == 1 for ports in origins.values()))

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
        for version in range(7, 13):
            self.assertIn(f"ncclNetPlugin_v{version}", symbols)

    def test_exported_abis_call_through_with_exact_layouts(self):
        for version in range(7, 13):
            with self.subTest(version=version):
                exe = Path(self.build.name) / f"abi_v{version}"
                subprocess.run([os.environ.get("CC", "cc"), *FLAGS, *STANDIN_FLAGS,
                                f"-DTEST_ABI={version}", str(HERE / "tests" / "abi_test.c"),
                                str(STANDIN / "shm_verbs.c"), *CORE_SOURCES, "-o", str(exe)], check=True)
                fabric = f"/sircl-emu-abi-{os.getpid()}-{version}"
                env = {k: v for k, v in os.environ.items() if not k.startswith("NET_SIRCL_")}
                env.update(SIRCL_EMU_FABRIC=fabric, NET_SIRCL_TOPOLOGY=str(self.topology), NET_SIRCL_POSITION="0")
                try:
                    result = subprocess.run([str(exe)], env=env, capture_output=True, text=True, timeout=45)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"ext-net v{version}:", result.stdout)
                finally:
                    (Path("/dev/shm") / fabric.lstrip("/")).unlink(missing_ok=True)



if __name__ == "__main__":
    unittest.main(verbosity=2)
