# Relay recovery and emulation protocol validation

Product status: **research-only**. Periodic relay restoration and safe protocol
selection are **qualified** for the checks below. Raw LL128 on the CPU-staged
emulation path remains **unsupported**; selecting supported protocols is a
mitigation, not an LL128 implementation.

Measurements collected on 2026-10-23 are in the structured
[recovery evidence](evidence/recovery.json). The transport and runtime context
is also documented in [DGX Spark validation](dgx-spark-validation.md).

## Artifact and execution conditions

The source containing periodic restoration and protocol selection is
`748142fb36850fb097454e4b4c01cf1b3070461e`. Its aarch64 production plugin has
SHA-256 `a2f33a58b2a1f3ab6e8d1b328de4c57b087785236651075ad3e59e6545c9ee14`
on all eight hosts. Four directly cabled DGX Sparks form a path, with one GB10
GPU/rank per host. Complete facts, a custom protected marker path and a relay
limit of two are retained by each host's service.

NCCL is unpatched 2.32.3, library SHA-256
`c77f5ced2c4b538aad2b0ab88a9838e9eac9a68e22017863ffb0cff9c4f58745`.
Physical collectives use nccl-tests v2.21.1, Ring, at most eight channels and
bfloat16. Sizes double from 8 KiB to 256 MiB, with two warm-ups and five timed
iterations. All-reduce, all-gather, reduce-scatter and broadcast each run in
eager mode and with 20 graph replays. Clocks are not fixed; these checks do
not establish a throughput regression threshold.

## Periodic recovery

Each host's retained fabric service restores missing state at startup and
reload. A timer requests reconciliation 15 seconds after the preceding check
finishes. The trigger reloads an active fabric service so healthy markers
survive; it starts an inactive service. Restore policy adds missing objects,
retains ownership checks and does not remove healthy state.

A middle path host is rebooted while every host's reconciliation timer is
enabled. An independent prerequisite service supplies its saved fabric IPv4
addresses while the normal address owner is reserved for the test. The host
returns to administration access after 125.77 seconds. The entire path verifies
after 126.76 seconds, with no operator apply or repair. Marker PIDs on an
unaffected endpoint remain unchanged.

Deleting an owned route and its permanent neighbor entry on a running host
causes group verification to fail. The timer repairs both; verification passes
after 12.88 seconds without operator repair. Eight physical collective sweeps
before reboot and eight after recovery all exit zero, select sircl and report
zero correctness errors. Their 256 size rows have no failures; monitored
RDMA/error/drop counters have zero growth and no required counter is missing.

These results qualify asynchronous relay-object restoration on that path.
Links, independently configured addresses, unchanged cabling and compatible
ownership remain prerequisites. Existing NCCL communicators and application
jobs are not resumed. Marker/kernel/NIC failure combinations beyond the
recorded checks remain unqualified.

Installer coordination is implemented in source
`5c76ef48878e9cef802b1aae1d5b9abb673ba37d`. Replacing or removing boot inputs
stops scheduling and waits for the manager-side restoration job, because
terminating its `systemctl` client alone does not cancel that job. A real
systemd regression with a delayed reload fails without that wait. With the
wait, removal completes after 3.48 seconds, retains its inputs until the reload
finishes and preserves the marker. A bounded timeout retains the inputs.
CPU fabric checks pass 146 tests. Transport ABI and wire behavior are unchanged.

## CPU-staged protocol selection

LL128 carries data and a validity flag in a 128-byte line. NVIDIA's
[protocol explanation](https://github.com/NVIDIA/nccl/issues/281) requires
ordered visibility of that line; splitting or reversing its accesses can
produce incorrect values. Ordinary CPU `memmove` in the emulator does not
provide that contract. This explains why enabling LL128 is unsafe there; the
measurements do not establish an upstream runtime defect.

Forced LL128 fails exact checks with sircl emulation under PyTorch
2.11.0+cu130 and the cached NGC PyTorch 26.09 image's
2.14.0a0+b2c75dd062.nv26.09, including PyTorch Socket controls. Reducing CUDA
cluster size to one also fails. An independent four-rank native NCCL probe
fails through sircl emulation, so PyTorch is not a necessary cause. Its Socket
arm passes once; that observation does not establish a memory-ordering
guarantee or invalidate the failing PyTorch Socket controls.

The emulation launcher selects LL/Simple and rejects LL128-enabling requests
before ranks start unless `--allow-ll128` explicitly permits a diagnostic run.
Seven channel-policy cases pass with every rank exact and independently exited
under both PyTorch versions, with no caller protocol override: 65.26 seconds
for the host framework and 67.83 seconds for the cached image. An unsafe
request exits 2 in 0.37 seconds with an explanation and launches no ranks.
The production RDMA plugin's tuner and protocol cost tables are unchanged.

## Restoration and software verification

After qualification, test timers, services and address prerequisites are
removed. The saved SparkRing configuration and relay owner are restored;
fabric verification passes. The saved four-rank FLARE group is running and
its health endpoint returns HTTP 200. Its recovery timer is active. Model
weights and system NCCL installations are unchanged.

Isolated Debian source/archive checks pass the plugin, core, fabric, protocol
selection, release, privacy and staged-installation suites. Real systemd checks
cover timer execution, missing-state repair, marker lifetime and pending-job
retirement. Site addresses, names, credentials and raw service logs are private.
