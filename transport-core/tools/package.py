#!/usr/bin/env python3
"""Archive this Git checkout's tracked source files with reproducible metadata."""
import argparse
import gzip
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def tracked_files():
    # Refuse a parent checkout when invoked from an extracted archive.
    if not (ROOT / ".git").exists():
        raise ValueError("source packaging requires this repository's Git checkout")
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    files = []
    for name in sorted(set(filter(None, names))):
        path = ROOT / name
        if (path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(ROOT)):
            raise ValueError(f"tracked source is missing or not a regular file: {name}")
        if (set(Path(name).parts) & {".git", "build", ".core-build", "__pycache__", ".pytest_cache", "dist", "test-results"}
                or name == "scripts/config/site-values.txt"
                or path.suffix in {".so", ".o", ".a", ".d", ".pyc"}):
            raise ValueError(f"generated or private file must not be tracked: {name}")
        files.append((name, path))
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="output .tar.gz source archive")
    args = parser.parse_args()
    try:
        members = tracked_files()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    if args.output.resolve() in {path.resolve() for _, path in members}:
        parser.error("output must not overwrite a tracked source file")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, path in members:
                info = archive.gettarinfo(str(path), name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o644
                with path.open("rb") as source:
                    archive.addfile(info, source)
    print(args.output)


if __name__ == "__main__":
    main()
