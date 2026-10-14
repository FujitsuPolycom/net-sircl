#!/usr/bin/env python3
"""Merge NET_SIRCL_PROFILE snapshots from one job (never mix repeated jobs)."""
import argparse
import glob
import json
from collections import defaultdict
from pathlib import Path


def summarize(paths):
    groups = defaultdict(lambda: defaultdict(int))
    threads = []
    seen = set()
    for path in paths:
        doc = json.loads(Path(path).read_text())
        if doc.get("schema") != 1:
            raise ValueError(f"{path}: unsupported profile schema")
        identity = (doc["host"], doc["pid"], doc["epoch"], doc["tid"])
        if identity in seen:
            raise ValueError(f"{path}: duplicate thread snapshot")
        seen.add(identity)
        threads.append({k: doc[k] for k in ("host", "rank", "pid", "tid", "wall_ns", "thread_cpu_ns")})
        if doc["lost_observations"]:
            raise ValueError(f"{path}: incomplete profile: lost observations")
        for row in doc["rows"]:
            g = groups[(doc["rank"], row["function"])]
            for key, value in row.items():
                if key in ("comm_id", "qp", "peer", "relay", "function"):
                    continue
                if isinstance(value, int):
                    g[key] += value
                elif isinstance(value, dict):
                    h = g.setdefault(key, {"count": 0, "sum": 0, "min": 0, "max": 0, "log2_bins": [0] * 65})
                    if value["count"]:
                        h["min"] = min(h["min"], value["min"]) if h["count"] else value["min"]
                    h["count"] += value["count"]
                    h["sum"] += value["sum"]
                    h["max"] = max(h["max"], value["max"])
                    h["log2_bins"] = [a + b for a, b in zip(h["log2_bins"], value["log2_bins"], strict=True)]
    return {"functions": [{"rank": k[0], "function": k[1], **v} for k, v in sorted(groups.items())],
            "threads": threads}


def mean(row, key):
    h = row[key]
    return h["sum"] / h["count"] if h["count"] else 0


def markdown(result):
    lines = ["| Rank | Function | Posted bytes | WRs | Data WRs | Polls | Empty % | CQEs/nonempty | Waits | Wait total ms | Chunk CQ us | Seen-to-credit us |",
             "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in result["functions"]:
        empty = 100 * r["empty"] / r["polls"] if r["polls"] else 0
        lines.append(f'| {r["rank"]} | {r["function"]} | {r["bytes"]} | {r["wrs"]} | {r["data_wrs"]} | '
                     f'{r["polls"]} | {empty:.2f} | {mean(r, "cqes"):.2f} | {r["waits"]} | '
                     f'{r["wait_ns"]["sum"] / 1e6:.3f} | {mean(r, "completion_ns") / 1e3:.3f} | '
                     f'{mean(r, "credit_usable_ns") / 1e3:.3f} |')
    lines += ["", "Latency columns are means; merged JSON retains histograms. Waits can overlap across requests.",
              "Remote-completion-to-credit latency is unavailable in ordinary verbs CQEs.", "",
              "| Rank | PID | Proxy TID | Wall ms | Thread CPU ms | CPU/wall % |",
              "|---:|---:|---:|---:|---:|---:|"]
    for t in result["threads"]:
        ratio = 100 * t["thread_cpu_ns"] / t["wall_ns"] if t["wall_ns"] else 0
        lines.append(f'| {t["rank"]} | {t["pid"]} | {t["tid"]} | {t["wall_ns"] / 1e6:.3f} | '
                     f'{t["thread_cpu_ns"] / 1e6:.3f} | {ratio:.2f} |')
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="JSON files or quoted glob patterns for ONE job")
    parser.add_argument("--json", type=Path, help="write merged counters and histograms")
    args = parser.parse_args()
    paths = [Path(p) for pattern in args.files for p in sorted(glob.glob(pattern))]
    if not paths:
        parser.error("no profile files matched")
    result = summarize(paths)
    if args.json:
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    print(markdown(result), end="")


if __name__ == "__main__":
    main()
