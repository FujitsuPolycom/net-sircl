#!/usr/bin/env python3
"""Create a standalone net-sircl/core source archive; no build or network action."""
import argparse
import gzip
from pathlib import Path
import tarfile

CORE = Path(__file__).resolve().parents[1]
TRANSPORT = CORE.parent
STANDALONE = (TRANSPORT / "net-sircl" / "VERSION").is_file()
RELEASE_FILES = (".gitattributes", ".gitignore", "LICENSE", "NOTICE", "README.md", "Makefile", "SOURCE.json")
RELEASE_DIRS = (".github", "docs", "examples", "scripts", "tests")


def include(path, relative):
    if any(p in {"build", ".core-build", "__pycache__", ".pytest_cache", ".git", "dist", "test-results"} for p in relative.parts):
        return False
    if relative.as_posix() == "scripts/config/site-values.txt":
        return False
    return path.is_file() and path.suffix not in {".so", ".o", ".a", ".d", ".pyc"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--include-libsircl", action="store_true")
    args = parser.parse_args()
    trees = {"transport-core": CORE, "net-sircl": TRANSPORT / ("net-sircl" if STANDALONE else "nccl/net-sircl")}
    if args.include_libsircl:
        trees["libsircl"] = TRANSPORT / "libsircl"
    for root in trees.values():
        if not (root / "VERSION").is_file():
            parser.error(f"missing source component: {root.name}")
    members = []
    for name, root in trees.items():
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if include(path, relative):
                members.append((f"{name}/{relative.as_posix()}", path))
    if STANDALONE:
        members.extend((name, TRANSPORT / name) for name in RELEASE_FILES)
        for name in RELEASE_DIRS:
            for path in (TRANSPORT / name).rglob("*"):
                relative = path.relative_to(TRANSPORT)
                if include(path, relative):
                    members.append((relative.as_posix(), path))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, path in sorted(members):
                info = archive.gettarinfo(str(path), name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o644
                with path.open("rb") as f:
                    archive.addfile(info, f)
    print(args.output)


if __name__ == "__main__":
    main()
