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

    def test_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "guide.txt").write_text("example")
            (path / "link").symlink_to(path / "guide.txt")
            self.assertEqual(self.scan(path).returncode, 2)


if __name__ == "__main__":
    unittest.main()
