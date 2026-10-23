# RDMA transport component

transport-core provides device and IPv4 RoCE GID discovery, reliable-connected
queue-pair setup, registered host-memory operations, completion primitives,
window arithmetic, topology parsing and the shared-memory verbs emulator used
by CPU and single-GPU tests. The API headers are `sircl_core/core.h` and
`sircl_core/verbs.h` under `transport-core/include/`.

net-sircl owns the NCCL ext-net mapping, clear-to-send (CTS) exchange, registration
policy and progress scheduling. Callers own resources, protocol sequencing,
CUDA readiness and completion policy. The progress callback creates no thread;
the abort object records a host request.

A window cap of zero means unlimited. Never give a relayed peer a zero cap.
With `legacy_oversize=1`, `sircl_core_window_claim` allows one claim larger than
the cap when nothing is in flight. This compatibility option is not the
relayed admission policy. net-sircl
atomically reserves separate forward and ACK credit and splits writes to fit.

Version 0.1.1 exposes ABI 1. An incompatible public API or data-layout change
requires a new ABI number. This is separate from the plugin's wire version 6,
which must agree between hosts.

## Retained compatibility code

`SOURCE.json` records the distributed source inventory separately from upstream
provenance and the historical 0.1.0 measurement hashes. The version macro and its
test use 0.1.1; the ABI remains 1. The component version and ABI do not identify hardware-qualified source bytes.
The following code is research-only and is not part of the plugin's transport:

- `transport-core/src/fabric.c` and `include/sircl_fabric.h`: collective planner
  from libsircl, the originating project's C collective library. The core build compiles it; the source-retention test requires both files; `fabric.c` includes the header.
- `transport-core/src/verbs_dl.c` and `include/sircl_core/verbs_dl.c`: optional
  dynamic verbs loader. The latter includes the former, and the core build
  compiles the loader. The plugin links libibverbs directly.
- `transport-core/include/sircl_core/legacy_proxy_names.h`: includes proxy
  renames alongside emulator renames. The emulator and tests use its verbs symbol mapping; the proxy aliases preserve
  compatibility for consumers of that header.

The Python proxy adapter and its external-consumer tests are not distributed.

## Private planning and emulation contracts

Implemented: `net-sircl/net_sircl_plan.c` constructs a deterministic validated
topology/admission plan. The plugin binds its device discovery to that result;
the CPU allocator oracle links the same planning implementation without the
plugin. The core topology parser rejects duplicate host names and multiply
connected physical ports for every caller.

`net-sircl/net_sircl_admission.h` owns credit reservation, completion accounting,
shared-member progress, pending-request order and membership retirement. Protocol
posting reserves forward and ACK resources together. A failed unaccepted write
returns its reservation; a partial control pair retains debt and poisons progress.
Direct-peer scheduling remains distinct from relay FIFO admission.

`transport-core/src/shm_memory.c` owns allocated segments and cached remote
mappings. Mapping leases prevent eviction while executing work uses a mapping;
inactive remote mappings are evicted when the bounded table needs a slot.
The emulator serializes worker membership transitions under one lifecycle lock.
Executing work holds QP ownership through its memory access and completion;
QP destruction waits for that work before returning. The worker never takes the
lifecycle lock, so joining it cannot wait for a lock held by the joining thread.

The emulator remains research-only test infrastructure. Its retirement and
mapping checks establish local software contracts, not NIC or relay behavior.

Its registry envelope supports eight local ranks with up to 1,024 local
connections per rank: 8,192 shared QPs, 32,768 shared registrations, 1,024 local
CQs and 4,096 local segment mappings. NCCL can allocate a 64-channel connection
pool even when its tuner selects eight channels for operations. The emulator's
registry must accommodate that initialization pool. CPU tests retain a smaller
mapping-cache bound to exercise eviction independently of the production bound.
The shared-memory registry layout is version 2; emulator providers with different
registry layouts must not attach to the same fabric segment. The hardware wire
protocol remains version 6.
