"""One topology file, two parsers: the plugin's (transport-core/src/topology.c) and fabric-tool's.

Both read the file a site writes once and copies to every host. They must agree on which files are
valid, on the cables a valid file describes and on the lanes between every pair of hosts; otherwise the
relays fabric-tool installs and the routes the plugin uses describe different fabrics. CPU only.
"""
import ctypes
import itertools
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fabric-tool"))

from fabric_tool import model, topology  # noqa: E402

MAX_POSITIONS = 64
NAME = 64


class CCable(ctypes.Structure):
    _fields_ = [("a", ctypes.c_int), ("pa", ctypes.c_int), ("b", ctypes.c_int), ("pb", ctypes.c_int)]


class CTopology(ctypes.Structure):
    """struct sircl_core_topology (transport-core/include/sircl_core/core.h)."""

    _fields_ = [("nhosts", ctypes.c_int),
                ("host", (ctypes.c_char * NAME) * MAX_POSITIONS),
                ("ncables", ctypes.c_int),
                ("cable", CCable * (2 * MAX_POSITIONS)),
                ("fn", ((ctypes.c_char * NAME) * 2) * 2),
                ("budget", ctypes.c_uint32)]


def ring(n):
    return [(i, 0, (i + 1) % n, 1) for i in range(n)]


def path(n):
    return ring(n)[:-1]


def explicit(hosts, cables, extra=""):
    names = " ".join(f"h{i}" for i in range(hosts))
    return f"hosts {names}\ncabling explicit\n" + "".join(
        f"cable {a}.port{pa} {b}.port{pb}\n" for a, pa, b, pb in cables) + extra


def turned(cables, positions):
    """The same cables with both ports of the given positions exchanged (those hosts turned around)."""
    return [(a, pa ^ (a in positions), b, pb ^ (b in positions)) for a, pa, b, pb in cables]


def relabelled(cables, order):
    """The same shape with the hosts in another order along it."""
    return [(order[a], pa, order[b], pb) for a, pa, b, pb in cables]


def valid_corpus():
    yield "pair, path", "hosts a b\ncabling path\n"
    yield "pair, ring is one cable", "hosts a b\ncabling ring\n"
    yield "pair, two cables", explicit(2, [(0, 0, 1, 1), (1, 0, 0, 1)])
    yield "pair, two cables port 0 to port 0", explicit(2, [(0, 0, 1, 0), (0, 1, 1, 1)])
    yield "pair, one cable port 1 to port 1", explicit(2, [(0, 1, 1, 1)])
    for n in range(3, 9):
        names = " ".join(f"n{i}" for i in range(n))
        yield f"ring {n}", f"hosts {names}\ncabling ring\n"
        yield f"path {n}", f"hosts {names}\ncabling path\n"
        order = list(range(0, n, 2)) + list(range(1, n, 2))
        yield f"permuted path {n}", explicit(n, relabelled(path(n), order))
        yield f"permuted ring {n}", explicit(n, relabelled(ring(n), order))
        odd = set(range(1, n, 2))
        yield f"path {n}, odd hosts turned", explicit(n, turned(path(n), odd))
        yield f"ring {n}, odd hosts turned", explicit(n, turned(ring(n), odd))
        yield f"ring {n}, host 2 turned", explicit(n, turned(ring(n), {2}))
        yield f"ring {n}, last host turned", explicit(n, turned(ring(n), {n - 1}))
    yield "custom functions", "hosts a b c d\ncabling ring\nport0 mlx5_0 mlx5_1\nport1 mlx5_2 mlx5_3\n"
    yield "one port renamed", "hosts a b c\ncabling path\nport1 left.a right-b\n"
    yield "budget", "hosts a b c d\ncabling path\nrelay_budget 196608\n"
    yield "comments and spacing", ("# leading comment\nnet-sircl-topology\n\n  hosts  a\tb   c # trailing\n"
                                   "\tcabling\tring\r\nrelay_budget 65536 # bytes\n")
    yield "hosts on several lines", "hosts a b\nhosts c\nhosts d e\ncabling ring\n"
    yield "ring plus nothing else", "cabling ring\nhosts z y x w\n"
    yield "explicit cables in any order", explicit(4, [(2, 0, 3, 1), (0, 0, 1, 1), (1, 0, 2, 1)])
    yield "longest host name", f"hosts a {'h' * 63}\ncabling path\n"
    yield "sixteen hosts", f"hosts {' '.join(f'h{i}' for i in range(16))}\ncabling ring\n"


def invalid_corpus():
    yield "no cabling", "hosts a b\n"
    yield "unknown cabling", "hosts a b\ncabling mesh\n"
    yield "two cabling words", "hosts a b\ncabling ring path\n"
    yield "cabling without a word", "hosts a b\ncabling\n"
    yield "one host", "hosts a\ncabling path\n"
    yield "no hosts", "cabling path\n"
    yield "a host twice", "hosts a b a\ncabling ring\n"
    yield "unknown key", "hosts a b\ncabling path\nflavour x\n"
    yield "explicit without cables", "hosts a b\ncabling explicit\n"
    yield "a host without a cable", explicit(3, [(0, 0, 1, 1)])
    yield "two islands", explicit(4, [(0, 0, 1, 1), (2, 0, 3, 1)])
    yield "a port used twice", explicit(3, [(0, 0, 1, 1), (0, 0, 2, 1)])
    yield "a cable to itself", explicit(2, [(0, 0, 0, 1)])
    yield "a cable outside the hosts", explicit(2, [(0, 0, 5, 1)])
    yield "port 2", "hosts a b\ncabling explicit\ncable 0.port2 1.port0\n"
    yield "cable with trailing text", "hosts a b\ncabling explicit\ncable 0.port0 1.port1x\n"
    yield "cable with a sign", "hosts a b\ncabling explicit\ncable +0.port0 1.port1\n"
    yield "cable with one end", "hosts a b\ncabling explicit\ncable 0.port0\n"
    yield "cable with three ends", "hosts a b c\ncabling explicit\ncable 0.port0 1.port1 2.port0\n"
    yield "cable joined by a dash", "hosts a b\ncabling explicit\ncable 0.port0-1.port1\n"
    yield "ring plus a cable on a used port", "hosts a b c\ncabling ring\ncable 0.port0 2.port1\n"
    yield "port line with one name", "hosts a b\ncabling path\nport0 only\n"
    yield "port line with three names", "hosts a b\ncabling path\nport1 a b c\n"
    yield "function with a slash", "hosts a b\ncabling path\nport0 ok bad/name\n"
    yield "function too long", f"hosts a b\ncabling path\nport0 ok {'f' * 64}\n"
    yield "budget with a sign", "hosts a b\ncabling path\nrelay_budget +65536\n"
    yield "budget in hexadecimal", "hosts a b\ncabling path\nrelay_budget 0x10000\n"
    yield "budget of zero", "hosts a b\ncabling path\nrelay_budget 0\n"
    yield "budget with two words", "hosts a b\ncabling path\nrelay_budget 65536 65536\n"
    yield "words after the header key", "hosts a b\ncabling path\nnet-sircl-topology 2\n"
    yield "host name too long", f"hosts a {'h' * 64}\ncabling path\n"
    yield "too many hosts", f"hosts {' '.join(f'h{i}' for i in range(65))}\ncabling ring\n"


class TopologyConformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="sircl-conformance-")
        cls.root = Path(cls.temp.name)
        library = cls.root / "topology.so"
        subprocess.run(["cc", "-std=c11", "-O2", "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "transport-core/include"), str(ROOT / "transport-core/src/topology.c"),
                        "-o", str(library)], check=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.sircl_core_topology_parse.argtypes = [ctypes.c_char_p, ctypes.POINTER(CTopology),
                                                      ctypes.c_char_p, ctypes.c_size_t]
        cls.lib.sircl_core_topology_route.argtypes = [ctypes.POINTER(CTopology), ctypes.c_int, ctypes.c_int,
                                                      ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int)]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def plugin(self, text):
        """(struct, None) when the plugin's parser accepts the text, else (None, its message)."""
        file = self.root / "case.topology"
        file.write_bytes(text.encode())
        parsed = CTopology()
        error = ctypes.create_string_buffer(512)
        if self.lib.sircl_core_topology_parse(str(file).encode(), ctypes.byref(parsed), error, len(error)):
            return None, error.value.decode()
        return parsed, None

    @staticmethod
    def tool(text):
        """(Topology, None) when fabric-tool's parser accepts the text, else (None, its message)."""
        try:
            return topology.parse(text, path="case.topology"), None
        except model.FabricError as error:
            return None, str(error)

    def test_structure_layout_matches_the_header(self):
        # nhosts, 64 names of 64 bytes, ncables, 128 cables of four ints, four names, budget.
        self.assertEqual(ctypes.sizeof(CTopology), 4 + 64 * 64 + 4 + 128 * 16 + 4 * 64 + 4)

    def test_valid_files_mean_the_same_fabric_to_both(self):
        count = 0
        for name, text in valid_corpus():
            with self.subTest(case=name):
                c, c_error = self.plugin(text)
                py, py_error = self.tool(text)
                self.assertIsNotNone(c, f"the plugin refuses it: {c_error}")
                self.assertIsNotNone(py, f"fabric-tool refuses it: {py_error}")
                self.assertEqual([c.host[i].value.decode() for i in range(c.nhosts)], list(py.hosts))
                self.assertEqual([c.fn[p][lane].value.decode() for p in (0, 1) for lane in (0, 1)],
                                 list(py.functions))
                self.assertEqual(c.budget, py.budget)
                c_cables = {frozenset({(c.cable[i].a, c.cable[i].pa), (c.cable[i].b, c.cable[i].pb)})
                            for i in range(c.ncables)}
                py_cables = {frozenset({(cable.a, cable.a_port), (cable.b, cable.b_port)}) for cable in py.cables}
                self.assertEqual(len(c_cables), c.ncables)
                self.assertEqual(c_cables, py_cables)
                fabric = py.group.layout.fabric
                for a, b in itertools.permutations(range(c.nhosts), 2):
                    names = (ctypes.c_char_p * 2)()
                    relays = ctypes.c_int(-1)
                    self.assertEqual(self.lib.sircl_core_topology_route(ctypes.byref(c), a, b, names,
                                                                        ctypes.byref(relays)), 2, (a, b))
                    lanes = model.derive_lanes(fabric, a, b, 2, bindings=py.bindings)
                    self.assertEqual([names[0].decode(), names[1].decode()],
                                     [lane.local.device for lane in lanes], (a, b))
                    self.assertEqual({relays.value}, {lane.hops - 1 for lane in lanes}, (a, b))
                count += 1
        self.assertGreater(count, 50)

    def test_lanes_are_the_same_function_class_on_one_path_at_both_ends(self):
        # The property the two derivations must share, checked on the plugin's side from the cables alone:
        # following lane L from a, out of its port, arrives at b on the port of b's lane L toward a.
        for name, text in valid_corpus():
            c, _ = self.plugin(text)
            if c is None:
                continue
            port_of = {c.fn[p][lane].value: (p, lane) for p in (0, 1) for lane in (0, 1)}
            far = {}
            for i in range(c.ncables):
                cable = c.cable[i]
                far[(cable.a, cable.pa)] = (cable.b, cable.pb)
                far[(cable.b, cable.pb)] = (cable.a, cable.pa)

            def lanes(a, b):
                names = (ctypes.c_char_p * 2)()
                relays = ctypes.c_int(-1)
                self.lib.sircl_core_topology_route(ctypes.byref(c), a, b, names, ctypes.byref(relays))
                return [port_of[names[0]], port_of[names[1]]], relays.value

            with self.subTest(case=name):
                for a, b in itertools.permutations(range(c.nhosts), 2):
                    forward, relays = lanes(a, b)
                    backward, _ = lanes(b, a)
                    for lane in (0, 1):
                        self.assertEqual(forward[lane][1], lane, (a, b))
                        here, out = a, forward[lane][0]
                        for _ in range(relays + 1):
                            here, arrived = far[(here, out)]
                            out = 1 - arrived
                        self.assertEqual(here, b, (a, b, lane))
                        self.assertEqual(backward[lane], (arrived, lane), (a, b, lane))

    def test_invalid_files_are_refused_by_both(self):
        for name, text in invalid_corpus():
            with self.subTest(case=name):
                c, c_error = self.plugin(text)
                py, py_error = self.tool(text)
                self.assertIsNone(c, "the plugin accepts it")
                self.assertIsNone(py, "fabric-tool accepts it")
                self.assertTrue(c_error and py_error)

    def test_the_one_deliberate_difference(self):
        # The emulation tests give both ports the same function names; relay admission then keeps the
        # aggregate model. fabric-tool configures real devices and needs four distinct functions.
        text = "hosts a b c\ncabling path\nport0 emu0 emu1\nport1 emu0 emu1\n"
        self.assertIsNotNone(self.plugin(text)[0])
        self.assertIn("one function twice", self.tool(text)[1])


if __name__ == "__main__":
    unittest.main()
