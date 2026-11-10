"""Source scanning contracts for archives without repository metadata."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCANNER = Path(__file__).resolve().parents[1] / "scripts/check_release_safety.py"


class ExtractedSourceTests(unittest.TestCase):
    def scan(self, path):
        return subprocess.run([sys.executable, str(SCANNER), "--tree", str(path)],
                              capture_output=True, text=True, timeout=10)

    def test_private_content_is_reported_without_disclosing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            private = "spark-" + "abcd"
            (path / "notes.txt").write_text(private)
            result = self.scan(path)
            self.assertEqual(result.returncode, 1)
            self.assertIn('"rule": "site-host"', result.stdout)
            self.assertNotIn(private, result.stdout)

    def test_documentation_address_is_accepted_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "guide.txt").write_text("Example address: 192.0.2.1")
            self.assertEqual(self.scan(path).returncode, 0)

    def test_an_empty_or_missing_tree_is_not_a_clean_one(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.scan(directory).returncode, 2)
            result = self.scan(Path(directory) / "absent")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("findings: 0", result.stdout)

    def test_a_nul_byte_does_not_hide_private_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            private = "spark-" + "abcd"
            (path / "blob.bin").write_bytes(b"\x89PNG\0\0" + private.encode() + b"\0tail")
            result = self.scan(path)
            self.assertEqual(result.returncode, 1)
            self.assertIn('"rule": "site-host"', result.stdout)
            self.assertNotIn(private, result.stdout)

    def test_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "guide.txt").write_text("example")
            (path / "link").symlink_to(path / "guide.txt")
            self.assertEqual(self.scan(path).returncode, 2)


if __name__ == "__main__":
    unittest.main()
