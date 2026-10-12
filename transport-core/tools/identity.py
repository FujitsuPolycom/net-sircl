#!/usr/bin/env python3
"""Identify authored transport inputs, excluding build artifacts and test outputs."""
import argparse
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_id():
    digest = hashlib.sha256()
    files = [ROOT / "VERSION", ROOT / "PROXY_INPUTS.json"]
    for directory in ("include", "src", "tools"):
        files.extend(p for p in (ROOT / directory).rglob("*") if p.is_file() and p.suffix in (".h", ".c", ".py"))
    for p in sorted(files):
        digest.update(p.relative_to(ROOT).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    value = source_id()
    if args.output:
        data = f'#define SIRCL_CORE_BUILD_ID "{value}"\n'.encode()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if not args.output.exists() or args.output.read_bytes() != data:
            args.output.write_bytes(data)
    else:
        print(value)
