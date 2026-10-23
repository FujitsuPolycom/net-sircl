# Qualification evidence

Product status: **research-only**. A qualified check applies to its source,
compiled artifact, runtime, topology and workload. Component version strings
alone do not identify the tested implementation.

The [DGX Spark validation record](dgx-spark-validation.md) and its
[measurement artifact](evidence/dgx-spark-transport.json) identify source
revision `ddc39fa07cd57010769a77094d0a6aad1efb9fbb`, compiled-library hashes,
matched controls, results and limitations.

| Component | Conditions and measurement | Status and conclusion |
|---|---|---|
| Physical RDMA plugin | Recorded revision; NCCL 2.32.3; direct pair, four-host path, eight-host ring and opposite ring endpoints; four collectives, eager and graphs, 8 KiB–256 MiB | **Qualified** for 32 sweeps of that revision. Across implementation and baseline controls, 72 sweeps contain 1,152 size rows with zero correctness errors and no monitored error/drop-counter growth. No throughput guarantee |
| Physical PyTorch graph lifetime | Four hosts, one GPU/rank per host; NGC PyTorch 26.09 with NCCL 2.32.3; eight eager checks and 20 graph replays; graph references released before destruction | **Qualified** before reboot and after group reconciliation; nine exact-result lines per rank, destruction 0.32–0.36 seconds |
| Standalone fabric tool | Real four-host path and eight-host ring; apply, verify, idempotency, removal and unrelated-filter preservation | **Qualified** for those operations; pre-existing clsact and unrelated filter retained |
| Local boot restoration | One middle path host rebooted; complete facts and independently persisted interface addresses; custom marker and relay limit | **Qualified** for restoration on that host; service applied and verified its local table |
| Group recovery after carrier reset | Rebooted host restores locally; two running neighbors lose six routes and six neighbor entries | **Unsupported** for automatic group recovery. Explicit group apply and verification restore traffic; the boot-only service does not reconcile running neighbors |
| Shared-memory verbs emulator | Eight local ranks and NCCL initialization pools; cached PyTorch 26.09 image, loaded NCCL 2.32.3 | **Qualified** for the seven-case channel matrix. CPU tests cover admission, worker retirement, mapping leases and registry bounds. Physical loss recovery and NIC performance unsupported |
| Shared-GPU LL128 combination | Four ranks on one GB10; PyTorch 2.11.0+cu130; NCCL 2.32.3; forced LL128 | **Unsupported**: exact checks fail with both sircl emulation and NCCL Socket. LL and Simple probes pass; Simple passes the channel matrix |
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
