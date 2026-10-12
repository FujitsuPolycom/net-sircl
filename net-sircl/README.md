# net-sircl plugin component

Build with `make`, `make emu` or `make check` here, or use the repository root
Makefile to include the transport-core checks. The shared core must be beside
this directory as `../transport-core`; `CORE=/path/to/transport-core` overrides
Makefile discovery. The production output is `libnccl-net-sircl.so`.

See the [root README](../README.md) for installation, topology configuration,
measured results, limitations and PyTorch usage. The [test guide](../docs/testing.md)
describes CPU checks and single-GPU emulation. Retain LICENSE, NOTICE and
nccl/LICENSE.txt, together with the core license and provenance, when distributing.
