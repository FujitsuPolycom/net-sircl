#!/usr/bin/env python3
"""Bounded PyTorch eager/graph/teardown check; launch one rank per GPU with torchrun.

Every rank runs this with RANK, WORLD_SIZE, MASTER_ADDR and MASTER_PORT set (no launcher needed) and the arm's
NCCL environment. bfloat16 all-reduce (sum) at 8 KiB, 32 KiB, 128 KiB, 512 KiB, 2 MiB, 8 MiB, 32 MiB and
256 MiB, eager, then one CUDA graph of a 64 KiB all-reduce replayed 20 times with new inputs. Rank r
contributes (r + 1) * (i % 7 + 1) at element i, so every element of the sum is exact in bfloat16 for up to 8
ranks. Prints one line per check and a final PASS or FAIL; exit status 0 only on PASS.
"""
import os
import sys
import time
import threading

import torch
import torch.distributed as dist

SIZES = [8 << 10, 32 << 10, 128 << 10, 512 << 10, 2 << 20, 8 << 20, 32 << 20, 256 << 20]


def expected(world, n, device):
    pattern = (torch.arange(n, device=device) % 7 + 1).to(torch.float32)
    return (pattern * (world * (world + 1) / 2)).to(torch.bfloat16)


def main():
    rank, world = int(os.environ["RANK"]), int(os.environ["WORLD_SIZE"])
    torch.cuda.set_device(0)
    device = torch.device("cuda", 0)
    dist.init_process_group("nccl", rank=rank, world_size=world, device_id=device)
    print(f"rank {rank}/{world}: torch {torch.__version__}, NCCL {torch.cuda.nccl.version()}, "
          f"NCCL_NET={os.environ.get('NCCL_NET', '')}, NCCL_NET_PLUGIN={os.environ.get('NCCL_NET_PLUGIN', '')}",
          flush=True)
    ok = True
    for size in SIZES:
        n = size // 2
        x = ((torch.arange(n, device=device) % 7 + 1).to(torch.float32) * (rank + 1)).to(torch.bfloat16)
        torch.cuda.synchronize()
        start = time.perf_counter()
        dist.all_reduce(x)
        torch.cuda.synchronize()
        us = (time.perf_counter() - start) * 1e6
        good = torch.equal(x, expected(world, n, device))
        ok &= good
        print(f"rank {rank}: eager {size} B {'exact' if good else 'WRONG'} ({us:.0f} us, first call)", flush=True)
    n = (64 << 10) // 2
    x = torch.zeros(n, device=device, dtype=torch.bfloat16)
    stream = torch.cuda.Stream()
    stream.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(stream):
        dist.all_reduce(x)  # warm-up outside capture, as PyTorch requires for NCCL
    torch.cuda.current_stream().wait_stream(stream)
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph):
        dist.all_reduce(x)
    for replay in range(20):
        x.copy_(((torch.arange(n, device=device) % 7 + 1).to(torch.float32) * (rank + 1)).to(torch.bfloat16))
        graph.replay()
        torch.cuda.synchronize()
        good = torch.equal(x, expected(world, n, device))
        ok &= good
        if not good:
            print(f"rank {rank}: graph replay {replay} WRONG", flush=True)
    print(f"rank {rank}: graph 64 KiB x20 {'exact' if ok else 'WRONG'}", flush=True)
    # NCCL finalize waits for CUDA graph references before it closes the network.
    # reset releases the executable even if another Python reference exists.
    graph.reset()
    del graph
    torch.cuda.synchronize()
    def expired():
        print(f"rank {rank}: FAIL teardown exceeded 15 s", flush=True)
        os._exit(124)
    deadline = threading.Timer(15, expired)
    deadline.daemon = True
    deadline.start()
    start = time.monotonic()
    print(f"rank {rank}: destroy_process_group ...", flush=True)
    dist.destroy_process_group()
    deadline.cancel()
    print(f"rank {rank}: teardown {time.monotonic() - start:.2f} s", flush=True)
    print(f"rank {rank}: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
