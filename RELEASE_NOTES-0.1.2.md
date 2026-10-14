# net-sircl 0.1.2

Status: **research-only; hardware qualification pending**. Plugin and fabric-tool
versions are 0.1.2. Bundled transport-core remains 0.1.1, ABI 1, with unchanged
source identity. The network library exports ext-net v7–v12 and tuner v4.
Wire version remains **6**; all participants need the same topology and relay
policy. Existing route allowances, ACK accounting and wire structures are unchanged.

## PyTorch teardown

**Observation.** The retained 0.1.1 hardware T1 script kept its CUDA graph alive
while destroying the process group. On the workstation, the same order leaves
four sircl ranks waiting after exact eager collectives and graph replay. Stock
NCCL Socket behaves the same way. Both controls reach the 15-second deadline.

**Change.** The corrected hardware check resets/releases the graph and synchronizes
before destruction. Four-rank emulation then exits normally with both transports.
The regression enforces a separate teardown bound and checks every rank's result.
NCCL's graph-reference wait precedes network cleanup; no speculative close,
deregister or proxy-thread ordering change was made to the plugin.

**Limit.** This identifies and fixes the recorded test's lifetime error. The
corrected eight-size, 20-replay T1 still needs to run on the four-host hardware path.
It is not evidence that every possible framework teardown failure is resolved.

## Automatic channels

**Change.** NCCL discovers the bundled tuner in the network library. It supplies
an eight-channel limit per collective on multi-node topologies with more than
two hosts. Two-host topologies keep stock NCCL tuning. Algorithms and protocols
remain NCCL's choices. Explicit NCCL channel environment entries disable the
limit; an external tuner or `NCCL_TUNER_PLUGIN=none` takes precedence.

**Scope.** The tuner cannot see individual peer positions or application
`ncclConfig_t`. It uses the complete topology consistently across ranks. INIT's
allocated channel pool can exceed eight; active collective channels are capped.
Point-to-point calls and concurrent operations are not a host-wide eight-channel
budget. See [policy, overrides and NVIDIA source attribution](docs/channels.md).

**Evidence.** GPU emulation covers a path of four, a cycle of eight, a pair,
explicit cap overrides and disabled tuning. These are correctness and scheduling
checks, not hardware bandwidth measurements. The direct-pair exception retains
stock tuning because the earlier cap-16 pair result regressed 32–128 MiB by
2.5–6%. Historical cap-8 hardware results remain in
[0.1.1 release notes](RELEASE_NOTES-0.1.1.md); no new hardware result is claimed.

## Runtime compatibility

Native direct/relayed emulation checks NCCL 2.27.7 (v10), 2.28.9 (v11),
2.31.2 (v12) and 2.32.3 (v12), including eager all-reduce/all-gather/broadcast,
graph replay and destruction. PyTorch checks use 2.28.9, 2.31.2 and 2.32.3.
The v12 loader starts in the 2.30 series; source compatibility and executed
runtime evidence are distinguished in [the matrix](docs/compatibility.md).
Neither SGLang nor full vLLM serving is qualified by these tests.

## Failure handling and fabric verification

`NET_SIRCL_REQUEST_TIMEOUT_MS` defaults to 60000 and bounds pending request
waits while NCCL polls progress. Connect and request timeouts reject invalid or
nonpositive settings. Peer loss and request expiry return persistent
`ncclRemoteError`; shared relay allowances are poisoned instead of reusing
abandoned credits. CPU injection covers missing clear-to-send, missing receive
completion, lost write completions, vanished peers and bounded close, for direct
and relayed connections. These tests do not model physical RC retry timing.

fabric-tool `verify` detects a missing middle-host rule and a rule that remains
present without `in_hw`. Ownership blockers now return nonzero even when the
forwarding objects otherwise match. Synthetic CLI tests cover failure, restoration
and unreachable relays. Verification remains read-only; no automatic host repair
or hardware fault injection is part of this release work.

## Validation boundary

Production and emulated builds, CPU suites, source-manifest checks, source archive
and staged installation are local checks. GPU tests use an RTX 5090 and shared-memory
verbs, with P2P/SHM disabled. No DGX Spark was contacted. Before promotion, run
corrected T1, automatic channel checks on path/cycle/pair, and the thirteen-line
hardware regression with clean drop/error counters and the existing L05 deadline.
