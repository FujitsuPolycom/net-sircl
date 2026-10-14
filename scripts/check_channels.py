"""NCCL 2.32.3 GPU emulation: automatic tuner policy and actual per-operation channels.

Use a GPU reservation. This checks active channels, not the larger pool allocated at init.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES = ("NCCL_MAX_NCHANNELS", "NCCL_MIN_NCHANNELS", "NCCL_MAX_NRINGS", "NCCL_MIN_NRINGS",
             "NCCL_MAX_CTAS", "NCCL_MIN_CTAS", "NCCL_TUNER_PLUGIN", "NET_SIRCL_CHANNELS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "test-results/channels")
    args = parser.parse_args()
    cases = [("path4", 4, "path", {}, 8), ("cycle8", 8, "ring", {}, 8),
             ("pair", 2, "path", {}, 0),
             ("override4", 4, "path", {"NCCL_MAX_NCHANNELS": "4"}, 0),
             ("override16", 4, "path", {"NCCL_MAX_NCHANNELS": "16"}, 0),
             ("disabled", 4, "path", {"NCCL_TUNER_PLUGIN": "none"}, None)]
    for name, world, layout, overrides, policy in cases:
        env = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
        env.update(NCCL_ALGO="Ring", **overrides)
        out = args.out / name
        command = [sys.executable, str(ROOT / "net-sircl/tests/emulation/nccl_e2e.py"),
                   "--launch", "--world-size", str(world), "--layout", layout,
                   "--plugin", str(ROOT / "net-sircl/libnccl-net-sircl-emu.so"), "--out", str(out)]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=260)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        assert result.returncode == 0, result.stdout + result.stderr
        assert all(r["exact_lines"] == 7 and r["plugin"] for r in summary["ranks"]), summary
        counts = []
        for rank in range(world):
            log = (out / f"rank{rank}.log").read_text()
            if policy is not None:
                assert f"net-sircl tuner: collective channel limit {policy} " in log
            else:
                assert "net-sircl tuner:" not in log
            spans = re.findall(r"channel\{Lo\.\.Hi\}=\{(\d+)\.\.(\d+)\}", log)
            assert spans, "NCCL 2.32.3 TUNING operation logs required"
            active = [int(hi) - int(lo) + 1 for lo, hi in spans]
            assert min(active) > 0
            limit = policy or int(overrides.get("NCCL_MAX_NCHANNELS", "64"))
            assert max(active) <= limit, (name, rank, active)
            if name in ("pair", "override16", "disabled"):
                assert max(active) > 8, (name, rank, active)
            counts.append(max(active))
        summary["max_active_channels"] = counts
        summary["case"] = name
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
