# net-sircl 0.1.2

Status: **research-only; hardware-qualified** on DGX Sparks for the conditions in
[Hardware qualification](#hardware-qualification). Plugin and fabric-tool
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
corrected eight-size, 20-replay check passed on the four-host hardware path
(see below). It is not evidence that every possible framework teardown failure
is resolved.

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
explicit cap overrides and disabled tuning. Hardware results follow below. The
direct-pair exception retains stock tuning because the earlier cap-16 pair
result regressed 32–128 MiB by 2.5–6%; on hardware an explicit cap of 8 was
faster than the pair's stock policy at 8–16 MiB and 128–256 MiB. Historical
cap-8 hardware results remain in [0.1.1 release notes](RELEASE_NOTES-0.1.1.md).

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

## Hardware qualification

**Conditions.** 2026-10-15, eight DGX Sparks (GB10) cabled as one ring,
ConnectX-7 RoCE at 200 Gb/s, one GPU and rank per host. The library under test
is the aarch64 release build of this source (`libnccl-net-sircl.so`, SHA-256
`26fb65bc7e22267cee457a85c3293d5429ca7ee3c031c17d41d8d54bbe8a96bd`). Unpatched
NCCL 2.32.3 (loaded version verified on every rank), nccl-tests v2.21.1 in BF16,
PyTorch 2.13.0+cu130, `NCCL_ALGO=Ring`, `NCCL_DEBUG_SUBSYS=INIT,NET,TUNING`.
Automatic arms had no channel, ring, CTA or tuner entry in their environment.
Layouts: a path of four (positions 0–3; the closing Ring edge relayed through
two hosts using the installed relay table, which passed read-only verification),
the ring of eight, and a direct pair with a two-host topology. Every job
snapshotted drop, discard, out-of-buffer, relay-rule, sequence, ACK-timeout
and retry counters on all eight hosts before and after, with a live
out-of-buffer watchdog.

**Results.** All 47 jobs passed: exact results in place and out of place, every
expected size, every rank exiting 0, zero mandatory drop/error deltas, no
watchdog stop and no kernel hardware error.

- Path of four, automatic policy: thirteen-line regression (all-reduce,
  all-gather and broadcast at 8 KiB–2 MiB and 8–256 MiB, eager and `-G 20`)
  plus reduce-scatter at both ranges. The CUDA-graph all-reduce sweep
  (8–256 MiB, 20 captured iterations) completed in 47.9 s against its 75 s
  limit. Every rank logged `collective channel limit 8`; INIT allocated
  64 channels and no rank-0 operation used more than eight.
- Corrected PyTorch teardown on the path: nine exact checks per rank, teardown
  0.54–0.68 s, `PASS` and exit 0 on all four ranks.
- Explicit caps on the path: with `NCCL_MAX_NCHANNELS` set to 4, 8 or 16 the
  tuner logged limit 0 and operations used exactly that many channels; with 16
  plus `NET_SIRCL_CHANNELS=4`, NCCL's entry won (16 active).
- Ring of eight, automatic: limit 8 on every rank, eight active channels, no
  relayed traffic; the CUDA-graph all-reduce sweep completed in 36.7 s.
- Pair with a two-host topology: limit 0 on every rank and NCCL's 64 active
  channels, as with the tuner disabled.

**Bus bandwidth, GB/s, out of place / in place.** Ratios compare jobs in the same
window; single jobs unless n is given.

| Layout and collective, 8–256 MiB | Automatic | Explicit cap 8 | Ratio range |
|---|---|---|---|
| Path all-reduce, 256 MiB | 14.05 / 14.12 | 14.22 / 14.38 | 0.85–1.10 across sizes |
| Path all-gather, 256 MiB | 13.92 / 14.34 | 14.21 / 14.35 | 0.95–1.05 |
| Path reduce-scatter, 256 MiB | 14.74 / 14.98 | 14.63 / 15.08 | 0.97–1.03 |
| Ring all-reduce, 256 MiB (n=2 automatic) | 23.00 / 23.04 | 23.08 / 23.14 | 0.97–1.02 |
| Ring all-gather, 256 MiB | 22.20 / 22.42 | 22.06 / 22.28 | 0.99–1.05 |
| Ring reduce-scatter, 256 MiB | 22.89 / 22.81 | 22.88 / 22.80 | 0.97–1.01 |
| Pair all-reduce, 256 MiB (n=2 automatic) | 21.71 / 21.71 | 22.71 / 22.71 | 0.49–1.03 |

Against the 0.1.1 cap-8 records, automatic all-reduce is 0.94–1.03 on the path
(except 0.85 at 32 MiB out of place) and 0.96–1.01 on the ring. Stock NCCL in
the same window (ring: Ring, four channels, `NCCL_NET=IB`) reached 23.76/23.80
at 256 MiB, so automatic ring all-reduce is 0.96–1.06 of stock at 16–256 MiB.
The pair's stock-policy result is 0.86–0.95 of stock NCCL at 8–16 MiB and
1.05–1.12 at 64–256 MiB.

**Costs of the automatic policy.** On the path, all-reduce at 8 KiB–2 MiB reached
0.59–0.69 (8 KiB) to 0.90–0.97 (2 MiB) of the explicit cap (two or three jobs per arm,
with and without TUNING logging); each job started about 1.5 s later because
INIT allocates 64 channels. The cause of the small-message gap is not isolated.
On the pair, NCCL's 64-channel policy produced 2–5 ms of PFC pause per job
against none at cap 8. Set `NCCL_MAX_NCHANNELS=8` where these matter.

**Not covered.** No physical fault injection or request-timeout expiry ran on
hardware; failure handling remains CPU-tested. Framework workloads other than
the PyTorch check ran with the 0.1.1 library (README, Verified with). Six-rank
jobs, ring-of-four plugin traffic and SGLang or vLLM qualification are not
part of this result.

## Local validation

Production and emulated builds, CPU suites, source-manifest checks, source archive
and staged installation are local checks. GPU emulation uses an RTX 5090 and
shared-memory verbs, with P2P/SHM disabled. The aarch64 release build ran
`make check` on a DGX Spark: transport-core 4 tests, plugin 25 tests, fabric-tool
127 tests and release 4 tests passed.
