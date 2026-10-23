# NCCL compatibility

The library exports ext-net **v7 through v12**. A runtime chooses the newest
interface it understands; exporting v12 does not require every runtime to use
v12. The wire protocol remains version 6 across those interfaces.

| NCCL runtime | Selected network ABI | Evidence for 0.1.2 |
|---|---|---|
| 2.27.7 | v10 | Native GPU emulation, direct and relayed routes |
| 2.28.9 | v11 | Native and PyTorch GPU emulation, direct and relayed routes |
| 2.30.3–2.30.7 | v12 | Loader/source compatibility only; not executed for 0.1.2 |
| 2.31.2 | v12 | Native and PyTorch GPU emulation, direct and relayed routes |
| 2.32.3 | v12 | Native/PyTorch GPU emulation; automatic channel policy and four-rank teardown |

The v12 loader series begins at NCCL 2.30. The
[2.30.7 loader](https://github.com/NVIDIA/nccl/blob/v2.30.7-1/src/plugin/net.cc)
and [2.31.2 loader](https://github.com/NVIDIA/nccl/blob/v2.31.2-1/src/plugin/net.cc)
try v12 before older versions. The
[2.28.9 loader](https://github.com/NVIDIA/nccl/blob/v2.28.9-1/src/plugin/net.cc)
uses v11, and [2.27.7](https://github.com/NVIDIA/nccl/blob/v2.27.7-1/src/plugin/net.cc)
uses v10. These are pinned NVIDIA sources, not a promise about future releases.
Earlier v7–v9 exports have CPU ABI/layout tests; this release does not claim
hardware or GPU qualification of every older NCCL version that can load them.

GPU emulation uses two processes on one RTX 5090, NCCL's distinct-host override,
P2P/SHM disabled and shared-memory verbs. It verifies every element of eager
all-reduce/all-gather/broadcast at six sizes through 64 MiB, graph replay and
communicator destruction. It does not test ConnectX-7, firmware, actual relays,
multihost performance, or a vLLM/SGLang server. NCCL 2.32.3 is the version
used for the 0.1.2 DGX Spark hardware qualification; framework runs listed in
the README's [Verified with](../README.md#verified-with) loaded 2.30.7, 2.31.2
or 2.32.3 with the 0.1.1 library.

## PyTorch and image versions

PyTorch wheels and container images carry their own NCCL. A framework version,
image name or exported plugin symbol does not establish the loaded runtime.
The runtime matrix above describes the recorded plugin 0.1.2 checks; it does
not qualify every binary carrying that version. The
[DGX Spark validation record](dgx-spark-validation.md) identifies the tested
source, compiled libraries, PyTorch versions and loaded NCCL library. Complete
framework workloads and community vLLM images remain unqualified.

Status: **implemented** for safe protocol selection in CPU-staged emulation:
the launcher defaults to LL and Simple and rejects an LL128-enabling request
unless `--allow-ll128` explicitly permits a diagnostic run. The production
RDMA plugin's protocol costs are unchanged.

Raw LL128 on the CPU-staged path is **unsupported**. Four ranks sharing one
GB10 with NCCL 2.32.3 fail forced-LL128 exact checks under PyTorch 2.11.0+cu130
and NGC PyTorch 26.09's 2.14.0a0, including Socket controls. An independent
native NCCL probe also fails through sircl emulation. This rules out PyTorch
as a necessary cause. NVIDIA's [protocol explanation](https://github.com/NVIDIA/nccl/issues/281)
requires ordered 128-byte data/flag visibility and cautions against forcing
LL128 when the buffer or interconnect lacks it. The emulator's CPU `memmove`
does not provide that guarantee. Safe selection is a mitigation, not an
implementation of LL128 or proof of an upstream runtime defect.

`scripts/check_e2e.py --expected-version 23102` queries `ncclGetVersion` after
importing PyTorch and fails if the loaded version differs. It then verifies the
network choice, exact data and process exits in subprocesses. Prefer testing the
image's existing library; do not replace its NCCL merely because the plugin also
exports a newer ABI. Preloading an older NCCL under a newer PyTorch can fail on
missing symbols before plugin initialization.

## Reproduce the native matrix

Compile `net-sircl/tests/emulation/nccl_compat_e2e.cc` with a CUDA toolkit and
NCCL's public headers/libraries (NCCL 2.27.7 headers suffice), then under a GPU
reservation run:

```sh
python3 scripts/check_compat.py --native /path/to/nccl_compat_e2e \
  --runtime 22707=/path/to/2.27.7/libnccl.so.2 \
  --runtime 22809=/path/to/2.28.9/libnccl.so.2 \
  --runtime 23102=/path/to/2.31.2/libnccl.so.2 \
  --runtime 23203=/path/to/2.32.3/libnccl.so.2
```

Each arm runs in its own process environment and records runtime number,
library SHA-256, selected ABI, exact-result count and teardown in
`test-results/compat`. No system library or framework installation is changed.
