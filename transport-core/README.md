# transport-core: RDMA transport library for net-sircl

The C11 library provides device and GID discovery, reliable-connected (RC)
queue-pair setup, host-memory registration, posting and completion primitives,
window arithmetic, topology parsing and a shared-memory RDMA emulator.
Version 0.1.0, ABI 1. It contains no CUDA kernels or NCCL collective algorithms.

From this directory, `make` builds the library and `make check` runs CPU tests.
Builds require a C compiler, pthreads, Python 3 and rdma-core headers. Tests use
shared memory and fixture directories, without NIC or remote-host access.

See [the component boundary and window semantics](../docs/transport-core.md).
Retain LICENSE and NOTICE when distributing the library.

`python3 tools/package.py --output FILE` creates a reproducible source archive
from this repository's tracked files, including all three components, docs,
examples, CI and tests. It requires a Git checkout; extracted archives can be
built and tested but cannot create another source archive.

`tools/identity.py` hashes transport-core sources, headers, tools and VERSION.
This source digest is not a checksum of the compiled library.
