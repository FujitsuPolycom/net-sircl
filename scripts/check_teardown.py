"""GPU regression: four-rank path teardown plus optional retained-graph negative controls.

Run under the site's GPU reservation, with CUDA PyTorch and the selected NCCL runtime.
Every rank must complete eager and graph data checks and destroy its group in 15 seconds.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "test-results/teardown")
    args = parser.parse_args()
    for net in ("sircl", "socket"):
        modes = ("release-graph", "keep-graph") if args.negative_controls else ("release-graph",)
        for mode in modes:
            out = args.out / f"{net}-{mode}"
            command = [sys.executable, str(ROOT / "net-sircl/tests/emulation/nccl_e2e.py"),
                       "--launch", "--net", net, "--teardown", mode, "--world-size", "4",
                       "--plugin", str(ROOT / "net-sircl/libnccl-net-sircl-emu.so"),
                       "--timeout", "180", "--teardown-timeout", "15", "--out", str(out)]
            env = dict(os.environ, NCCL_ALGO="Ring")
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=200)
            print(result.stdout, end="", flush=True)
            print(result.stderr, end="", file=sys.stderr)
            summary = json.loads(result.stdout.strip().splitlines()[-1])
            (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            ranks = summary["ranks"]
            assert len(ranks) == 4
            assert all(r["exact_lines"] == 7 and not r["wrong"] for r in ranks), summary
            if net == "sircl":
                assert all(r["plugin"] for r in ranks), summary
            if mode == "release-graph":
                assert result.returncode == 0 and all(r["pass"] and r["exit"] == 0 for r in ranks), summary
                assert all(float(r["teardown"].split()[0]) < 15 for r in ranks), summary
            else:
                assert result.returncode != 0
                assert all(r["exit"] == 124 and not r["pass"] for r in ranks), summary
                for rank in range(4):
                    assert "FAIL teardown exceeded 15.0 s" in (out / f"rank{rank}.log").read_text()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
