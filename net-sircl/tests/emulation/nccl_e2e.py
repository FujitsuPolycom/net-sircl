#!/usr/bin/env python3
"""NCCL end to end through net-sircl on one GPU (GPU emulation; no RoCE device, no DGX Spark).

Two ranks share GPU 0. NCCL 2.32.3 accepts several ranks per GPU with NCCL_MULTI_RANK_GPU_ENABLE=1; with
NCCL_P2P_DISABLE=1 and
NCCL_SHM_DISABLE=1 its network transport is the only one left, so every byte goes through the network plugin:
net-sircl built against the shared-memory verbs stand-in (libnccl-net-sircl-emu.so), or NCCL's own socket
transport for comparison.

    nccl_e2e.py --launch --teardown release-graph|keep-graph [--net sircl|socket] [--windowed] --out DIR

Each rank: bfloat16 all-reduce, all-gather and broadcast from 8 KiB to 64 MiB, eager, every element checked;
one CUDA graph of a 64 KiB all-reduce replayed 20 times with new inputs, checked; then teardown:
  release-graph  the graph is deleted and the stream synchronized before destroy_process_group (the order
                 nccl-tests uses);
  keep-graph     destroy_process_group while the captured graph still exists (the order of the 2026-10-11 T1
                 script). NCCL's destroy waits until every graph that holds the communicator is gone
                 (init.cc commDestroySync, "keep polling until all graphs referencing us die").
Each rank prints "PASS" after destroy returns. --launch starts both ranks with the environment and reports
per rank: exit status, whether PASS printed, and how long teardown took (a 60 s limit for teardown).
"""
import argparse
import json
import os
import subprocess
import sys
import time

SIZES = [8 << 10, 64 << 10, 512 << 10, 4 << 20, 32 << 20, 64 << 20]


def rank_main(args):
    import torch
    import torch.distributed as dist

    rank, world = int(os.environ["RANK"]), int(os.environ["WORLD_SIZE"])
    torch.cuda.set_device(0)
    device = torch.device("cuda", 0)
    dist.init_process_group("nccl", rank=rank, world_size=world, device_id=device)
    ok = True

    def pattern(n, r):
        return ((torch.arange(n, device=device) % 7 + 1).to(torch.float32) * (r + 1)).to(torch.bfloat16)

    for size in SIZES:
        n = size // 2
        x = pattern(n, rank)
        dist.all_reduce(x)
        good = torch.equal(x, (pattern(n, 0).float() * (world * (world + 1) / 2)).to(torch.bfloat16))
        out = [torch.empty(n, device=device, dtype=torch.bfloat16) for _ in range(world)]
        dist.all_gather(out, pattern(n, rank))
        good &= all(torch.equal(out[r], pattern(n, r)) for r in range(world))
        y = pattern(n, rank)
        dist.broadcast(y, src=1)
        good &= torch.equal(y, pattern(n, 1))
        torch.cuda.synchronize()
        ok &= good
        print(f"rank {rank}: {size} B all-reduce/all-gather/broadcast {'exact' if good else 'WRONG'}", flush=True)

    n = (64 << 10) // 2
    x = torch.zeros(n, device=device, dtype=torch.bfloat16)
    stream = torch.cuda.Stream()
    stream.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(stream):
        dist.all_reduce(x)
    torch.cuda.current_stream().wait_stream(stream)
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph):
        dist.all_reduce(x)
    want = (pattern(n, 0).float() * (world * (world + 1) / 2)).to(torch.bfloat16)
    for _ in range(20):
        x.copy_(pattern(n, rank))
        graph.replay()
        torch.cuda.synchronize()
        ok &= torch.equal(x, want)
    print(f"rank {rank}: graph 64 KiB x20 {'exact' if ok else 'WRONG'}", flush=True)

    if args.teardown == "release-graph":
        del graph
        torch.cuda.synchronize()
    start = time.monotonic()
    print(f"rank {rank}: destroy_process_group ...", flush=True)
    dist.destroy_process_group()
    print(f"rank {rank}: teardown {time.monotonic() - start:.2f} s", flush=True)
    print(f"rank {rank}: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


def launch(args):
    os.makedirs(args.out, exist_ok=True)
    base = dict(os.environ, WORLD_SIZE="2", MASTER_ADDR="127.0.0.1", MASTER_PORT=str(29500 + os.getpid() % 500),
                NCCL_MULTI_RANK_GPU_ENABLE="1", NCCL_P2P_DISABLE="1", NCCL_SHM_DISABLE="1", NCCL_NVLS_ENABLE="0",
                NCCL_DEBUG="INFO",
                NCCL_DEBUG_SUBSYS="INIT,NET", NCCL_SOCKET_IFNAME="lo")
    if args.net == "sircl":
        base.update(NCCL_NET="sircl", NCCL_NET_PLUGIN=args.plugin, NET_SIRCL_OOB_ADDR="127.0.0.1",
                    SIRCL_EMU_FABRIC=f"/sircl-emu-glm-e2e-{os.getpid()}", NET_SIRCL_STANDIN_DEVICES="2")
    else:
        base.update(NCCL_NET="Socket", NCCL_NET_PLUGIN="none")
    procs = []
    for rank in range(2):
        env = dict(base, RANK=str(rank))
        if args.windowed and args.net == "sircl":
            peer = 1 - rank
            env.update(NET_SIRCL_POSITION=str(rank), NET_SIRCL_ROUTES=f"{peer}=emu0/emu1",
                       NET_SIRCL_WINDOWS=f"{peer}=131072", NET_SIRCL_CHUNK="32768")
        log = open(os.path.join(args.out, f"rank{rank}.log"), "w")
        procs.append((subprocess.Popen([sys.executable, __file__, "--rank", "--teardown", args.teardown], env=env,
                                       stdout=log, stderr=subprocess.STDOUT), log))
    deadline = time.monotonic() + args.timeout
    results = []
    for rank, (p, log) in enumerate(procs):
        try:
            code = p.wait(timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()
            code = "timeout"
        log.close()
        text = open(os.path.join(args.out, f"rank{rank}.log")).read()
        teardown = next((line.split("teardown ")[1] for line in text.splitlines() if ": teardown " in line), None)
        results.append({"rank": rank, "exit": code, "pass": f"rank {rank}: PASS" in text,
                        "exact_lines": text.count(" exact"), "teardown": teardown,
                        "plugin": "Using network sircl" in text, "wrong": "WRONG" in text})
    if args.net == "sircl":
        shm = "/dev/shm" + base["SIRCL_EMU_FABRIC"]
        if os.path.exists(shm):
            os.unlink(shm)
    summary = {"net": args.net, "teardown": args.teardown, "windowed": args.windowed, "ranks": results}
    print(json.dumps(summary))
    return 0 if all(r["pass"] and r["exit"] == 0 for r in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--rank", action="store_true")
    parser.add_argument("--teardown", choices=("release-graph", "keep-graph"), default="release-graph")
    parser.add_argument("--net", choices=("sircl", "socket"), default="sircl")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--plugin", default="")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--out", default="/tmp/nccl-e2e")
    args = parser.parse_args()
    if args.rank:
        return rank_main(args)
    return launch(args)


if __name__ == "__main__":
    sys.exit(main())
