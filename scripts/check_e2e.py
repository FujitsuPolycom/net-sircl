"""Run both NCCL emulation modes; only an explicit optional mode may skip."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--if-available", action="store_true")
    args = parser.parse_args()
    reason = None
    try:
        import torch
        if not torch.cuda.is_available():
            reason = "no CUDA GPU available to PyTorch"
        else:
            # Query the dynamically loaded runtime, not PyTorch's build-time version.
            import ctypes
            nccl = ctypes.CDLL("libnccl.so.2")
            version = ctypes.c_int()
            if nccl.ncclGetVersion(ctypes.byref(version)) != 0 or version.value != 23203:
                reason = "runtime NCCL 2.32.3 is required by this test"
    except (ImportError, OSError, AttributeError) as exc:
        reason = f"CUDA PyTorch/NCCL prerequisite unavailable ({type(exc).__name__})"
    if reason:
        print(f"{'SKIP' if args.if_available else 'ERROR'}: {reason}", flush=True)
        return 0 if args.if_available else 1
    plugin = ROOT / "net-sircl/libnccl-net-sircl-emu.so"
    if not plugin.is_file():
        print("ERROR: build the emulated plugin with make emu")
        return 1
    for windowed in (False, True):
        output = ROOT / "test-results" / ("windowed" if windowed else "plain")
        command = [sys.executable, str(ROOT / "net-sircl/tests/emulation/nccl_e2e.py"),
                   "--launch", "--plugin", str(plugin), "--out", str(output)]
        if windowed:
            command.append("--windowed")
        result = subprocess.run(command, text=True, capture_output=True, timeout=300)
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr, flush=True)
        if result.returncode:
            return result.returncode
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        if not all(r["plugin"] and r["exact_lines"] == 7 and not r["wrong"] for r in summary["ranks"]):
            raise RuntimeError("NCCL did not verify every collective through sircl")
        for rank in range(2):
            log = (output / f"rank{rank}.log").read_text()
            if "net-sircl 0.1.0 (wire version 2)" not in log or "transport-core 0.1.0 ABI 1 source " not in log:
                raise RuntimeError("initialization identity missing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
