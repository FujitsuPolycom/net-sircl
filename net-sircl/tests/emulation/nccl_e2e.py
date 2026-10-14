#!/usr/bin/env python3
"""NCCL end to end through net-sircl on one GPU (GPU emulation; no RoCE device, no DGX Spark).

Two ranks share GPU 0. NCCL 2.32.3 accepts several ranks per GPU with NCCL_MULTI_RANK_GPU_ENABLE=1; with
NCCL_P2P_DISABLE=1 and
NCCL_SHM_DISABLE=1 its network transport is the only one left, so every byte goes through the network plugin:
net-sircl built against the shared-memory verbs stand-in (libnccl-net-sircl-emu.so), or NCCL's own socket
transport for comparison.

    nccl_e2e.py --launch --teardown release-graph|keep-graph [--net sircl|socket] [--windowed] --out DIR
    nccl_e2e.py --launch --native /path/to/nccl_compat_e2e --plugin PLUGIN --windowed --out DIR

The native backend uses distinct NCCL_HOSTID values to model two hosts on GPU 0 with older NCCL releases.
It checks uint32 data and captures all three collectives. It needs no PyTorch installation.

Each rank: bfloat16 all-reduce, all-gather and broadcast from 8 KiB to 64 MiB, eager, every element checked;
one CUDA graph of a 64 KiB all-reduce replayed 20 times with new inputs, checked; then teardown:
  release-graph  the graph is deleted and the stream synchronized before destroy_process_group (the order
                 nccl-tests uses);
  keep-graph     destroy_process_group while the captured graph still exists.
                 NCCL's destroy waits until every graph that holds the communicator is gone
                 (init.cc commDestroySync, "keep polling until all graphs referencing us die").
Each rank prints "PASS" after destroy returns. --launch starts both ranks with the environment and reports
per rank: exit status, whether PASS printed, and how long teardown took. --timeout bounds the whole run.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import threading

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
        graph.reset()
        del graph
        torch.cuda.synchronize()
    def expired():
        print(f"rank {rank}: FAIL teardown exceeded {args.teardown_timeout} s", flush=True)
        os._exit(124)
    deadline = threading.Timer(args.teardown_timeout, expired)
    deadline.daemon = True
    deadline.start()
    start = time.monotonic()
    print(f"rank {rank}: destroy_process_group ...", flush=True)
    dist.destroy_process_group()
    deadline.cancel()
    print(f"rank {rank}: teardown {time.monotonic() - start:.2f} s", flush=True)
    print(f"rank {rank}: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


def launch(args):
    os.makedirs(args.out, exist_ok=True)
    base = dict(os.environ, WORLD_SIZE=str(args.world_size), MASTER_ADDR="127.0.0.1", MASTER_PORT=str(29500 + os.getpid() % 500),
                NCCL_MULTI_RANK_GPU_ENABLE="1", NCCL_P2P_DISABLE="1", NCCL_SHM_DISABLE="1", NCCL_NVLS_ENABLE="0",
                NCCL_DEBUG="INFO",
                NCCL_DEBUG_SUBSYS="INIT,NET,TUNING", NCCL_SOCKET_IFNAME="lo")
    if args.net == "sircl":
        base.update(NCCL_NET="sircl", NCCL_NET_PLUGIN=args.plugin, NET_SIRCL_OOB_ADDR="127.0.0.1",
                    SIRCL_EMU_FABRIC=f"/sircl-emu-glm-e2e-{os.getpid()}", NET_SIRCL_STANDIN_DEVICES="4")
    else:
        base.update(NCCL_NET="Socket", NCCL_NET_PLUGIN="none")
    procs = []
    rendezvous = tempfile.TemporaryDirectory(prefix="net-sircl-nccl-")
    topology = os.path.join(rendezvous.name, "topology")
    with open(topology, "w") as f:
        hosts = "rank0 relay0 relay1 rank1" if args.windowed else " ".join(f"rank{r}" for r in range(args.world_size))
        f.write(f"hosts {hosts}\ncabling {args.layout}\nport0 emu0 emu1\nport1 emu2 emu3\n")
    for rank in range(args.world_size):
        env = dict(base, RANK=str(rank), NET_SIRCL_TOPOLOGY=topology,
                   NET_SIRCL_HCA="emu0,emu1,emu2,emu3",
                   NET_SIRCL_POSITION=str(rank * 3 if args.windowed else rank))
        # Model separate hosts for both sircl and the Socket control.
        env["NCCL_HOSTID"] = f"net-sircl-emulated-host-{rank}"
        env["NCCL_CUMEM_HOST_ENABLE"] = "0"
        log = open(os.path.join(args.out, f"rank{rank}.log"), "w")
        command = ([args.native, str(rank), os.path.join(rendezvous.name, "id")] if args.native else
                   [sys.executable, __file__, "--rank", "--teardown", args.teardown,
                    "--teardown-timeout", str(args.teardown_timeout)])
        procs.append((subprocess.Popen(command, env=env,
                                       stdout=log, stderr=subprocess.STDOUT), log))
    deadline = time.monotonic() + args.timeout
    results = []
    for rank, (p, log) in enumerate(procs):
        try:
            code = p.wait(timeout=max(1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            code = "timeout"
        log.close()
        text = open(os.path.join(args.out, f"rank{rank}.log")).read()
        teardown = next((line.split("teardown ")[1] for line in text.splitlines() if ": teardown " in line), None)
        results.append({"rank": rank, "exit": code, "pass": f"rank {rank}: PASS" in text,
                        "exact_lines": text.count(" exact"), "teardown": teardown,
                        "plugin": "Using network sircl" in text and "via NET/sircl/" in text and "via P2P" not in text,
                        "wrong": "WRONG" in text})
    if args.net == "sircl":
        shm = "/dev/shm" + base["SIRCL_EMU_FABRIC"]
        if os.path.exists(shm):
            os.unlink(shm)
    rendezvous.cleanup()
    summary = {"net": args.net, "teardown": args.teardown, "windowed": args.windowed,
               "backend": "native" if args.native else "pytorch", "ranks": results}
    print(json.dumps(summary))
    return 0 if all(r["pass"] and r["exit"] == 0 and not r["wrong"] and
                    (args.net != "sircl" or r["plugin"]) for r in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--rank", action="store_true")
    parser.add_argument("--teardown", choices=("release-graph", "keep-graph"), default="release-graph")
    parser.add_argument("--net", choices=("sircl", "socket"), default="sircl")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--world-size", type=int, default=2)
    parser.add_argument("--layout", choices=("path", "ring"), default="path")
    parser.add_argument("--teardown-timeout", type=float, default=15)
    parser.add_argument("--plugin", default="")
    parser.add_argument("--native", default="", help="path to the nccl_compat_e2e executable")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--out", default="/tmp/nccl-e2e")
    args = parser.parse_args()
    if args.world_size < 2 or args.world_size > 8 or args.teardown_timeout <= 0 or args.timeout <= 0:
        parser.error("world-size must be 2..8 and timeouts must be positive")
    if args.windowed and (args.world_size != 2 or args.layout != "path"):
        parser.error("--windowed models two endpoints of a four-host path")
    if args.native and args.world_size != 2:
        parser.error("the native backend models two ranks")
    if args.native and args.teardown != "release-graph":
        parser.error("the native backend releases its graph before communicator teardown")
    if args.rank:
        return rank_main(args)
    return launch(args)


if __name__ == "__main__":
    sys.exit(main())
