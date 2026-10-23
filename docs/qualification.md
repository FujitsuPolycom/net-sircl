# Qualification evidence

Product status: **research-only**. A qualified check applies to its source,
compiled artifact, runtime, topology and workload. Component version strings
alone do not identify the tested implementation.

The [recovery and protocol validation](recovery-validation.md) records periodic
restoration and safe emulation selection. The
[DGX Spark transport validation record](dgx-spark-validation.md) and its
[measurement artifact](evidence/dgx-spark-transport.json) identify source
revision `ddc39fa07cd57010769a77094d0a6aad1efb9fbb`, compiled-library hashes,
matched controls, results and limitations.

| Component | Conditions and measurement | Status and conclusion |
|---|---|---|
| Physical RDMA plugin | Artifacts identified by the two validation records; NCCL 2.32.3; four collectives, eager and graphs, 8 KiB–256 MiB | **Qualified** for the recorded topologies and artifacts. Transport controls total 72 sweeps; recovery checks add 16 four-host-path sweeps. Zero reported correctness errors or monitored error/drop growth. No throughput guarantee |
| Physical PyTorch graph lifetime | Four hosts, one GPU/rank per host; NGC PyTorch 26.09 with NCCL 2.32.3; eight eager checks and 20 graph replays; graph references released before destruction | **Qualified** before reboot and after group reconciliation; nine exact-result lines per rank, destruction 0.32–0.36 seconds |
| Standalone fabric tool | Real four-host path and eight-host ring; apply, verify, idempotency, removal and unrelated-filter preservation | **Qualified** for those operations; pre-existing clsact and unrelated filter retained |
| Local boot restoration | One middle path host rebooted; complete facts and independently persisted interface addresses; custom marker and relay limit | **Qualified** for restoration on that host; service applied and verified its local table |
| Periodic group recovery after carrier reset | Four-host path; local 15-second reconciliation timers; independently persisted addresses; middle-host reboot | **Qualified** for the recorded check: group verifies after 126.76 seconds without operator repair; unaffected marker PIDs retained. An injected route/neighbor deletion converges in 12.88 seconds. Interrupted application jobs are not resumed |
| Shared-memory verbs emulator | Eight local ranks and NCCL initialization pools; cached PyTorch 26.09 image, loaded NCCL 2.32.3 | **Qualified** for the seven-case channel matrix. CPU tests cover admission, worker retirement, mapping leases and registry bounds. Physical loss recovery and NIC performance unsupported |
| Emulation protocol selection | CPU-staged transfers; NCCL 2.32.3; PyTorch 2.11.0+cu130 and NGC PyTorch 26.09 | **Qualified** for seven channel cases in each runtime using LL/Simple. LL128-enabling requests require diagnostic opt-in; otherwise rejected before ranks start |
| Raw LL128 through CPU-staged emulation | Independent native NCCL probe and two PyTorch versions on GB10 | **Unsupported**. CPU copies lack the protocol's required visibility guarantee. Safe selection is a mitigation; physical RDMA protocol costs are unchanged |
| Distributed source manifest `SOURCE.json`, field `source_sha256` | Every distributed C/C++ source and header, including marker and tests; inventory completeness and hash equality | **Implemented** provenance check; equality alone does not establish hardware qualification |

Historical plugin and framework measurements are retained in
[release 0.1.2](../RELEASE_NOTES-0.1.2.md),
[release 0.1.1](../RELEASE_NOTES-0.1.1.md) and
[0.1.0 measurements](measurements.md). They do not qualify different source
bytes. Operational requirements belong in [configuration](configuration.md),
[fabric setup](fabric.md) and [testing](testing.md).

A qualification record must identify source commit and manifest, compiled
library SHA-256, topology, GPU/NIC/driver/firmware/OS, runtime and framework,
environment, clock policy, workload, independent rank exits, correctness,
counter deltas and individual timing measurements. Keep site identifiers in
private logs; publish sanitized conditions and bounded conclusions.
