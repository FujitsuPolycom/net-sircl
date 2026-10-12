# SIRCL transport core

The C11 core supplies protocol-independent RC setup, registered-host-memory
operations, topology/window arithmetic and the CPU emulation provider. Its ABI
is 1 and its version is 0.1.0. It does not contain NCCL collective algorithms,
CUDA kernels or a model framework.

```sh
make
make check
```

Builds need a C compiler, pthreads, Python3 and rdma-core headers. Tests use
shared memory and fixture directories; they contact no NIC or remote host.
`include/sircl_core/core.h` is the control/planning API;
`include/sircl_core/verbs.h` provides inline provider operations. Callers retain
resource ownership, CUDA readiness, protocol sequencing and completion policy.
Zero-cap windows are unbounded; callers must not represent an unadmitted relay
as a zero-cap direct lane. Legacy empty-window oversize claims are opt-in.

The progress callback API creates no thread. The abort object records a host
request only. Neither is a claim that SIRCL ABI 9 kernels watch a new abort word,
or that its session loops already share one progress thread.

See [DESIGN.md](DESIGN.md) for migration boundaries, byte-identity contracts and
performance gates. Retain LICENSE, NOTICE and [PROVENANCE.json](PROVENANCE.json)
when distributing the core. net-sircl retains its NVIDIA NCCL header notices.


Create a standalone source package from either the SparkRing checkout or the
standalone net-sircl repository:

```sh
python3 tools/package.py --output /tmp/net-sircl-core.tar.gz
```

The archive has sibling `net-sircl`, `transport-core` and `fabric-tool` directories. From the
standalone repository it also includes the root build, documentation, examples,
CI and release tests; use `make all emu check` at that root. From SparkRing,
build in `net-sircl` and `fabric-tool`, and run `make check` in all three. Fabric-tool
tests require pytest. Comparisons
with absent SparkRing/libsircl consumers skip. `--include-libsircl` requires
that optional library source beside the core. No generated binary is included.

`tools/identity.py` computes the core source digest reported by the component
builds. The generated source-view identities and kernel pack hashes remain
separate evidence. The source digest is not a compiled-library checksum.
