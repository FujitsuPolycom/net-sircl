"""Run the native GPU collective/graph/teardown fixture against explicit NCCL libraries.

Example: python scripts/check_compat.py --native /path/nccl_compat_e2e \
  --runtime 23102=/path/libnccl.so.2 --runtime 23203=/other/libnccl.so.2
Run under the site's GPU reservation. Does not install or replace framework libraries.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ABIS = {22707: 10, 22809: 11, 23102: 12, 23203: 12}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--native", required=True, type=Path)
    p.add_argument("--runtime", required=True, action="append", help="NCCL integer version=library path")
    p.add_argument("--out", type=Path, default=ROOT / "test-results/compat")
    args = p.parse_args()
    for spec in args.runtime:
        value, library = spec.split("=", 1)
        version = int(value)
        if version not in ABIS:
            p.error(f"no expected ABI recorded for runtime {version}")
        library = Path(library).resolve(strict=True)
        env = dict(os.environ, LD_PRELOAD=str(library), NCCL_ALGO="Ring")
        env["LD_LIBRARY_PATH"] = str(library.parent) + ":" + env.get("LD_LIBRARY_PATH", "")
        for windowed in (False, True):
            out = args.out / f"{version}-{'relayed' if windowed else 'direct'}"
            command = [sys.executable, str(ROOT / "net-sircl/tests/emulation/nccl_e2e.py"),
                       "--launch", "--native", str(args.native.resolve()),
                       "--plugin", str(ROOT / "net-sircl/libnccl-net-sircl-emu.so"), "--out", str(out)]
            if windowed:
                command.append("--windowed")
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=260)
            assert result.returncode == 0, result.stdout + result.stderr
            summary = json.loads(result.stdout.strip().splitlines()[-1])
            assert all(r["plugin"] and r["exact_lines"] == 7 and r["pass"] for r in summary["ranks"]), summary
            for rank in range(2):
                log = (out / f"rank{rank}.log").read_text()
                assert f"NCCL runtime {version}" in log
                assert f"(v{ABIS[version]})" in log or f"ext-net v{ABIS[version]}" in log
            summary.update(runtime=version, abi=ABIS[version], library_sha256=hashlib.sha256(library.read_bytes()).hexdigest())
            (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
