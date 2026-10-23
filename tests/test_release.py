"""Standalone release contracts: sources, examples, identities and distributable bytes."""
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_release_transport_source_bytes_match_manifest(self):
        manifest = json.loads((ROOT / "SOURCE.json").read_text())
        excluded = {".git", "build", ".core-build", "dist", "test-results", ".codegraph"}
        sources = {path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*")
                   if path.is_file() and path.suffix in {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp"}
                   and not set(path.relative_to(ROOT).parts) & excluded}
        self.assertEqual(set(manifest["source_sha256"]), sources)
        for name, expected in manifest["source_sha256"].items():
            with self.subTest(path=name):
                self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), expected)

    @unittest.skipUnless((ROOT / ".git").exists(), "source archive has no Git index; packaging requires a checkout")
    def test_archive_reproducible_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            archives = [Path(tmp) / name for name in ("first.tar.gz", "second.tar.gz")]
            # An untracked file inside a shipped directory must never enter the archive.
            with tempfile.NamedTemporaryFile(dir=ROOT / "docs", suffix=".md"):
                for path in archives:
                    subprocess.run([sys.executable, str(ROOT / "transport-core/tools/package.py"),
                                    "--output", str(path)], check=True)
            self.assertEqual(archives[0].read_bytes(), archives[1].read_bytes())
            with tarfile.open(archives[0]) as archive:
                members = archive.getmembers()
                names = {m.name for m in members}
                tracked = set(filter(None, subprocess.check_output(
                    ["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")))
                self.assertEqual(names, tracked)
                self.assertEqual(len(names), len(members))
                for required in ("README.md", "LICENSE", "NOTICE", "Makefile", "SOURCE.json",
                                 "examples/pair.topology", ".github/workflows/ci.yml",
                                 "net-sircl/nccl/LICENSE.txt", "docs/transport-core.md",
                                 "RELEASE_NOTES-0.1.1.md", "RELEASE_NOTES-0.1.2.md", "net-sircl/ROUTE-ALLOWANCE.md",
                                 "net-sircl/net_sircl_tuner.h", "net-sircl/nccl/tuner_v4.h",
                                 "examples/torch_teardown.py", "scripts/check_teardown.py",
                                 "scripts/check_channels.py", "scripts/check_compat.py",
                                 "net-sircl/PROFILING.md", "net-sircl/net_sircl_plan.h",
                                 "net-sircl/net_sircl_plan.c", "net-sircl/net_sircl_admission.h",
                                 "net-sircl/net_sircl_profile.h", "net-sircl/net_sircl_compat.h",
                                 "fabric-tool/LICENSE", "fabric-tool/NOTICE", "fabric-tool/VERSION",
                                 "fabric-tool/mesh_marker.c", "fabric-tool/fabric_tool/cli.py",
                                 "fabric-tool/fabric-tool", "fabric-tool/tests/test_plan.py"):
                    self.assertIn(required, names)
                for member in members:
                    self.assertTrue(member.isfile())
                    self.assertNotIn("..", Path(member.name).parts)
                    self.assertFalse(set(Path(member.name).parts) & {".git", "build", ".core-build", "dist", "test-results"})
                    self.assertEqual(archive.extractfile(member).read(), (ROOT / member.name).read_bytes())
                    self.assertEqual(member.mtime, 0)

    def test_release_identity_is_built_in(self):
        subprocess.run(["make", "-C", str(ROOT / "net-sircl"), "emu"], check=True)
        library = ctypes.CDLL(str(ROOT / "net-sircl/libnccl-net-sircl-emu.so"))
        version = (ROOT / "net-sircl/VERSION").read_text().strip()
        self.assertEqual(ctypes.c_char_p(ctypes.addressof(
            ctypes.c_char.in_dll(library, "net_sircl_release"))).value.decode(), version)
        identity = subprocess.check_output([sys.executable, str(ROOT / "transport-core/tools/identity.py")]).strip()
        self.assertTrue(identity in (ROOT / "net-sircl/libnccl-net-sircl-emu.so").read_bytes(),
                        "compiled emulator lacks the expected transport source identity; rebuild it from this source tree")

    def test_all_example_routes_parse(self):
        subprocess.run(["make", "-C", str(ROOT / "net-sircl"), "emu"], check=True)
        library = ctypes.CDLL(str(ROOT / "net-sircl/libnccl-net-sircl-emu.so"))
        routes = library.net_sircl_topology_routes
        routes.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]
        for name, size in (("pair", 2), ("ring4", 4), ("ring8", 8), ("path4", 4)):
            for position in range(size):
                output = ctypes.create_string_buffer(4096)
                self.assertEqual(routes(str(ROOT / f"examples/{name}.topology").encode(), position,
                                        output, len(output)), 0, output.value)
                self.assertEqual(len(output.value.decode().strip(";").split(";")), size - 1)


if __name__ == "__main__":
    unittest.main()
