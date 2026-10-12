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
the cap when nothing is in flight. net-sircl uses this to let a chunk larger
than the window make progress.

Version 0.1.0 exposes ABI 1. An incompatible public API or data-layout change
requires a new ABI number. This is separate from the plugin's wire version 2,
which must agree between hosts.

## Retained compatibility code

All C sources and headers are frozen for 0.1.0 to preserve the measured bytes
listed in SOURCE.json. Some comments refer to the originating project.
The following code is research-only and is not part of the plugin's transport:

- `transport-core/src/fabric.c` and `include/sircl_fabric.h`: collective planner
  from libsircl, the originating project's C collective library. The core build compiles it; the source-retention test requires both files; `fabric.c` includes the header.
- `transport-core/src/verbs_dl.c` and `include/sircl_core/verbs_dl.c`: optional
  dynamic verbs loader. The latter includes the former, and the core build
  compiles the loader. The plugin links libibverbs directly.
- `transport-core/include/sircl_core/legacy_proxy_names.h`: includes proxy
  renames alongside emulator renames. Frozen emulator sources and tests include
  it, so the unused renames cannot be separated for this release.

The Python proxy adapter and its external-consumer tests are not distributed.
