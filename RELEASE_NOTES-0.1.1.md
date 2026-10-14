# net-sircl 0.1.1

Status: **research-only**. The plugin, bundled transport-core and fabric-tool
distribution versions are 0.1.1. The core ABI remains 1. The plugin supplies
NCCL ext-net APIs v7 through v12 and uses **wire version 6**. Every participant
must use wire 6, the same complete topology and the same effective relay budget;
wire 2 peers from 0.1.0 are incompatible. A topology is required even for a pair.

## Relay admission and progress

The per-queue relay allowance is derived from all routes in the topology,
including routes outside the active communicator. Each source/function has a
static weighted share of every queue it traverses. With base budget B at most
327680 bytes, forward credit is bounded by 6B/5 and separate reverse ACK debt
by B/5. Their combined queue envelope is at most 458752 bytes, leaving 65536
bytes below an assumed independent 512 KiB queue. The four-host path has a
tighter maximum of 442368 bytes. The [allocation proof and assumptions](net-sircl/ROUTE-ALLOWANCE.md)
cover reciprocal routes, rounding, conservative fallbacks and healthy progress.
They do not establish physical queue independence or safety with unrelated traffic.

Forward and ACK reservations must fit atomically; only successful RC completions
return credit. Failed outstanding work poisons the pool. All channels and
communicators in one process share the source/function allowance. Oldest-first
credit admission uses a FIFO per relayed function; a request leaves the FIFO
after posting its data and final pair, or its CTS pair. Each bounded completion
queue visit immediately refills pending work. Adaptive splitting uses available
credit up to the 64 KiB payload ceiling. The ordering prevents overtaking; it
does not promise a wall-clock latency bound.

`NET_SIRCL_PROFILE` enables opt-in JSON profiling of posts, completions, credit
waits and thread CPU time. An unset or empty value disables it. The
[profiling guide](net-sircl/PROFILING.md) documents overhead, censored observations
and the distinction between post-to-CQE time and physical wire latency.

For relayed paths, set `NCCL_MAX_NCHANNELS=8` on every rank before NCCL
initializes, leaving channel minima unset. Startup emits an advisory when
configured relayed peers exist and no `NCCL_MAX_NCHANNELS`, `NCCL_MAX_NRINGS`
or `NCCL_MAX_CTAS` environment entry exists. The plugin does not set a cap or
change the environment. The recommendation is supported for the measured path
below; cap 8 on direct pairs and eight-rank jobs remains unmeasured here.

## Measurements: rounds 8–10

Conditions common to these records: 2026-10-14, four DGX Spark GB10 hosts used
as a path within an eight-host cabled cycle, two intermediate relays on the
end-to-end edge, ConnectX-7 RoCE at 200 Gb/s, one rank/GPU per host, NCCL 2.32.3
Ring, nccl-tests v2.21.1, and 8–256 MiB messages. Rounds 9–10 explicitly record
BF16. Bus bandwidth is GB/s; paired values below are out of place / in place.
These are retained hardware records, not tests run during release preparation.

### Round 8: 64, 32 and 16 channels

**Measurement.** Source 9154685d and its documentation-only 3541507e variant,
wire 6, separate forward/ACK allowances and 64 KiB chunks. All-reduce L04 has
n=2 at 64 channels, n=4 each at 32 and 16; graph L05 has one job at 16.

**Result.** All jobs return exact results and zero mandatory drop/error deltas.
At 256 MiB, L04 medians are 12.41/12.41 (64), 12.11/12.02 (32) and
13.66/13.68 (16). At 16–256 MiB, cap 16 reaches 0.97–1.04 of the historical
64-channel reference; its L05 completes in 49.27 s against a 75 s deadline.
The historical reference has 429 drop and sequence deltas and is not a clean
control. Its low 8 MiB result also inflates ratios at that size.

**Conclusion.** Cap 16 improves this relayed all-reduce over the candidate's
64-channel setting. The non-monotonic cap-32 result rules out assuming that
every channel reduction gives a proportional gain.

### Round 9: 8, 16 and 24 channels and a direct pair

**Measurement.** Candidate ee91dcc6 has the same transport as 9154685d plus
a startup advisory. L04 has n=4 at cap 16 and n=2 each at caps 8 and 24.
Cap 16 also has one graph, all-gather and reduce-scatter job. Direct-pair
all-reduce compares cap 16 against uncapped 64 channels, n=2 each.

**Result.** All 16 jobs return exact results with zero mandatory drop/error
deltas. At 256 MiB the path L04 medians are 14.57/14.55 (8), 13.77/13.77 (16)
and 12.32/12.22 (24). Cap 16 reproduces round 8 within 1%; graph L05 takes
49.18 s. On the direct pair, cap 16 is approximately 2.5–6% slower at
32–128 MiB, faster at 8–16 MiB, and mixed at 256 MiB. Cap 8 is not tested
on the pair. A single historical wire-2 reference job at cap 16 is clean,
but its per-peer allowance has no aggregate relay-capacity proof.

**Conclusion.** Cap 8 is the strongest path all-reduce candidate in this
screening. Pair results do not support applying channel caps indiscriminately.
Round 9 alone has no cap-8 graph or other-collective qualification.

### Round 10: cap 8 confirmation and other collectives

**Measurement.** The same ee91dcc6 and historical 4ef05fdc binaries from round 9
are used. Combined round-9/10 L04 counts are n=4 at caps 8 and 16, n=2 at
caps 4 and 24. The two sessions are about 15 minutes apart. Cap 8 has a graph
job and one all-gather/reduce-scatter job each; uncapped controls have one
reported job per collective. Historical reference samples total four at cap
16 and three at cap 8.

**Result.** All 16 executed jobs have exact results, successful rank exits and
zero mandatory relay drops, discards, tc drops, sequence errors, ACK timeouts
and retry-exhaustion deltas. The combined L04 medians are:

| Size | Candidate cap 8 | Candidate cap 16 |
|---|---|---|
| 8 MiB | 16.22 / 15.34 | 14.64 / 14.14 |
| 16 MiB | 16.52 / 16.34 | 14.31 / 14.23 |
| 32 MiB | 16.83 / 16.95 | 15.09 / 15.04 |
| 64 MiB | 16.11 / 16.24 | 14.63 / 14.71 |
| 128 MiB | 15.16 / 15.00 | 13.56 / 13.62 |
| 256 MiB | 14.41 / 14.65 | 13.77 / 13.77 |

Cap 8 leads the candidate cap sweep at five of six sizes; cap 4 wins at
32 MiB. Cap-8 graph L05 completes in 44.80 s, exact, with 14.6–17.1 GB/s.
At 16–256 MiB, cap-8 all-gather is 1.14–1.64 times uncapped and reduce-scatter
1.25–1.50 times uncapped. At 256 MiB those values are 14.38/14.51 versus
11.09/11.15 for all-gather, and 14.80/14.94 versus 11.26/11.32 for
reduce-scatter. These collective comparisons have one sample per cell.

Against the historical reference at the same cap, the candidate reaches
0.93–1.00 at cap 8 and 0.89–0.95 at cap 16. All seven capped reference jobs
are drop-free, but finite clean samples do not supply the missing capacity
bound. Ratios to its dropping 64-channel run are not clean-control speedups.

The first uncapped all-gather stops the coordinator because non-mandatory
`vport_loopback_*` counters decrease on an off-path function. All mandatory
counters remain zero and results are exact. The verdict tool then records
these decreases as nonmonotonic instead of invalidating them; all other
negative deltas remain invalidating. The reported repeat matches the first
within 0.3%.

**Conclusion.** Cap 8 is the supported application recommendation for this
four-host relayed path and these three collectives. It reduces the bounded
candidate's cost relative to the historical reference. These measurements
do not qualify serving workloads, other topologies, direct pairs at cap 8,
eight-rank operation or the final standalone 0.1.1 binary.

Evidence identifiers: `net-sircl-0.1.1-round8/RESULTS.md`,
`net-sircl-0.1.1-round9/RESULTS.md` and `net-sircl-0.1.1-round10/RESULTS.md`
in the retained `ring8-hw-20261013` research record, with each round's
`tables/bandwidth.md`, `tables/bandwidth.json` and job summaries. Raw site
records are not distributed. The candidate exported here is 37daef99,
whose advisory recommends eight channels; rounds 9–10 measure ee91dcc6.

## Final hardware pass — PLACEHOLDER FOR MAINTAINER

**Pending: fill from `net-sircl-0.1.1-final/RESULTS.md`.**

- Conditions and exact source/binary identity: **TO FILL**.
- Measurement procedure, settings, sample counts and counters: **TO FILL**.
- Results, correctness, errors and performance: **TO FILL**.
- Conclusion, limitations and qualification scope: **TO FILL**.

This section contains no final-pass result or implied qualification.

## Other limits and provenance

The independent queue model assumes reciprocal installed forwarding,
independent per-function/per-direction queues of at least 512 KiB, one plugin
process per physical host, consistent policy across overlapping jobs and no
unrelated forwarding producers. It does not coordinate disconnected jobs or
bound unbounded retransmissions. GPU Direct RDMA, DMA-BUF and grouped receives
remain unsupported. The historical direct-pair captured all-gather retry
failure is not established as resolved by these path measurements.

Fabric-tool retains the public 0.1.0 implementation and separate originating
revision 47de221a, with its distribution version set to 0.1.1. Its boot
restoration remains unqualified. [SOURCE.json](SOURCE.json) distinguishes
candidate provenance, shipped bytes and immutable historical measured hashes.
