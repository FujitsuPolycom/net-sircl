# DGX Spark transport and fabric validation

Product status: **research-only**. This record qualifies the specific source
artifacts identified below. Periodic restoration and safe emulation protocol
selection have a separate [validation record](recovery-validation.md).
Measurements were collected on 2026-10-23. The structured
[measurement artifact](evidence/dgx-spark-transport.json) contains per-sweep
results, timings, platform versions, GPU checks and negative controls.

## Artifact identity and conditions

The transport implementation is source revision
`ddc39fa07cd57010769a77094d0a6aad1efb9fbb`. Its aarch64 production plugin has
SHA-256 `52fa08d212cdabba6cdd2be629092a5d95ec8235430a46f0022d42d69d1ed3ef`
on all eight hosts. Its embedded transport source identity is
`1d159d428f60a5b2c43dd55a7a27aef4050bedf82263c4eb0ec5a6762acb1e75`.
The packet-marker binary has SHA-256
`06b0ccf0af10b312a92d966d787b37d6fb4be681e13d6a2beb7389b24dfe39f7`.
Fabric Python sources are identical between that revision and the separately
tested resource-ownership revision
`cf2301c086bd38207b595738d263ea5f8c237387`.

Matched controls use baseline source
`bd61460fdb03d52fe2f1e6ce685f4437b98c50b9`, production-library SHA-256
`650844c24ed0727c09eb3f1049080232a179b8cdd887c9a18a23c2ddea2f1bcf`.
The resource-ownership revision's production-library SHA-256 is
`9701b41f6464173d5266de6da0055f56f3dc6f82688c6f00a946807658d1387d`.
Version strings remain plugin 0.1.2, core 0.1.1, core ABI 1 and wire 6.
The emulator's private shared-memory layout is version 2; it must not share a
fabric with a version-1 emulator.

Eight DGX Sparks provide one GB10 GPU per host and directly cabled ConnectX-7
functions. The fleet contains Ubuntu 24.04.4/24.04.5, NVIDIA kernels
6.17.0-1029/7.0.0-1019 and GPU drivers 580.173.02/580.178.04. NIC firmware is
28.45.4028; rdma-core/libibverbs is 50.0-2ubuntu0.2. Builds use GCC 13.3.0;
the benchmark build uses CUDA 13.0.88. GPU clocks were not fixed. The artifact's
idle/active clock samples do not establish clocks during every timed operation.

The selected unpatched NCCL runtime is 2.32.3, library SHA-256
`c77f5ced2c4b538aad2b0ab88a9838e9eac9a68e22017863ffb0cff9c4f58745`,
from the aarch64 `nvidia-nccl-cu13` wheel, SHA-256
`a5bee92b2f4af218c109f8d221c3c9adcc752b94ae0ffcb0ed5abf9341a7724c`.
It is staged separately; installed framework libraries are not replaced.
NVIDIA nccl-tests is v2.21.1, source
`afd59abab774a6a4b492eb344bd0c845a0b7bb4d`, with MPI and one GPU/rank per host.

## Physical collective results

Every sweep selects sircl explicitly, the pinned production plugin, Ring,
at most eight channels and bfloat16. Sizes double from 8 KiB through 256 MiB:
16 size rows, two warm-ups and five timed iterations, in-place and out-of-place
correctness checks. Graph sweeps use 20 replays. All-reduce, all-gather,
reduce-scatter and broadcast each have eager and graph sweeps.

| Cabling and participants | Source comparison | Sweeps | Result |
|---|---|---:|---|
| Directly cabled pair | Baseline and resource-ownership revision | 16 | Passed |
| Eight-host ring, all hosts participating | Baseline and resource-ownership revision | 16 | Passed |
| Directly cabled pair | Transport revision with expanded emulator registries | 8 | Passed |
| Four-host path, including relayed end-to-end connection | Baseline and transport revision with expanded registries | 16 | Passed |
| Eight-host ring, all hosts participating | Transport revision with expanded registries | 8 | Passed |
| Opposite ring endpoints; two paths, three relays per path | Transport revision with expanded registries | 8 | Passed |

All 72 sweeps exit zero, identify sircl and report zero out-of-bounds values.
The 1,152 size rows have no reported correctness failures. Every suite records
before/after observations from all four RDMA functions on all eight hosts:
out-of-sequence, packet sequence, local ACK timeout and implied NAK counters,
plus available ethtool discard/drop/out-of-buffer counters. All monitored
deltas are zero and no required counter is missing. Comparisons span traffic
windows; a reboot's reset counters are not treated as continuous. These are
single matched sweeps, not a statistical performance qualification.

The physical PyTorch check uses cached `nvcr.io/nvidia/pytorch:26.09-py3`,
image ID `sha256:a2d1be1817a6ecf66c56d60e4ab76c1ff745b407c31b4a7a76c1021f8a013d53`,
PyTorch 2.14.0a0+b2c75dd062.nv26.09 and separately loaded NCCL 2.32.3.
Four hosts each report eight exact eager all-reduces through 256 MiB, an exact
64 KiB graph replayed 20 times, PASS and exit zero. Destruction takes
0.35–0.36 seconds before reboot and 0.32–0.33 seconds after group repair.
Graph references are released before destruction.

## Fabric lifecycle and recovery boundary

Real facts, marker installation, apply, explicit verification and a second
idempotent apply pass on the ring and four-host path. Removing a path host's
table preserves its pre-existing clsact queue and an unrelated ARP filter
outside the relay preferences. Reapply and verification pass; the test filter
is removed afterward.

The generated boot service retains execution intent, selected host, custom
marker and relay limit 2. Removing one host's table and restarting the service
restores it. An actual middle-host reboot with independently persisted IPv4
addresses restores and verifies that host's filters, routes, neighbors and
markers. Administration access returns after 123.22 seconds.

Address configuration is a prerequisite. With its normal address-providing
service paused and no replacement, the restarted host has no fabric IPv4
addresses; restoration refuses all changes. Complete facts do not configure
those addresses. The successful local boot check uses a temporary prerequisite
service to restore saved addresses before the unmodified fabric unit runs.

The boot-only service in the recorded source does not recover the entire group.
The reboot also resets neighboring
carriers. Two running neighbors lose six routes and six neighbor entries in
total; group verification fails although the rebooted host verifies. Explicit
apply across all four hosts repairs the group; verification and the repeated
physical PyTorch test then pass. That repair does not qualify autonomous
recovery. See [fabric operation](fabric.md) for the operator sequence.

## GPU emulation and runtime boundary

The expanded registries pass all seven channel-policy cases with the cached
PyTorch image and NCCL 2.32.3: four-rank path and eight-rank cycle use eight
active channels; direct-pair and stock-tuner controls use 64; explicit overrides
use four and 16; tuner disablement uses 64. Every rank reports seven exact
checks and exit zero. The matrix completes in 67.90 seconds.

On host PyTorch 2.11.0+cu130 with the same NCCL library, direct/windowed checks
and four-rank teardown pass. Retaining a CUDA graph causes sircl and Socket
negative controls to exceed the 15-second destruction bound, as intended.
The default channel matrix fails its four-channel case. Protocol probes isolate
an unsupported combination: forced LL128 with four ranks on one GB10 produces
incorrect values with both sircl emulation and Socket. LL and Simple probes
pass, and Simple passes all seven channel cases. These observations do not
establish a physical RDMA defect or a root cause inside NCCL/PyTorch.

NCCL 2.31.2 with the cached image fails the first two-rank emulation collective
for baseline and resource-ownership revisions. These failures do not demonstrate
a regression between those revisions or qualify that shared-GPU combination.

## Software checks and exclusions

The transport revision passes isolated Debian 13 x86-64 source/archive builds,
26 plugin tests, 144 fabric tests, five planning tests, three native core
executables, seven checkout release tests, extracted-source checks, privacy
scans and staged installation. The native lifetime regression exercises 530
remote mappings, 1,536 simultaneous registrations and 300 completion queues;
a reduced cache bound forces eviction. All eight aarch64 builds produce the
same production-library hash.

These measurements do not qualify vLLM/SGLang serving, training jobs, physical
packet-loss recovery, rank-loss recovery or an exhaustive concurrency envelope.
Periodic group restoration is scoped separately in the recovery validation.
Site host names, addresses, keys and raw service logs remain private.
