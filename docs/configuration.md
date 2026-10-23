# Configuration


A text file, the same on every host, `#` for comments:

```text
# Ring A: four Sparks cabled as a ring (host i's port 0 to host i+1's port 1)
hosts node-0 node-1 node-2 node-3
cabling ring
```

| Key | Meaning |
|---|---|
| `hosts <name> ...` | host names in position order; a host finds its own position by its host name (`NET_SIRCL_POSITION` overrides) |
| `cabling ring` / `path` / `explicit` | `ring`: host i's port 0 is cabled to host i+1's port 1, and the last host to the first; `path`: the same without the closing cable; `explicit`: only `cable` lines |
| `cable <i>.port<p> <j>.port<q>` | one cable (repeatable) |
| `port0 <fn> <fn>`, `port1 <fn> <fn>` | the RDMA functions of each port, lane order (default the DGX OS names above) |
| `relay_budget <bytes>` | directional budget ceiling, 65536–393216 bytes; default ceiling 393216, further limited by `NET_SIRCL_RELAY_BUDGET` |

A topology file is required, including for a direct pair. The plugin derives the shortest cabled path,
the local functions of its two lanes, the relays on the way and the source allowance. Each physical port
must have at most one cable; host names must be unique. Expose both functions required by each route. Where two directions are equally short (the opposite host of an
even ring), lane 0 takes one direction and lane 1 the other, as verified by the standalone route and queue-model tests.

The NCCL ranks may be a subset of the hosts: a path of three Sparks inside a ring of four lists all four hosts,
and the fourth relays.

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `NET_SIRCL_TOPOLOGY` | required | topology shared by all hosts, including relay-only hosts |
| `NET_SIRCL_POSITION` | from the topology file's host names | this process's position |
| `NET_SIRCL_ROUTES` | unset | optional `<position>=<function>/<function>,...`; must exactly match topology lanes |
| `NET_SIRCL_WINDOWS` | unset | `<position>=<bytes>,...`; direct peers: optional per-function payload cap; relayed peers: can only lower the shared source allowance; 0 is rejected for relayed peers |
| `NET_SIRCL_HCA` | every active RoCE function | comma list of functions to expose |
| `NET_SIRCL_GID_INDEX` | the function's RoCE v2 IPv4 GID | GID index |
| `NET_SIRCL_RELAY_BUDGET` | 327680 | base budget B (split limits: 6B/5 forward, B/5 ACK) per direction/function; decimal 65536–327680, limited by topology `relay_budget` |
| `NET_SIRCL_CHUNK` | 65536 | maximum payload bytes per independently completed chunk; adapts to available credit |
| `NET_SIRCL_OOB_ADDR` | first IPv4 of `NCCL_SOCKET_IFNAME` | address for the connection exchange |
| `NET_SIRCL_CONNECT_TIMEOUT_MS` | 60000 | connect/accept deadline |
| `NET_SIRCL_REQUEST_TIMEOUT_MS` | 60000 | deadline for a pending send/receive, including waiting for clear-to-send |
| `NET_SIRCL_CHANNELS` | 8 on topologies with more than two hosts; otherwise 0 | tuner collective limit, 1–64; 0 leaves selection to NCCL; explicit NCCL channel settings take precedence |
| `NET_SIRCL_IB_TIMEOUT` | 20 | RC ACK timeout exponent, decimal 1-31; one timeout is 4.096 microseconds times 2^value |
| `NET_SIRCL_IB_RETRY_CNT` | 7 | RC transport retry count, decimal 0-7 (0 disables retries); RNR retries remain 7 |
| `NET_SIRCL_RELAXED_ORDERING` | 1 | register memory with relaxed ordering |
| `NET_SIRCL_ROUTE_CHECK` | 1 | refuse a peer the function's interface has no route to |
| `NET_SIRCL_SPEED_MBPS` | 100000 | speed reported to NCCL's tuner, per function |
| `NET_SIRCL_TC` | 0 | RoCE traffic class |

`NET_SIRCL_ROUTES` may confirm topology lanes, but cannot change them. Unknown peers fail connection
admission. Invalid window maps fail initialization; a relayed peer's override cannot remove or raise the
directional cap. A smaller override lowers every function's source allowance on that host.
Direct peers have window 0 unless explicitly capped. The directional budget accepts decimal digits only;
empty, signed, malformed, overflowing and out-of-range values fail initialization.

Both timeout settings accept decimal integers in 1–2147483647 milliseconds.
Invalid settings fail initialization. Request deadlines start at receive submission
or the sender's first wait for clear-to-send; after admission, the send gets its
own completion deadline. They are total request bounds, not idle timers, so choose
a value larger than the slowest expected individual transfer. Health is checked
on the first and every 4096th waiting progress call; detection requires NCCL to
continue polling. Peer loss or deadline expiry returns `ncclRemoteError` and stays
failed on subsequent calls. Relayed connections also poison their shared allowance,
so abandoned credits cannot fund another transfer. Close abandons QPs without
waiting for remote recovery. Use an outer application timeout too: the plugin cannot
time out CUDA or a process that no longer calls it.

If a relay loses a rule, `fabric-tool verify` reports the missing filter and returns
nonzero. A filter with no `in_hw` flag also fails verification, even if its actions
still match. Verification is read-only; restore through the normal reviewed apply
workflow before creating a fresh process group.

The IB timeout and retry count are read at the first initialization and shared by live communicators.
Unset values use the defaults; empty, malformed, overflowing or out-of-range values fail initialization.
Signs and whitespace are not accepted. `NCCL_IB_TIMEOUT` and `NCCL_IB_RETRY_CNT` do not override these
plugin settings. Timeout 0 (infinite in verbs) is deliberately excluded to retain finite ACK timeouts.
Defaults match [NVIDIA NCCL v2.32.3-1](https://github.com/NVIDIA/nccl/blob/v2.32.3-1/src/transport/net_ib/connect.cc).
Exponent 20 allows about 4.3 seconds per ACK timeout, compared with about 67 milliseconds at 14.
The longer retry budget tolerates longer ACK stalls but delays failure or recovery of dropped tail packets.
It is a mitigation, not evidence that a fabric or receiver stall has been resolved.

## Relay allowances and profiling

See [the route allowance proof](../net-sircl/ROUTE-ALLOWANCE.md) for the
source/function allocation, separate forward and ACK ledgers, fallback model,
queue assumptions and oldest-first credit scheduling. The topology must list
all forwarding hosts, including hosts outside the active communicator.

`NET_SIRCL_PROFILE=/existing/writable/directory/job-prefix` enables cumulative
JSON profiling. Unset or empty disables it. See [profiling](../net-sircl/PROFILING.md)
for the schema, overhead and `net-sircl/tools/summarize_profile.py`.

## Channel policy

The bundled tuner supplies an eight-channel collective limit on multi-node
topologies with more than two hosts. Two-host topologies keep NCCL defaults.
Explicit NCCL channel settings disable the tuner limit. See
[automatic policy, override precedence and source attribution](channels.md).

Set `NCCL_NET_PLUGIN` to the installed library and `NCCL_NET=sircl` to require it.
Use `NCCL_ALGO=Ring` on rings and paths and `NCCL_DEBUG=INFO
NCCL_DEBUG_SUBSYS=INIT,NET` for diagnostics.

Use one interface name or prefix in `NCCL_SOCKET_IFNAME`. The plugin does not
parse NCCL's list/exclusion/exact-match syntax; configure `NET_SIRCL_OOB_ADDR`
explicitly if using that syntax. Loopback is only suitable for local tests.

## PyTorch and CUDA graphs

DDP and `torch.distributed` use the plugin through NCCL without a framework patch.
Check the NCCL INIT log for the library actually loaded; PyTorch's compiled-against
version may differ from a preloaded NCCL library.

Delete **all captured graphs referencing the communicator** before teardown:

```python
graph.reset()  # release the executable even if Python aliases remain
del graph  # reset/release every other captured graph too
torch.cuda.synchronize()
torch.distributed.destroy_process_group()
```

NCCL waits for graph references during communicator destruction. Keeping them
alive can hang teardown with either this plugin or NCCL's socket transport.
The [emulated test](../net-sircl/tests/emulation/nccl_e2e.py) demonstrates the order.
`make check-teardown` tests four ranks on an emulated path with a 15-second
teardown deadline. `python scripts/check_teardown.py --negative-controls` also
checks that retained graphs reach that deadline with sircl and stock Socket.
Use [the hardware check](../examples/torch_teardown.py) under an outer 150-second
job timeout. It checks eight eager all-reduce sizes and 20 graph replays, releases
the graph, then enforces a separate 15-second teardown bound.

Graph references must be released before communicator destruction. NCCL
2.32.3's [commDestroySync](https://github.com/NVIDIA/nccl/blob/v2.32.3-1/src/init.cc#L3122)
waits for those references before stopping proxy threads and closing the network.
The recorded four-host path check passed nine exact checks per rank and reported
0.54–0.68 seconds for destruction, with `PASS` and exit 0 on all ranks.
The [qualification table](qualification.md) identifies the source revision and
conditions for that result. Other graph lifetimes and modified plugin builds
require their own validation; this result is not a guarantee for every teardown.
