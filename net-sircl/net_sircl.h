/* net-sircl: an NCCL network plugin for DGX Spark cabling.
 *
 * The plugin implements NCCL's ext-net interface (ncclNet_v12_t, NVIDIA NCCL 2.32.3) for pairs, rings and
 * paths of DGX Sparks joined by direct QSFP cables, RoCE v2, one ConnectX-7 with two ports per Spark seen as
 * four RDMA functions across two PCI domains. Each function is one NCCL device. A peer beyond a cable is
 * reached by an ordinary RC queue pair addressed to its GID; a relay table on the hosts between carries the
 * frames (SparkRing's fabric installer writes one; it tags frames by destination address).
 *
 * Wire model (one-sided RDMA writes; no receive work requests):
 * - The receiver's irecv RDMA-writes a clear-to-send entry (buffer address, rkey, size, tag) into the
 *   sender's FIFO slot, then a 4-byte sequence word after it on the same queue pair.
 * - The sender's isend matches the entry, RDMA-writes the payload into the receiver's buffer, then a 4-byte
 *   size word and a 4-byte done word into the receiver's completion slot, all on one queue pair, so RC
 *   ordering makes the done word prove the payload landed.
 * - Relayed connections share a route-derived charged-byte allowance per outgoing function,
 *   with separate ACK debt when both resources permit control-pair progress;
 *   otherwise the base route or B/(N-2) aggregate policy remains. Shares round down.
 *   Forward traffic plus reverse ACKs stays below 7/8 of an assumed 512 KiB queue at default B.
 *   Direct peers keep unwindowed sends. See README "Relay window" for the capacity assumptions.
 *
 * Routes. A peer position's route lists the local functions of its lanes (up to two, lane order). The
 * connector keeps NCCL's device when it is one of them (NCCL spreads channels over its devices, which keeps
 * both PCI functions of a cable busy), else takes the lanes in turn; it sends its lane, and the acceptor uses
 * the same lane of its own route, so both ends are the same function class on the same path.
 *
 * Settings (environment, read at init):
 *   NET_SIRCL_TOPOLOGY      required topology file (below): routes for every peer, this host's position
 *                           from its host name
 *   NET_SIRCL_POSITION      this process's position (overrides the topology file's host-name match)
 *   NET_SIRCL_ROUTES        <position>=<function>[/<function>],... lanes per peer (SIRCL_PEER_ROUTES format);
 *                           must match the topology lanes
 *   NET_SIRCL_WINDOWS       <position>=<bytes>,... optional direct-peer payload cap per function;
 *                           relayed entries can only lower every function's source allowance
 *   NET_SIRCL_RELAY_BUDGET  directional charged-byte budget, default 327680, range 65536..327680
 *   NET_SIRCL_IB_TIMEOUT    RC ACK timeout exponent, default 20, decimal 1..31 (direct and relayed)
 *   NET_SIRCL_IB_RETRY_CNT  RC retry count, default 7, decimal 0..7
 *   NET_SIRCL_HCA           comma list of RDMA functions to expose (default: every active RoCE function)
 *   NET_SIRCL_GID_INDEX     GID index (default: the function's RoCE v2 IPv4 GID, found in sysfs)
 *   NET_SIRCL_CHUNK         maximum payload bytes per signaled windowed chunk (default 65536);
 *                           shrinks to available credit and half the allowance when two 4 KiB pieces fit
 *   NET_SIRCL_OOB_ADDR      IPv4 address for the connection exchange (default: NCCL_SOCKET_IFNAME's first
 *                           IPv4 address, else 127.0.0.1)
 *   NET_SIRCL_CONNECT_TIMEOUT_MS  connect/accept deadline (default 60000)
 *   NET_SIRCL_RELAXED_ORDERING    1 (default) registers memory with IBV_ACCESS_RELAXED_ORDERING
 *   NET_SIRCL_ROUTE_CHECK   1 (default) refuses a peer this function's interface has no route to
 *   NET_SIRCL_SPEED_MBPS    the speed each device reports to NCCL's tuner (default 100000)
 *   NET_SIRCL_TC            RoCE traffic class (default 0)
 *
 * Topology file (text; `#` starts a comment):
 *   hosts <name0> <name1> ...      host names in position order (positions 0, 1, ...)
 *   cabling ring|path|explicit     ring/path: host i's port 0 is cabled to host i+1's port 1 (path: no
 *                                  cable from the last host back to the first)
 *   cable <i>.port<p> <j>.port<q>  one cable (repeatable; with `cabling explicit`, or added to ring/path)
 *   port0 <function> <function>    the functions of port 0, lane order (default rocep1s0f0 roceP2p1s0f0)
 *   port1 <function> <function>    the functions of port 1 (default rocep1s0f1 roceP2p1s0f1)
 *   relay_budget <bytes>           directional budget ceiling (default 393216), limited by the environment
 *
 * NVIDIA NCCL's plugin headers (nccl/, Apache-2.0) are vendored unchanged from tag v2.32.3-1,
 * plugins/net/example/nccl/; see NOTICE. */
#ifndef NET_SIRCL_H_
#define NET_SIRCL_H_

#include <stddef.h>
#include <stdint.h>

#define NET_SIRCL_MAGIC 0x53495243u /* "SIRC" */
#define NET_SIRCL_VERSION 6         /* independent forward and reverse-ACK admission */
#define NET_SIRCL_MAX_DEVS 16
#define NET_SIRCL_MAX_LANES 2
#define NET_SIRCL_SLOTS 32          /* NCCL_NET_MAX_REQUESTS */

/* The listener's handle (at most NCCL_NET_HANDLE_MAXSIZE bytes). NCCL passes the same handle buffer to
 * every connect() call of one connection, so the connector keeps its in-progress state's address in
 * `pending` (zero when the listener writes the handle). */
struct net_sircl_handle {
  uint32_t magic;          /* NET_SIRCL_MAGIC */
  uint16_t version;        /* NET_SIRCL_VERSION */
  uint16_t port;           /* the listener's TCP port, network order */
  uint32_t addr;           /* the listener's IPv4 address, network order */
  int32_t position;        /* the listener's position, -1 when unknown */
  uint64_t pending;        /* connector state (pointer), zero in a fresh handle */
};

/* Exchanged over the connection socket, both directions: what the other side needs to bring its queue pair
 * to RTR and to write into this side's control block. */
struct net_sircl_meta {
  uint32_t magic;
  uint32_t qpn;
  uint8_t gid[16];
  uint32_t mtu;            /* enum ibv_mtu */
  uint32_t ctrl_rkey;
  uint64_t ctrl_addr;
  int32_t position;
  int32_t lane;            /* the connector's lane; the acceptor answers with the same, -1 without a route */
  uint64_t policy;         /* identical topology and directional relay budget on every host */
};

/* Each comm's control block, registered remote-writable. The sender's peer writes `cts` and `cts_seq`; the
 * receiver's peer writes `done_size` and `done_seq`. The receiver also stages its outgoing entries in its own
 * `cts` before writing them. */
struct net_sircl_cts {
  uint64_t addr;
  uint32_t rkey;
  uint32_t size;
  uint32_t tag;
  uint32_t reserved;
};

struct net_sircl_ctrl {
  struct net_sircl_cts cts[NET_SIRCL_SLOTS];
  uint32_t cts_seq[NET_SIRCL_SLOTS];   /* sequence + 1 of the entry in the slot */
  uint32_t done_size[NET_SIRCL_SLOTS];
  uint32_t done_seq[NET_SIRCL_SLOTS];  /* sequence + 1 of the completed receive in the slot */
};

/* Test hooks (layer tests). */
void net_sircl_stats(void *send_comm, uint64_t *max_inflight, uint64_t *chunks);
int net_sircl_comm_device(void *comm, char *name, size_t len);
int net_sircl_topology_routes(const char *path, int position, char *out, size_t len);

#endif /* NET_SIRCL_H_ */
