# Hardware validation of the standalone fabric tool

Status: not run. This procedure qualifies fabric-tool on two rings of four
DGX Sparks. CPU tests do not qualify real forwarding or boot restoration.

Use placeholders `node-0` through `node-3` for one ring and `node-4` through
`node-7` for the other. Substitute actual host names and SSH targets privately.
Each ring cables position i's port 0 to position i+1's port 1, closing at 3â†’0.
For its path test, describe the same ordered hosts with `cabling path`.

## Prepare and record (about 15 minutes)

1. Reserve a maintenance window and stop workloads/recovery on the test hosts.
   Record versions, archive and binary SHA-256 values, cabling, interface facts,
   current relay ownership and counters. Keep the inputs of any installer whose relays you remove, so they can be restored.
2. If another installer owns the relays (for example the SparkRing installer,
   recorded in `/run/sparkring-fabric/relay-state.json`), remove them with that
   installer first and disable
   its boot restore/marker units for this window. Verify that its ownership
   record and table are removed before starting the standalone tool. Do not
   use this tool to remove another installer's table.
3. Stage a source archive containing sibling `net-sircl`, `transport-core` and
   `fabric-tool` directories. Build the plugin and marker for aarch64. Record
   the compiler, DGX OS, firmware and rdma-core versions.

## Install and verify (about 15 minutes per ring)

Follow `docs/fabric.md` with this ring's topology.
Read facts from all four hosts. Preview and install the marker. Preview apply;
confirm it changes nothing. Execute apply, then run explicit `verify` and
require exit zero with all hosts and lane routes checked. Save command outputs.
Also test a relayed IP path bound to its origin interface.

Confirm the marker is root-owned at
`/usr/local/libexec/fabric-tool/fabric-tool-marker` and matches its adjacent
`.sha256` file. Record owner and mode for both files and every parent through
`/`: none may be writable by group or others. Apply the same full parent check
to `/var/lib/fabric-tool/logs`, and confirm marker logs are written there.
Generated start scripts must check these properties and the digest before
launching a marker; security refusals must not start it.

Capture counters before and after each install/verify operation on every fabric
network device and RDMA function:

```sh
ethtool -S NETDEV | grep -Ei 'discard|drop|out_of_buffer'
cat /sys/class/infiniband/FUNCTION/hw_counters/out_of_sequence
cat /sys/class/infiniband/FUNCTION/hw_counters/packet_seq_err
cat /sys/class/infiniband/FUNCTION/hw_counters/local_ack_timeout_err
cat /sys/class/infiniband/FUNCTION/hw_counters/implied_nak_seq_err
```

Require no growth outside the traffic test window. Missing counters must be
recorded rather than silently treated as zero. Abort on unexplained growth.

## Plugin traffic (about 20 minutes per ring)

Use unpatched NCCL 2.32.3, nccl-tests v2.21.1 and one GPU/rank per host.
Select `NCCL_NET=sircl`, the stamped plugin and `NCCL_ALGO=Ring`. Record actual
library selection and version/core identity from INIT logs. Run a stock pair
control, then all-reduce, all-gather and broadcast on the path of four at
8â€“256 MiB, bfloat16, eager and 20 graph replays. Run the repository's PyTorch
example; release captured graphs before process-group teardown.

Require exit zero on every rank, exact results, no out-of-bounds errors and
bounded teardown. Record counters and throughput with the full conditions;
CPU tests predict no throughput value. Compare only matched runs.

## Boot persistence (about 15 minutes on one ring)

Install the boot unit locally on all four hosts with identical complete facts.
Reboot one host; wait for addresses and the unit to become ready. Require
`verify` to pass for every host and repeat a relayed traffic probe. Record the
systemd journal and counters. Keep any other installer's restore disabled.

## Remove and restore (about 10 minutes per ring)

Preview and execute `remove-boot` locally where installed. Preview and execute
`remove` for the table. Confirm that only owned objects disappeared and that
unrelated networking matches its pre-test snapshot. Restore the prior owner's
table and boot services using its own saved inputs and documented commands;
verify them before resuming workloads/recovery.

If any command fails after changes begin, record partial state and restore
under operator control. Each operation needs a timeout and a rollback owner.
Do not treat installer blockers as proof that every prior step was rolled back.
Publish only logs with host names and addresses removed, including failures and checks not run.
