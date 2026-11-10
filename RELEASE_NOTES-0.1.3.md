# net-sircl 0.1.3

Status: **research-only; this release is CPU-tested only.** The 0.1.3 plugin,
transport-core and fabric-tool have not run on DGX Sparks as released. Plugin
and fabric-tool versions are 0.1.3; bundled transport-core is 0.1.2, ABI 1,
with a new source identity. The network library exports ext-net v7–v12 and
tuner v4. Wire version remains **6** and the wire structures are unchanged;
every host of a job must run the same build.

0.1.3 contains everything merged after the `v0.1.2` tag, in two steps:

1. **Resource ownership and recovery** (merged 2026-10 as #3): fabric-tool
   keeps resolved interface bindings and resource ownership across planning,
   apply, restoration and removal, and a local timer restores missing relay
   state after carrier resets; the transport uses a shared validated admission
   plan with explicit registration and request lifetimes; CPU-staged emulation
   selects LL/Simple and rejects unsafe LL128 requests. Revisions of this work
   ran on hardware, and they are tagged: `validation/2026-10-23-transport`,
   `validation/2026-10-23-resource-ownership`, `validation/2026-10-23-recovery`
   and `validation/2026-10-23-installer-coordination` (the last is later than
   the revision that ran). Their records are the
   [transport validation](docs/dgx-spark-validation.md), the
   [recovery validation](docs/recovery-validation.md) and the
   [qualification table](docs/qualification.md).
2. **Review fixes** (merged as #4), described below. None of them has run on
   hardware, and they change the plugin's data path (deadlines, connection
   setup), the topology parser and fabric-tool's reads and ownership record.
   The hardware records of step 1 therefore do not cover 0.1.3.

## Behaviour that changes for users

- **A rank that is late is waited for.** `NET_SIRCL_REQUEST_TIMEOUT_MS` now
  bounds only this host's own transport progress (a send from its
  clear-to-send to completion, and a receive's clear-to-send post). Waiting
  for the peer application to post its matching request has no deadline by
  default, as with NCCL's own transports; before, a rank more than 60 seconds
  late to a collective failed the job. `NET_SIRCL_PEER_WAIT_TIMEOUT_MS` adds
  an optional bound. A failed connection now shuts its socket down, so the
  peer's requests on it fail promptly.
  See [deadlines](docs/configuration.md#deadlines).
- **Settings fail closed.** `NET_SIRCL_CHUNK`, `NET_SIRCL_SPEED_MBPS`,
  `NET_SIRCL_TC`, `NET_SIRCL_RELAXED_ORDERING`, `NET_SIRCL_ROUTE_CHECK` and
  `NET_SIRCL_GID_INDEX` are validated like the other settings: an empty,
  malformed or out-of-range value fails initialization instead of selecting
  the default or being truncated. `NET_SIRCL_OOB_ADDR` must be an IPv4 address
  when set; when no address is found the plugin warns that it advertises
  127.0.0.1.
- **Topology files are read strictly, and identically by the plugin and
  fabric-tool.** A `cabling` line is required; extra words, a `port0`/`port1`
  line without exactly two names, over-long names, more than 64 hosts, a host
  without a cable and cables that do not join every host are refused with a
  message, where the plugin used to guess, truncate or continue without
  routes. A two-host `cabling ring` is one cable for both tools (fabric-tool
  used to read two); write a double-cabled pair with `cabling explicit`.
- **Stray connections no longer fail communicator setup.** The listener binds
  the advertised address and drops a connection that never sends valid
  metadata.
- **fabric-tool**
  - Only processes run by root count as markers (`pgrep -U 0 -u 0`), so a
    process that merely carries the marker's name cannot stand in for one.
  - A missing tool or a failed read on a host is a problem that blocks
    `apply`/`remove` and fails `verify`, instead of reading as "nothing
    installed".
  - The ownership record's layout name is derived from the topology's content,
    not its file name, so a workstation and the boot unit agree. Each host
    shows one pending record write the first time.
  - `cabling explicit` in any order or port pairing can be planned.
  - `apply --boot` no longer stops marker processes the plan does not name.
  - `install-boot` starts the reconcile timer even when its immediate apply
    fails; `remove-boot` can be run twice; ingress qdiscs added by a run that
    failed later stay recorded as owned.
  - `remove` refuses a host whose interfaces have other names unless `--facts`
    is given, and a host whose record names a different marker executable
    unless `--marker` names it. `verify --adopt` is rejected (it never did
    anything).

## Fixes without a change in normal use

- Equally short lanes on an even ring cabled with `explicit` now follow the
  cables at the higher position; they were assumed to arrive on port 1, which
  put the two ends of a lane on different paths when a host was turned round.
  Ring and path cabling are unaffected.
- A direct peer capped with `NET_SIRCL_WINDOWS` above about 64 MiB could
  overflow the queue pair's send queue on a large send; payload writes now
  stop short of it and resume as completions arrive.
- A forked child (a data-loader worker) no longer keeps the one-plugin-per-host
  claim alive after its parent finalizes or exits.
- A connect toward a position the topology gives no route to logs why.
- The plugin build uses the bundled `transport-core` before a directory of
  that name next to the checkout.
- The release scanner fails on an empty or missing tree and scans files that
  contain NUL bytes.

## Tests added

Peers that are late rather than lost, stray connections, a window deeper than
the send queue, strict settings, fork handling, what the topology parser
refuses and accepts, lane reciprocity on turned rings, a conformance corpus
that both topology parsers must read alike, and 57 fabric-tool cases.

## Local validation

On x86-64 Linux (Ubuntu 24.04, GCC 13.3): transport-core's three C tests and
seven Python tests, the plugin's 29 CPU tests (one skipped without rdma-core
headers), the 20 release and conformance tests and fabric-tool's 203 tests
pass, and the release scanner reports no findings. CI builds the real-NIC
library and repeats the suites on x86-64 and aarch64.

## Before using 0.1.3 on hardware

Run the [hardware procedure](fabric-tool/HARDWARE-TEST-PLAN.md) and the
nccl-tests and PyTorch checks of the earlier records on the build you deploy,
and record the result as the [qualification table](docs/qualification.md)
requires. The changes most worth exercising are the deadline behaviour with a
deliberately late rank, relayed traffic after fabric-tool's stricter reads,
and one `apply` on hosts that carry a record written by 0.1.2.

## Documentation corrections

- The README said restoring relay tables at boot was not yet qualified. It was
  checked once on hardware with an earlier fabric-tool revision; the README now
  points to that record and its limits.
- The README, the 0.1.0 measurements and the ring-of-four example said a ring
  of four was CPU-tested only. It ran nccl-tests with the 0.1.0 transport, as
  the 0.1.0 notes state, and has not been rerun since.
- The 0.1.0 notes and the 0.1.0 measurements disagree on whether relaxed
  ordering disabled was measured and on which installer configured the
  path-of-four relays. No retained record settles either, and both documents
  now say so.

## Not changed

- `transport-core/src/fabric.c` and the `sircl_protocol_*` declarations in
  `sircl_fabric.h`, which nothing in this repository calls or defines.
