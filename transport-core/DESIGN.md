# Shared RDMA transport core

Transport extraction design and compatibility contract. The reference trees are SIRCL/libsircl at `947bce1e` and net-sircl at `7b81b968`. Existing source line references below identify those baselines; later refactoring moves their owners.

## Decision

Create `spark_transport/transport-core`, version 0.1.0, with a C11 API and hardware/emulation providers. Move protocol-independent verbs, device/QP setup, memory registration, route/window arithmetic and the shared-memory verbs provider into this owner. net-sircl retains NCCL ext-net mapping and its CTS protocol. libsircl retains collective planning/launches and SIRCL ABI 9 protocol state machines. The production Python SIRCL path is unchanged.

Preserve the measured fast paths: no new worker threads, kernel launches, device synchronization, locks, per-operation allocation or provider indirection is introduced on posting/completion. Inline posting/polling helpers retain the provider's existing dispatch. Setup operations can call ordinary C functions. Core API versioning is independent of SIRCL wire ABI and NCCL plugin API.

## Inventory and compatibility classification

Paths below are relative to `spark_transport/`.

Within the SIRCL column, shortened paths are relative to `sircl/sparkring_sircl/`; "proxy" means `oneshot/_roce_proxy.c` and "P2P" means `p2p/_p2p_proxy.c`. Shortened libsircl C paths are relative to `libsircl/src/`, and plugin C paths to `nccl/net-sircl/`. Line numbers refer to the baseline revisions above, not generated compilation views.

| Mechanic | SIRCL serving | libsircl | net-sircl | Classification and decision |
|---|---|---|---|---|
| Named devices, context/PD, active port/GID | `sircl/sparkring_sircl/oneshot/_roce_proxy.c:720`, `p2p/_p2p_proxy.c:305` | Byte copies `libsircl/src/transport/sircl_roce_proxy.c:720`, `sircl_p2p_proxy.c:305`; runtime verbs loading `verbs_dl.c:13` | `nccl/net-sircl/net_sircl.c:521`–668 | Context/port/PD mechanics equivalent; auto-discovery versus explicit configured device/GID intentional. Core supplies mechanics, caller retains selection policy. |
| Lanes and peer metadata | `routes.py:426`, native `connect_lane` | `engine.c`, `tools/site_routes.py:62`; SIRCL native records | `net_sircl.c:406`, `867`, `959` | Both distinguish two PCI-function lanes from two cables. Collective rank records versus independent TCP connection metadata differ intentionally; keep both wire formats. |
| QP creation and INIT/RTR/RTS | Native proxy720–818,1006 onward; P2P305–403,591–638 | Same byte copies | `net_sircl.c:827`–958 | RC, SGE counts, inline16, PSN0, retry7/timeout14 largely equivalent. CQ sharing, queue depths, LID and access flags are caller policy. Parameterize rather than silently normalize. |
| Registration | Proxy775, P2P360 | Runtime-loaded verbs plus pinned arenas | `net_sircl.c:1177` | Host registration common. Plugin data MR requests relaxed ordering; SIRCL arenas do not. Preserve this deliberate performance difference and per-PD keys. |
| Layout/routes | Python `routes.py:219`–635 | `libsircl/src/fabric.c:19`–317 and site_routes | `net_sircl.c:291`–500 | Duplicate topology representation/shortest-path arithmetic; supported limits and parsers differ. Shared topology primitives keep both input formats and current tie-breaking. Pure planning has no CUDA dependency. |
| Relay budgets | `routes.py:671`–789, `p2p/budget.py:92`–195 | C fabric queue accounting353–438, per-session/proxy windows | `net_sircl.c:503`,1253,1387 | Queue accounting versus process-local `(function,peer)` shared windows differ intentionally. Neither proves cross-process fabric-wide admission. Core represents both and never claims global admission. |
| Window retirement | Proxy1106–1174 and proof retirement1317; P2P service_out836 | Same native state machines | Atomic window claims1253, CQ retirement1387 | SIRCL can retire bytes using protocol delivery proof; plugin uses signaled completions. Preserve retirement authority. Empty-window oversize admission is a legacy plugin quirk, explicit rather than silently changed here. |
| Posting | Proxy1392,2533; P2P780 | Byte copies | `net_sircl.c:1233` | All use ordered RDMA WRITE/flag traffic. Linked payload/flag WQEs versus separate CTS/size/done posts differ intentionally. Share WR construction/provider access without changing lists or fences. |
| CQ progress | Proxy1132,2758; P2P771,progress_main | Per-session native progress threads | `net_sircl.c:1387`–1442, poll on NCCL callbacks | CQ polling is equivalent; scheduling is deliberately different. Keep each scheduler, expose bounded progress callbacks for a later shared scheduler. |
| Failure/abort | Native failure words, timed GPU waits, thread failure | `api.c`/`engine.c` fail-stop, bootstrap and peer failure | `net_sircl.c:174`,792,1028,1297 | Failure detection and policy belong above shared resource/error primitives. A core abort request cannot claim to interrupt every GPU wait in ABI 9. |
| Teardown | Native destroy paths | `sircl_p2p_proxy.c:1302`, proxy destroy; engine quiescence | `net_sircl.c:810`,1467 onward | Same resource types; order, retained resources on failed deregistration, and error reporting differ. Preserve component behavior; add provider fault tests rather than broad cleanup changes. |
| Receipts | Native counters/source digests and session receipts | `engine.c` JSON counters/pack hashes; `sirclGetInfo` | `net_sircl_stats`, release export1491 onward | Wire/request counters remain component-specific. Add a core identity separately; do not rename existing evidence fields or count synthetic extra posts. |
| Emulation | Python/native CPU simulators | `src/transport/shm_verbs.c`, fake verbs, asynchronous ordered executor | Byte copy in `tests/standin` | Accidental source duplication. Make one core-owned implementation with compatibility includes and unchanged semantics. |
| CPU policy | `cpus.py:116`,172; proxy affinity | Engine capacity-based creation policy | NCCL drives callbacks | Intentional policies; core permits externally driven progress without choosing affinity for applications. |

Accidental duplication includes identical QP-state construction, verbs loader/provider glue and stand-in sources. This task does not turn different completion proofs, registration flags or wire formats into one protocol. The Python/C planner parity tests remain a compatibility gate, not a claim that the Python implementation is already migrated.

## C API boundary

Use `sircl_core_*` names and an independent ABI constant. The public planning/control header contains fixed-width types and no CUDA/NCCL dependency. The low-level verbs header exposes provider types so hot operations can inline; clients supplying a verbs provider compile the same helpers against real or emulated headers.

| Surface | Contract |
|---|---|
| Identity | ABI, semantic version, compiled source identity; independent of NCCL/SIRCL wire versions |
| Device discovery/setup | Explicit named-device open/query plus reusable sysfs GID/netdev/GUID helpers; no environment guessing inside the transport operation |
| QP | Create RC with caller-provided depths/CQ/inline limit; INIT, RTR and RTS use explicit access, address/MTU/retry attributes |
| MR | Register/deregister a caller-owned allocation with explicit flags; no hidden GPU registration, staging or extra copy |
| Route | Physical cable/port/function roles, path derivation, lane pairing and bounded parser outputs; supported limits remain explicit |
| Window | Available-byte calculation, atomic claim/release where requested, high-water snapshot; explicit legacy oversize mode |
| Posting | Construct/post an RDMA WRITE or an existing WR list; preserve ordering, inline and signal choices; no allocation in the helper |
| Completion | Bounded CQ polling with original WC identifiers; protocol owners interpret completion cookies and retire the appropriate credits |
| Progress | A caller-owned array of bounded callbacks; poll without an internal thread. Future shared scheduling can drive many sessions once their protocol loops expose bounded steps. |
| Failure/abort | First-error state and host abort request with explicit lifetime; transport errors are translated by the caller. Device abort support remains unavailable until every GPU wait and its ABI support it. |
| Teardown | Explicit destroy/deregister operations return provider errors; caller must retain memory until it proves DMA quiescence |
| Receipts | Core version/ABI/source identity plus read-only window/progress state; no additional per-packet logging |

READ/staged VRAM, brokered cross-process reservations and NIXL notifications are extension points, not features claimed by this extraction.

The initial callable shape is:

```c
unsigned sircl_core_abi(void);
const char *sircl_core_version(void);
uint64_t sircl_core_window_room(uint64_t cap, uint64_t outstanding);
int sircl_core_window_claim(uint64_t *outstanding, uint64_t *high_water,
                           uint64_t cap, uint64_t bytes, int legacy_oversize);
void sircl_core_window_release(uint64_t *outstanding, uint64_t bytes);
int sircl_core_progress_poll(struct sircl_core_progress_source *sources,
                             size_t count, unsigned budget);
void sircl_core_request_abort(struct sircl_core_abort *state, int reason);
int sircl_core_abort_reason(const struct sircl_core_abort *state);
int sircl_core_topology_parse(const char *path, struct sircl_core_topology *out,
                             char *error, size_t error_bytes);
int sircl_core_topology_route(const struct sircl_core_topology *topology,
                             int local, int peer, const char **functions,
                             int *relays);
int sircl_core_gid_index(const char *sysfs_root, const char *device, unsigned port);

/* Provider-specific, inline where posting/polling latency matters. */
struct ibv_context *sircl_core_open_named_device(const char *name);
int sircl_core_query_port(struct ibv_context *context, unsigned port,
                          struct ibv_port_attr *attributes);
int sircl_core_query_gid(struct ibv_context *context, unsigned port,
                         int gid_index, union ibv_gid *gid);
struct ibv_mr *sircl_core_reg_mr(struct ibv_pd *pd, void *address,
                               size_t bytes, int access_flags);
struct ibv_qp *sircl_core_create_rc_qp(struct ibv_pd *pd, struct ibv_cq *cq,
                                     unsigned send_depth, unsigned inline_bytes);
int sircl_core_qp_init(struct ibv_qp *qp, unsigned port, int access_flags);
int sircl_core_qp_rtr(struct ibv_qp *qp, const struct sircl_core_qp_peer *peer);
int sircl_core_qp_rts(struct ibv_qp *qp, unsigned timeout, unsigned retry);
int sircl_core_post_send(struct ibv_qp *qp, struct ibv_send_wr *list,
                         struct ibv_send_wr **bad);
int sircl_core_poll_cq(struct ibv_cq *cq, int capacity, struct ibv_wc *out);
```

`sircl_core_qp_peer` carries remote GID/QPN/LID, local GID index/port, minimum MTU, traffic class, flow label explicitly. RTS timeout/retry are arguments; PSN0, one read resource, RNR timer12 and retry7 preserve the two baselines. Resource-returning functions leave ownership with their caller. Progress callbacks receive a finite budget and may not block; abort state is host-only. Compatibility `sircl_fabric_*` planning functions retain their public structures and bounds, while collective `sircl_protocol_*` algorithms remain above the core.

## Byte identity and ABI 9

Keep libsircl's two pristine vendored proxy files and SHA-256 guards unchanged and continue checking equality with SIRCL's serving source. Do not edit a frozen source and then replace its expected hash to make a test pass.

libsircl will compile a deterministic core-adapted source view generated from those verified bytes. The adaptation replaces only selected setup/verbs mechanics with core calls; SIRCL protocol loops, memory layout, operation encodings, flags, fences, kernel sources and feature values remain unchanged. The generator requires exact input identities and exact replacement counts and fails closed on upstream drift. Builds and tests distinguish the pristine-source identity from the adapted/core build identity. A generated source view is not described as byte-identical machine code.

The source-view approach avoids changing production SIRCL during this task while permitting genuine shared setup/resource logic. Its maintenance cost is explicit: changes to a native proxy require review of the adaptation and its fixture tests. A later SIRCL migration uses the same helpers directly, with package build inclusion, native digest refresh, Python release tests and hardware qualification. Wire ABI remains 9 if layouts/semantics remain identical; an abort-word or device-command layout change requires a separately negotiated ABI/feature transition and rebuilt kernel packs. No feature bit falsely advertises a shared progress engine or GPU abort support.

## Packaging and attribution

The core is standalone source with VERSION, Apache-2.0 LICENSE, NOTICE, public headers, CPU tests, provider implementation and extraction instructions. net-sircl plus the core can be archived as a standalone source project; build/install records both identities and installs both notices. No SIRCL Python package, libsircl library, CUDA compiler or GPU is required to build the network plugin's real or CPU stand-in variants.

The libsircl image builder exports the committed library and core trees into sibling directories in its isolated build container. It refuses uncommitted tracked changes and rejects native artifacts built against another core tree. Core tree/version metadata and installed notices are covered by the layer receipt's hash. Existing image locks retain their schema and their library-only `source_tree` field; the receipt binds the dependency separately. Pre-core source fixtures remain supported without that dependency.

Retain SparkRing copyright and RoCEnante attribution: Local Inference Lab's b12x/comm/roce, written by Jason (original-el8), PR 295, with dual-rail and graph-collective contributions by Luke Alonso (lukealonso); b12x lives in FlashInfer/experimental/b12x. Retain net-sircl's NVIDIA NCCL header notice and LICENSE.txt. This task reads the vendored NCCL API headers and existing SparkRing source; it does not import a new NVIDIA NCCL implementation. Commit messages record that distinction.

## Stages and gates

1. Import only net-sircl at 7b81b968 onto 947bce1e and record this design. Gate: its existing 16 CPU tests pass unchanged; inspect both baseline identities.
2. Add core/API/providers/planning, fault/window/compatibility tests and standalone build. Gate: CPU tests, real-provider compile, emulation-provider checks and extraction packaging.
3. Move net-sircl onto the core. Gate: all 16 baseline tests, shared-window and dual-function behavior, standalone install/build, and its NCCL end-to-end emulation test.
4. Move libsircl transport onto the core with guarded adaptation. Gate: empty-build make check, CMake build/guard checks, SIRCL source equality, identical kernel sources/packs with the same toolchain, focused emulator checks.
5. Commit the integrated tree before workstation GPU execution. Run the complete library/torch/setup/sweeps/nccl-tests/mixed suite on both RTX 5090s, including all golden digests, all broadcast roots/layouts, graph P2P 4/6/8, fail-stop and teardown. Preserve failures and retries; no golden-file changes to hide regressions.
6. Publish local final report with exact commits/results and a ring-four hardware regression plan. Nothing is pushed or deployed to Sparks.

## Performance and open questions

The acceptance requirement is no regression against the plugin release candidate and Wave B. Source equivalence, CPU tests and GPU emulation establish correctness/mechanism evidence, not physical RDMA speed. Real timing stays a coordinating-session gate: pair one/two-function bandwidth, ring/path performance, small libsircl collective latency and broadcast from every root. Two independent rings of four cannot reproduce a cycle-eight measurement directly; compare baseline/candidate binaries on identical ring-four layouts, retain cycle-eight results as historical references, and do not combine two rings into an invented eight-node path.

Open hardware questions are compiler effects on native posting, registration/provider behavior and contention; no predicted speedup is part of this extraction. Shared process progress and all-waits abort are later changes with independent tests. Multi-process relay reservation is also not solved by code deduplication alone.
