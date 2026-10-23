"""Record the complete distributed C, C++, and header inventory and its SHA-256 hashes."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp"}


def main():
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    sources = sorted(name for name in names if name and not name.startswith(".codegraph/")
                     and Path(name).suffix in SUFFIXES and (ROOT / name).is_file())
    path = ROOT / "SOURCE.json"
    document = json.loads(path.read_text())
    document["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
