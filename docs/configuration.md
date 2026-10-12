# Configuration

The same topology file belongs on every participating host. `#` starts a comment.

| Key | Meaning |
|---|---|
| `net-sircl-topology` | Optional first line; ignored by the parser |
| `hosts <name> ...` | Host names in cable order; zero-based positions |
| `cabling ring` | Port 0 to the next host's port 1, including the closing cable |
| `cabling path` | Same ordering without a closing cable |
| `cabling explicit` | Only the supplied `cable` lines |
| `cable <i>.port<p> <j>.port<q>` | One cable; repeat as needed. With `cabling ring` or `path`, adds to the generated cables |
| `port0 <fn> <fn>` | Two RDMA functions (primary, then secondary), carrying lanes 0 and 1; default `rocep1s0f0 roceP2p1s0f0` |
| `port1 <fn> <fn>` | Two RDMA functions (primary, then secondary), carrying lanes 0 and 1; default `rocep1s0f1 roceP2p1s0f1` |
| `relay_budget <bytes>` | Maximum bytes in flight toward a relayed peer per local function; default 393216, 75% of the 512 KiB NIC forwarding queue |

The plugin derives shortest paths, lane functions, relay counts and windows.
For equally short directions in an even ring, lane 0 takes one direction and
lane 1 the other. NCCL may use a subset of the file's hosts, but all intermediate
hosts on any selected path must provide the required relay state.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `NET_SIRCL_TOPOLOGY` | unset | Topology file |
| `NET_SIRCL_POSITION` | inferred from host name | Position in the topology |
| `NET_SIRCL_ROUTES` | topology routes | `<peer>=<function>[/<function>],...` per-peer lane overrides |
| `NET_SIRCL_WINDOWS` | topology relay budget | `<peer>=<bytes>,...` per-function/peer cap shared by this process's comms; zero means unbounded |
| `NET_SIRCL_HCA` | all active RoCE functions | Comma-separated function allowlist |
| `NET_SIRCL_GID_INDEX` | first nonzero RoCE v2 IPv4 GID | GID index override |
| `NET_SIRCL_CHUNK` | 32768 | Signaled chunk bytes for windowed transfers; minimum 4096 |
| `NET_SIRCL_OOB_ADDR` | IPv4 selected by `NCCL_SOCKET_IFNAME`, otherwise loopback | Local address advertised for connection exchange; must be peer-reachable |
| `NET_SIRCL_CONNECT_TIMEOUT_MS` | 60000 | Connect/accept deadline |
| `NET_SIRCL_RELAXED_ORDERING` | 1 | Enable relaxed-ordering memory registration |
| `NET_SIRCL_ROUTE_CHECK` | 1 | Refuse peers without a route through the selected interface |
| `NET_SIRCL_SPEED_MBPS` | 100000 | Per-function speed reported to NCCL |
| `NET_SIRCL_TC` | 0 | RoCE traffic class |

`NET_SIRCL_ROUTES` and `NET_SIRCL_WINDOWS` override individual topology peers.
There is no need to set them in the pair quickstart. Without topology or explicit
routes, NCCL's chosen device is used and the plugin cannot infer relay windows.

Set `NCCL_NET_PLUGIN` to the installed shared library and `NCCL_NET=sircl` to
require it. Use `NCCL_ALGO=Ring` on rings and paths. For diagnostics, use
`NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET`.

Use one interface name or name prefix in `NCCL_SOCKET_IFNAME`. Its first IPv4
address becomes the plugin's OOB address. NCCL's list/exclusion/exact-match
syntax is not parsed here; configure `NET_SIRCL_OOB_ADDR` explicitly if using it.
Loopback is useful for single-host tests and is unsuitable between Sparks.

`NET_SIRCL_STANDIN_DEVICES` and `SIRCL_EMU_FABRIC` are test-provider controls,
not production configuration. `net-sircl/tests/emulation/nccl_e2e.py` sets them itself.

## PyTorch and CUDA graphs

DDP and `torch.distributed` use the plugin through NCCL without a framework patch.
Check the NCCL INIT log for the library actually loaded; PyTorch's compiled-against
version may differ from a preloaded NCCL library.

Delete **all captured graphs referencing the communicator** before teardown:

```python
del graph  # also release any other references to captured graphs
torch.cuda.synchronize()
torch.distributed.destroy_process_group()
```

NCCL waits for graph references during communicator destruction. Keeping them
alive can hang teardown with either this plugin or NCCL's socket transport.
The [emulated test](../net-sircl/tests/emulation/nccl_e2e.py) demonstrates the order.
