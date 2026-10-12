# Hardware measurements

These are research-only communication measurements, not serving qualification
or performance guarantees. No hardware was accessed to prepare this release.
The local release changes packaging, documentation, tests and the plugin version
stamp; the transport C sources and NVIDIA headers retain the measured source bytes.

## Shared-core source

Source: SparkRing `a4de4dd5280f01613889ced7873aa85ccf8dc2bb` compared with plugin
RC `7b81b968`, both wire version 2. Evidence record:
`shared-core-hw-20261012/RESULTS.md`, 2026-10-12 06:07–07:41 UTC.
The record identifiers here identify the owner's research archive; raw logs
contain private site information and are not included in this public repository.

Conditions: DGX Spark GB10, unpatched NVIDIA NCCL 2.32.3 rebuilt from pinned
source, nccl-tests v2.21.1, bfloat16, one process/GPU per host, host-memory
staging, relaxed ordering enabled, both PCI functions per cable. Application
clocks were set to 2418 MHz; driver versions were 580.173.02 and 580.178.04.
Models were stopped on all eight hosts. A pair used one cable of the physical
cycle of eight; the path used four consecutive positions with its closing peer
connection forwarded through two intermediate hosts. Other physical cables
remained connected. Relay tables were already installed.

The pair used 64 channels and both cable functions. Cycle and path used
`NCCL_ALGO=Ring` and four channels. The 13 nccl-tests lines covered an 8-byte
probe plus all-reduce, all-gather and broadcast, eager and CUDA graphs (`-G 20`),
over small (8 KiB–2 MiB) and large (8–256 MiB) size sweeps.
All 13 arms passed every line: every rank exited zero, and every reported
`#wrong` value and out-of-bounds error count was zero.

The following are 256 MiB **out-of-place bus bandwidth**, GB/s, means of two
runs per variant in RC/candidate/candidate/RC order. Ratios compare candidate
with RC, not with stock NCCL.

| Layout | Collective | RC eager / graph | Shared core eager / graph |
|---|---|---|---|
| Pair | all-reduce | 21.60 / 21.47 | 21.51 / 21.50 |
| Pair | all-gather | 19.00 / 18.92 | 18.72 / 18.87 |
| Pair | broadcast | 23.52 / 23.48 | 23.59 / 23.44 |
| Cycle of eight | all-reduce | 23.95 / 23.94 | 23.95 / 23.93 |
| Cycle of eight | all-gather | 23.18 / 23.12 | 23.18 / 23.12 |
| Cycle of eight | broadcast | 24.28 / 24.32 | 24.26 / 24.31 |
| Path of four | all-reduce | 17.70 / 17.67 | 17.67 / 17.73 |
| Path of four | all-gather | 18.14 / 18.00 | 18.12 / 18.06 |
| Path of four | broadcast, root 0 | 24.32 / 24.31 | 24.30 / 24.30 |

Candidate/RC ratios were 0.986–1.003 for the pair, 0.999–1.000 for the cycle,
and 0.998–1.003 for the path. Stock NCCL's pair eager all-reduce was 20.62 GB/s
in one same-session reference run. No measured relay error counter grew across
the complete readings bracketing the nccl-tests arms.

The pair graph follow-up used six runs per variant over 18 rows (three
collectives, six sizes from 8–256 MiB). No row was lower under the predeclared
separation rule. Initial two-run separations did not reproduce. This is a
run-to-run comparison, not a confidence bound for all workloads.

PyTorch 2.13.0+cu130 with NCCL 2.32.3 preloaded on the path of four passed four
runs per variant: DDP for 20 steps with identical parameters, eight eager
all-reduce sizes, and 20 replays of a 64 KiB captured all-reduce. All graphs were
released before process-group destruction. Shared-core teardown returned in
0.17–0.31 seconds on every rank.

## RC and stock references

`nccl-net-rc-20261011/RESULTS.md` records RC `7b81b968`, NCCL 2.32.3,
nccl-tests v2.21.1, bfloat16 on a pair and a path of four. The hardware session
ran on 2026-10-12 around 03:10 UTC despite the record's date label. GPU clocks
were set to 2418 MHz, driver 580.173.02, with preinstalled relay state, both
functions per cable and relaxed ordering on. The NCCL binary differs from the
later shared-core session's rebuild.

On the pair, RC/stock bus bandwidth was at least 0.9 on 35 of 36 rows from
8–256 MiB. Eager all-reduce at 8 MiB was 10.53 versus 12.89 GB/s (0.82).
The shared-core session's RC row was 10.68 versus stock 10.93 (0.98), while
the candidate reached 11.84. The weak row is not a universal regression claim
or a resolved performance guarantee. The RC path reached 17.74 GB/s all-reduce
at 256 MiB through two relays, eager and with `-G 20`.

`ring8-finale-20261011/RESULTS.md` records stock NCCL 2.32.3 on the same classes
of layouts. On the cycle of eight with cuMem on, every line passed, with eager
256 MiB all-reduce at 23.77 GB/s. A cuMem-off probe failed on nonadjacent tree
connections. On the path of four, the 8-byte probe connected but all-reduce and
all-gather timed out at 75 seconds; data crossing the relayed closing pair ran
at roughly 0.5 MB/s. Root-0 broadcast passed because it did not use that pair.
Thus “stock does not run the path” refers to usable relayed collectives, not
an inability to establish any connection. This stock session is context, not
a same-binary control for the shared-core cycle measurement.

## Unmeasured conditions

No ring-of-four hardware run; no relaxed-ordering-off comparison. No serving
performance or arbitrary multi-sender relay-budget qualification. CPU tests
and shared-memory emulation do not establish physical relay behavior, real
queue-pair loss recovery or NIC performance. Release 0.1.0 has not had a separate
hardware session with its final version string and package-derived core digest.
