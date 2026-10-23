# Qualification evidence

Status: **research-only**. Hardware results apply to the recorded source revision,
topology and workload. They do not qualify a binary built from different source
bytes, even when its component version strings match.

| Component and source identity | Conditions | Measurement and result | Qualification limit |
|---|---|---|---|
| Plugin 0.1.2, source revision `bd61460fdb03d52fe2f1e6ce685f4437b98c50b9` | Four DGX Sparks in a path; NCCL 2.32.3; PyTorch eager and CUDA graphs; graph references released before destruction | Nine exact checks per rank; destruction 0.54–0.68 seconds; `PASS` and exit 0 on all four ranks | Qualified for that recorded check; not every graph lifetime or serving workload |
| Plugin 0.1.2, same source revision | Eight-Spark ring; automatic collective channel policy | Eight active operation channels; CUDA-graph all-reduce sweep completed in 36.7 seconds | Qualified for the recorded workload and cabling; not a throughput guarantee |
| Framework integrations using plugin 0.1.1 | Versions and topologies listed in the root README framework matrix; one GPU and rank per host | Correct results, unchanged error counters and independent rank exit in single runs | Research-only; integration results do not qualify modified plugin builds |
| Fabric configuration tool | CPU planning, synthetic host observations and generated command checks | Software contracts for ownership, command forms, input validation and restoration policy | Implemented; physical forwarding and actual reboot restoration require `fabric-tool/HARDWARE-TEST-PLAN.md` |
| Shared-memory verbs emulator | Processes of one Linux host; no physical NIC or relay | Exact data movement, bounded admission, worker retirement and mapping-lifetime checks | Implemented test infrastructure; physical loss recovery and NIC performance unsupported |
| Distributed source manifest `SOURCE.json`, field `source_sha256` | Every distributed C/C++ source and header, including the marker and tests | Hash equality and inventory completeness checked by release tests | Implemented provenance check; source equality alone is not hardware qualification |

The plugin measurement records are in
[release 0.1.2](../RELEASE_NOTES-0.1.2.md),
[release 0.1.1](../RELEASE_NOTES-0.1.1.md) and
[historical 0.1.0 measurements](measurements.md). These are explicitly historical
records. Operational requirements belong in [configuration](configuration.md),
[fabric setup](fabric.md) and [testing](testing.md).

For a hardware qualification, record the source commit, complete distributed
source manifest, compiled-library SHA-256, topology, GPU/NIC/driver/firmware/OS
versions, NCCL/framework versions, environment, clocks, workload, independent
rank exits, correctness checks, error-counter deltas and individual timing
measurements. Keep site-specific identifiers in private logs; publish sanitized
conditions and bounded conclusions.
