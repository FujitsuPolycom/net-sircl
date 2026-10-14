/* net-sircl: an NCCL network plugin for DGX Spark cabling. See net_sircl.h for the wire model and settings,
 * README.md for status and use.
 *
 * Built two ways: against libibverbs (libnccl-net-sircl.so, the plugin NCCL loads), and with
 * NET_SIRCL_STANDIN against libsircl's shared-memory verbs stand-in (tests/standin/shm_verbs.c), where each
 * process owns emulated devices and real NCCL, or the layer tests, drive the plugin between processes of one
 * host. Under the stand-in only its own shared segments can be registered, so a registration keeps a shadow
 * segment and the data path copies through it; the wire protocol is the same. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <limits.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#ifdef NET_SIRCL_STANDIN
#include "shm_verbs.h"
#define SCCL_RENAME_VERBS
#include "proxy_names.h"
#endif
#include <infiniband/verbs.h>

#include "nccl/net.h"
#include "net_sircl.h"
#include <sircl_core/core.h>
#include <sircl_core/verbs.h>

#ifndef NET_SIRCL_RELEASE
#define NET_SIRCL_RELEASE "unversioned"
#endif
__attribute__((visibility("default"))) const char net_sircl_release[] = NET_SIRCL_RELEASE;
__attribute__((visibility("default"))) const char net_sircl_core_release[] = SIRCL_CORE_VERSION;
#ifndef SIRCL_CORE_BUILD_ID
#define SIRCL_CORE_BUILD_ID "unrecorded"
#endif
__attribute__((visibility("default"))) const char net_sircl_core_source[] = SIRCL_CORE_BUILD_ID;

#define MAX_NET_BYTES (1u << 30) /* NCCL's MAX_NET_SIZE: transfers fit 32-bit lengths */
#define SEND_DEPTH 1024
#define CQ_DEPTH 2048
#define MAX_POSITIONS 64
#define MAX_WINDOWS 128
#define RELAY_QUEUE_BYTES 524288u
/* Reserve 1/8 of each queue. Reverse ACKs cost at most 2/5 of the
 * opposite direction's charged writes (128 / (64 + 256), for 1..64 B).
 * The split policy instead bounds forward charge by 6B/5 and ACK debt by B/5.
 * Either policy obeys 7B/5 <= 7/8 * capacity. */
#define RELAY_BUDGET_DEFAULT (RELAY_QUEUE_BYTES * 5u / 8u)
#include "net_sircl_routes.h"
#define PEER_CHECK_EVERY 4096u   /* test()/isend() calls between peer-loss checks of a waiting comm */

/* wr_id: kind in 56-63, ACK units (128 bytes) in 40-55, charge in 8-39,
 * slot in 0-7. A windowed WR's charge fits 32 bits; direct writes are <=1 GiB. */
enum { WR_DATA = 1, WR_FINAL = 2, WR_CTS = 3 };
#define WR_ID(kind, bytes, slot) (((uint64_t)(kind) << 56) | ((uint64_t)(bytes) << 8) | (uint64_t)(slot))

#define WR_ACK_ID(kind, bytes, slot, ack) (WR_ID(kind, bytes, slot) | ((uint64_t)(ack) / 128u << 40))
#define WR_CHARGE(id) (((id) >> 8) & UINT64_C(0xffffffff))
#define WR_ACK(id) ((((id) >> 40) & UINT64_C(0xffff)) * 128u)

static ncclDebugLogger_t logger;
#define LOG(level, ...) \
  do { if (logger) logger(level, NCCL_NET, __FILE__, __LINE__, __VA_ARGS__); } while (0)
#define WARN(...) LOG(NCCL_LOG_WARN, __VA_ARGS__)
#define INFO(...) LOG(NCCL_LOG_INFO, __VA_ARGS__)

struct dev {
  char name[64];           /* the RDMA function's name (routes and NET_SIRCL_HCA match it) */
  char label[80];          /* "sircl:<name>", NCCL's device name */
  char netdev[64];         /* its Ethernet interface, for the route check ("" when unknown) */
  char *pci_path;          /* the function's sysfs path (NULL under the stand-in) */
  uint64_t guid;
  struct ibv_context *ctx;
  struct ibv_pd *pd;
  uint8_t port;
  int gid_index;
  union ibv_gid gid;
  enum ibv_mtu mtu;
};

/* A peer position's route: the local functions of its lanes (lane order) and the cap on payload in flight
 * toward it through each function (0: none). */
struct route {
  int ndev;
  int dev[NET_SIRCL_MAX_LANES];
  uint32_t window;
  int relays;
  unsigned next;           /* round-robin lane for connections whose NCCL device is not in the route */
};

/* Relayed comms share one source allowance per outgoing function, including control writes.
 * Optional direct-peer windows retain their independent (function, peer) payload accounting. */
struct comm;
struct req;
struct window {
  int dev, position;
  uint32_t cap;
  uint32_t ack_cap;       /* zero: aggregate policy; otherwise independent ACK debt */
  uint64_t ack_inflight;  /* protected by lock, held until successful RC CQE */
  uint64_t inflight;       /* atomic */
  uint64_t max_inflight;   /* atomic, for the tests */
  /* The comms that share the window. Progressing any of them drains every member's completions and posts
   * every member's waiting chunks, so one comm's chunks never wait on completions that sit, unpolled, in
   * another comm's queue (two NCCL comms toward one peer can be driven by different threads). */
  pthread_mutex_t lock;
  struct comm *members;
  struct req *pending_head, *pending_tail; /* FIFO admission under lock */
  int relay;
  ncclResult_t error;
};

static struct {
  pthread_mutex_t lock;
  int refs;
  int ndevs;
  struct dev devs[NET_SIRCL_MAX_DEVS];
  int position;
  uint32_t chunk;
  int speed_mbps;
  int traffic_class;
  int relaxed_ordering;
  int connect_timeout_ms, request_timeout_ms;
  int ib_timeout, ib_retry_cnt;
  int relay_budget, nhosts, owner_fd, route_derived;
  uint64_t policy;
  struct window relay_windows[NET_SIRCL_MAX_DEVS];
  uint32_t oob_addr;       /* network order */
  struct route routes[MAX_POSITIONS];
  int nwindows;
  struct window windows[MAX_WINDOWS];
} g = {.lock = PTHREAD_MUTEX_INITIALIZER};

/* A registration. Under the stand-in the NIC sees `shadow`, a shared segment of the same size. */
struct mr {
  struct ibv_mr *ib;
  uint8_t *base;
  size_t size;
  uint8_t *shadow;
};

struct comm;
struct req {
  int used, is_send, done;
  int64_t started_ms;
  struct comm *c;
  uint32_t seq, slot;
  uint32_t total, posted;
  uint64_t laddr, raddr;
  uint32_t lkey, rkey;
  uint64_t profile_wait_start;
  struct profile_row *profile_wait_row;
  int final_posted;
  int cts_posted, cts_done;
  struct req *pending_next;
  uint8_t *user;           /* receive: where the payload belongs (stand-in copies it there from `shadow`) */
  uint8_t *shadow;
};

struct comm {
  int fd, dev, is_send;
  struct profile_comm *profile;
  struct ibv_cq *cq;
  struct ibv_qp *qp;
  struct net_sircl_ctrl *ctrl;
  struct ibv_mr *ctrl_mr;
  uint64_t rctrl_addr;
  uint32_t rctrl_rkey;
  uint32_t seq;            /* next request's sequence */
  uint32_t head;           /* oldest unfinished request sequence */
  struct window *win;      /* NULL: no cap */
  struct comm *next;
  uint32_t mtu_bytes;
  uint64_t inflight;       /* this comm's share of win->inflight */
  uint64_t chunks;
  int peer_position, lane;
  unsigned waits;          /* calls since the last peer-loss check */
  int64_t cts_wait_ms;
  ncclResult_t error;
  struct req reqs[NET_SIRCL_SLOTS];
};

#include "net_sircl_profile.h"

struct listen_comm {
  int fd, dev;
  struct pending *accepting;
};

/* A connection in progress, on either side. */
struct pending {
  int fd, state, dev;
  struct comm *c;
  struct net_sircl_meta mine, theirs;
  size_t got, ready_got;
  uint8_t ready;
  int64_t deadline_ms;
};
enum { P_CONNECTING = 0, P_WAIT_META, P_WAIT_READY };

static int64_t now_ms(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

/* -- memory the peer may write: the stand-in only registers its own segments -- */
static void *shared_alloc(size_t bytes) {
#ifdef NET_SIRCL_STANDIN
  char err[256];
  void *p = sccl_emu_segment_alloc(bytes ? bytes : 1, err, sizeof err);
  if (!p) WARN("net-sircl: stand-in segment: %s", err);
  return p;
#else
  void *p = NULL;
  if (posix_memalign(&p, 4096, bytes ? bytes : 1)) return NULL;
  memset(p, 0, bytes);
  return p;
#endif
}

static void shared_free(void *p) {
  if (!p) return;
#ifdef NET_SIRCL_STANDIN
  sccl_emu_segment_free(p);
#else
  free(p);
#endif
}

/* -- settings -- */
static long env_long(const char *name, long fallback) {
  const char *v = getenv(name);
  if (!v || !*v) return fallback;
  char *end;
  long x = strtol(v, &end, 10);
  return *end ? fallback : x;
}

/* Reject malformed or overflowing QP fields before opening devices; never truncate to uint8_t. */
static ncclResult_t env_range(const char *name, int fallback, int min, int max, int *out) {
  const char *v = getenv(name);
  *out = fallback;
  if (!v) return ncclSuccess;
  for (const char *p = v; *p; ++p)
    if (*p < '0' || *p > '9') goto invalid;
  errno = 0;
  char *end;
  long x = strtol(v, &end, 10);
  if (!*v || errno == ERANGE || *end || x < min || x > max) goto invalid;
  *out = (int)x;
  return ncclSuccess;
invalid:
  WARN("net-sircl: %s must be a decimal integer in %d..%d (got '%s')", name, min, max, v);
  return ncclInvalidArgument;
}

static uint32_t oob_address(void) {
  const char *a = getenv("NET_SIRCL_OOB_ADDR");
  struct in_addr in;
  if (a && *a && inet_pton(AF_INET, a, &in) == 1) return in.s_addr;
  const char *ifname = getenv("NCCL_SOCKET_IFNAME");
  if (ifname && *ifname && *ifname != '^' && *ifname != '=') {
    struct ifaddrs *list, *i;
    if (getifaddrs(&list) == 0) {
      uint32_t found = 0;
      for (i = list; i && !found; i = i->ifa_next)
        if (i->ifa_addr && i->ifa_addr->sa_family == AF_INET && !strncmp(i->ifa_name, ifname, strlen(ifname)))
          found = ((struct sockaddr_in *)i->ifa_addr)->sin_addr.s_addr;
      freeifaddrs(list);
      if (found) return found;
    }
  }
  return htonl(INADDR_LOOPBACK);
}

static int in_list(const char *list, const char *name) {
  char copy[1024];
  snprintf(copy, sizeof copy, "%s", list);
  for (char *save = NULL, *e = strtok_r(copy, ", ", &save); e; e = strtok_r(NULL, ", ", &save))
    if (!strcmp(e, name)) return 1;
  return 0;
}

static int dev_by_name(const char *name) {
  for (int d = 0; d < g.ndevs; ++d)
    if (!strcmp(g.devs[d].name, name)) return d;
  return -1;
}

/* -- routes: NET_SIRCL_ROUTES, NET_SIRCL_WINDOWS, NET_SIRCL_TOPOLOGY -- */

/* Overrides may reduce a direct window or the function allowances, never invent a route.
 * Topology is the admission contract: unknown peers and changed routes fail closed. */
static ncclResult_t route_overrides(void) {
  const char *names[] = {"NET_SIRCL_ROUTES", "NET_SIRCL_WINDOWS"};
  for (int kind = 0; kind < 2; ++kind) {
    const char *text = getenv(names[kind]);
    if (!text) continue;
    if (!*text || strlen(text) >= 2048 || text[0] == ',' || text[strlen(text)-1] == ',' || strstr(text, ",,"))
      goto invalid;
    char copy[2048];
    strcpy(copy, text);
    uint64_t seen = 0;
    for (char *save = NULL, *e = strtok_r(copy, ",", &save); e; e = strtok_r(NULL, ",", &save)) {
      char *eq = strchr(e, '=');
      if (!eq || eq == e) goto invalid;
      *eq = 0;
      for (char *q = e; *q; ++q) if (*q < '0' || *q > '9') goto invalid;
      errno = 0;
      char *end;
      long peer = strtol(e, &end, 10);
      if (errno || *end || peer < 0 || peer >= g.nhosts || peer == g.position ||
          !g.routes[peer].ndev || (seen & (UINT64_C(1) << peer))) goto invalid;
      seen |= UINT64_C(1) << peer;
      struct route *r = &g.routes[peer];
      if (!kind) {
        char expected[160];
        snprintf(expected, sizeof expected, "%s/%s", g.devs[r->dev[0]].name, g.devs[r->dev[1]].name);
        if (strcmp(expected, eq + 1)) goto invalid;
      } else {
        if (!eq[1]) goto invalid;
        for (char *q = eq + 1; *q; ++q) if (*q < '0' || *q > '9') goto invalid;
        errno = 0;
        unsigned long cap = strtoul(eq + 1, &end, 10);
        if (errno || *end || cap > MAX_NET_BYTES || (cap && cap < 1024)) goto invalid;
        if (r->relays) {
          if (!cap) goto invalid;
          for (int d = 0; d < g.ndevs; ++d)
            if (cap < g.relay_windows[d].cap) g.relay_windows[d].cap = (uint32_t)cap;
        } else r->window = (uint32_t)cap;
      }
    }
    continue;
invalid:
    WARN("net-sircl: invalid %s; routes must match topology; relay windows cannot disable the function cap", names[kind]);
    return ncclInvalidArgument;
  }
  return ncclSuccess;
}

static int my_position_in(const struct sircl_core_topology *t) {
  long p = g.position;
  if (p >= 0) return (int)p;
  char name[256] = {0};
  if (gethostname(name, sizeof name - 1)) return -1;
  char *dot = strchr(name, '.');
  for (int i = 0; i < t->nhosts; ++i) {
    if (!strcmp(t->host[i], name)) return i;
    if (dot && (size_t)(dot - name) == strlen(t->host[i]) && !strncmp(t->host[i], name, (size_t)(dot - name)))
      return i;
  }
  return -1;
}

/* Routes and windows of every reachable peer from a topology file. */
static int routes_from_topology(const char *path) {
  struct sircl_core_topology t;
  char err[512];
  if (sircl_core_topology_parse(path, &t, err, sizeof err)) {
    WARN("net-sircl: NET_SIRCL_TOPOLOGY: %s", err);
    return -1;
  }
  int me = my_position_in(&t);
  if (me < 0 || me >= t.nhosts) {
    WARN("net-sircl: NET_SIRCL_TOPOLOGY %s does not list this host; set NET_SIRCL_POSITION", path);
    return -1;
  }
  if (t.budget < 65536 || t.budget > 393216) {
    WARN("net-sircl: topology relay_budget must be in 65536..393216 bytes");
    return -1;
  }
  /* A port has one cable; this excludes ambiguous forwarding graphs. */
  unsigned occupied[MAX_POSITIONS] = {0};
  for (int i = 0; i < t.ncables; ++i) {
    int a = t.cable[i].a, b = t.cable[i].b, pa = t.cable[i].pa, pb = t.cable[i].pb;
    if ((occupied[a] & (1u << pa)) || (occupied[b] & (1u << pb))) {
      WARN("net-sircl: topology connects a port more than once");
      return -1;
    }
    occupied[a] |= 1u << pa;
    occupied[b] |= 1u << pb;
  }
  for (int a = 0; a < t.nhosts; ++a)
    for (int b = a + 1; b < t.nhosts; ++b)
      if (!strcmp(t.host[a], t.host[b])) return -1;
  g.position = me;
  g.nhosts = t.nhosts;
  if (t.budget < (uint32_t)g.relay_budget) g.relay_budget = (int)t.budget;
  /* Hash initialized parser output, with an effective budget; equal policy is required at connect.
   * This is a compatibility fingerprint, not an authentication primitive. */
  t.budget = (uint32_t)g.relay_budget;
  g.policy = UINT64_C(14695981039346656037);
  for (size_t i = 0; i < sizeof t; ++i)
    g.policy = (g.policy ^ ((const unsigned char *)&t)[i]) * UINT64_C(1099511628211);
  uint32_t caps[MAX_POSITIONS][ROUTE_FUNCTIONS];
  g.route_derived = route_allowances(&t, (uint32_t)g.relay_budget, caps);
  uint32_t ack_caps[MAX_POSITIONS][ROUTE_FUNCTIONS];
  split_allowances(&t, g.route_derived, caps, ack_caps);
  /* Include the admission algorithm in the fingerprint as well as the wire
   * version: equal topology alone does not make unequal policies compatible. */
  g.policy = (g.policy ^ NET_SIRCL_VERSION) * UINT64_C(1099511628211);
  unsigned sources = t.nhosts > 2 ? (unsigned)t.nhosts - 2 : 1;
  for (int d = 0; d < g.ndevs; ++d) {
    int f = route_function(&t, g.devs[d].name);
    g.relay_windows[d].cap = f >= 0 ? caps[me][f] : (uint32_t)g.relay_budget / sources / 64u * 64u;
    g.relay_windows[d].ack_cap = f >= 0 ? ack_caps[me][f] : 0;
    g.relay_windows[d].relay = 1;
  }
  for (int peer = 0; peer < t.nhosts; ++peer) {
    if (peer == me) continue;
    const char *fn[2];
    int relays = 0;
    if (sircl_core_topology_route(&t, me, peer, fn, &relays) < 0) continue;
    struct route *r = &g.routes[peer];
    r->ndev = 0;
    for (int l = 0; l < NET_SIRCL_MAX_LANES; ++l) {
      int d = dev_by_name(fn[l]);
      if (d < 0) {
        WARN("net-sircl: topology lane %s is not exposed; retain both route lanes in NET_SIRCL_HCA", fn[l]);
        return -1;
      }
      r->dev[r->ndev++] = d;
    }
    r->relays = relays;
    uint32_t a = g.relay_windows[r->dev[0]].cap, b = g.relay_windows[r->dev[1]].cap;
    r->window = relays > 0 ? (a < b ? a : b) : 0;
  }
  return 0;
}

/* Test hook: the routes a topology file gives `position`, as "<peer>=<fn>/<fn>:<relays>:<window>;...". */
int net_sircl_topology_routes(const char *path, int position, char *out, size_t len) {
  struct sircl_core_topology t;
  char err[512];
  out[0] = '\0';
  if (sircl_core_topology_parse(path, &t, err, sizeof err)) {
    snprintf(out, len, "error: %s", err);
    return -1;
  }
  if (t.budget < 65536 || t.budget > 393216) {
    snprintf(out, len, "error: relay_budget must be in 65536..393216 bytes");
    return -1;
  }
  uint32_t budget = t.budget < RELAY_BUDGET_DEFAULT ? t.budget : RELAY_BUDGET_DEFAULT;
  uint32_t caps[MAX_POSITIONS][ROUTE_FUNCTIONS];
  int derived = route_allowances(&t, budget, caps);
  uint32_t ack_caps[MAX_POSITIONS][ROUTE_FUNCTIONS];
  split_allowances(&t, derived, caps, ack_caps);
  size_t used = 0;
  for (int peer = 0; peer < t.nhosts; ++peer) {
    const char *fn[2];
    int relays = 0;
    if (peer == position || sircl_core_topology_route(&t, position, peer, fn, &relays) < 0) continue;
    uint32_t a = caps[position][route_function(&t, fn[0])], b = caps[position][route_function(&t, fn[1])];
    int n = snprintf(out + used, len - used, "%d=%s/%s:%d:%u;", peer, fn[0], fn[1], relays,
                     relays > 0 ? (a < b ? a : b) : 0u);
    if (n < 0 || (size_t)n >= len - used) return -1;
    used += (size_t)n;
  }
  return 0;
}

static struct window *window_for(int d, int position, uint32_t cap) {
  if (!cap) return NULL;
  pthread_mutex_lock(&g.lock);
  struct window *w = NULL;
  for (int i = 0; i < g.nwindows && !w; ++i)
    if (g.windows[i].dev == d && g.windows[i].position == position) w = &g.windows[i];
  if (!w && g.nwindows < MAX_WINDOWS) {
    w = &g.windows[g.nwindows++];
    memset(w, 0, sizeof *w);
    w->dev = d, w->position = position, w->cap = cap;
    pthread_mutex_init(&w->lock, NULL);
  }
  pthread_mutex_unlock(&g.lock);
  return w;
}

/* Abstract socket ownership has no stale file after a crash. Production containers must use
 * the host network namespace, as RDMA deployments do; separate namespaces cannot share this guard. */
static int claim_host(void) {
  struct sockaddr_un addr = {.sun_family = AF_UNIX};
#ifdef NET_SIRCL_STANDIN
  const char *fabric = getenv("SIRCL_EMU_FABRIC");
  snprintf(addr.sun_path + 1, sizeof addr.sun_path - 1, "net-sircl:%s:%d", fabric ? fabric : "emu", g.position);
#else
  snprintf(addr.sun_path + 1, sizeof addr.sun_path - 1, "net-sircl-relay-owner");
#endif
  g.owner_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
  if (g.owner_fd < 0) return -1;
  if (bind(g.owner_fd, (struct sockaddr *)&addr, sizeof addr)) {
    close(g.owner_fd);
    g.owner_fd = -1;
    WARN("net-sircl: one plugin process per host is required for relay accounting");
    return -1;
  }
  return 0;
}

/* -- devices -- */


static ncclResult_t open_devices(void) {
#ifdef NET_SIRCL_STANDIN
  char err[256];
  if (sccl_emu_attach(err, sizeof err)) {
    WARN("net-sircl: stand-in attach: %s", err);
    return ncclSystemError;
  }
  /* Emulated functions "emu0".."emu<N-1>" (NET_SIRCL_STANDIN_DEVICES, default 2), unique in the fabric by
   * this process's id. */
  int nemu = (int)env_long("NET_SIRCL_STANDIN_DEVICES", 2);
  if (nemu < 1) nemu = 1;
  if (nemu > 4) nemu = 4;
  for (int k = 0; k < nemu; ++k) {
    uint8_t gid[16] = {0};
    uint32_t pid = (uint32_t)getpid();
    gid[9] = (uint8_t)k;
    gid[10] = gid[11] = 0xff;
    memcpy(gid + 12, &pid, 4);
    char name[64];
    snprintf(name, sizeof name, "emu%d-%u", k, pid);
    if (sccl_emu_add_device(name, gid) < 0) {
      WARN("net-sircl: stand-in device %s", name);
      return ncclSystemError;
    }
  }
#endif
  const char *hca = getenv("NET_SIRCL_HCA");
  int n = 0;
  struct ibv_device **list = ibv_get_device_list(&n);
  if (!list) return ncclSystemError;
  for (int i = 0; i < n && g.ndevs < NET_SIRCL_MAX_DEVS; ++i) {
    const char *ibname = ibv_get_device_name(list[i]);
    char name[64];
    snprintf(name, sizeof name, "%s", ibname);
#ifdef NET_SIRCL_STANDIN
    char *dash = strchr(name, '-');
    if (dash) *dash = '\0'; /* routes name "emu<k>" */
#endif
    if (hca && *hca && !in_list(hca, name)) continue;
    struct ibv_context *ctx = ibv_open_device(list[i]);
    if (!ctx) continue;
    struct ibv_port_attr pa;
    memset(&pa, 0, sizeof pa);
    if (sircl_core_query_port(ctx, 1, &pa) || pa.state != IBV_PORT_ACTIVE
#ifndef NET_SIRCL_STANDIN
        || pa.link_layer != IBV_LINK_LAYER_ETHERNET
#endif
    ) {
      sircl_core_close_device(ctx);
      continue;
    }
    struct dev *d = &g.devs[g.ndevs];
    memset(d, 0, sizeof *d);
    snprintf(d->name, sizeof d->name, "%s", name);
    snprintf(d->label, sizeof d->label, "sircl:%s", name);
    d->ctx = ctx;
    d->port = 1;
    d->mtu = pa.active_mtu;
#ifdef NET_SIRCL_STANDIN
    d->gid_index = (int)env_long("NET_SIRCL_GID_INDEX", 0);
#else
    d->gid_index = (int)env_long("NET_SIRCL_GID_INDEX", -1);
    if (d->gid_index < 0) d->gid_index = sircl_core_gid_index("/sys/class/infiniband", name, d->port);
    if (d->gid_index < 0) {
      WARN("net-sircl: %s has no RoCE v2 IPv4 GID; set NET_SIRCL_GID_INDEX", name);
      sircl_core_close_device(ctx);
      continue;
    }
    char path[256], real[PATH_MAX];
    snprintf(path, sizeof path, "/sys/class/infiniband/%s/device", name);
    if (realpath(path, real)) d->pci_path = strdup(real);
    d->guid = sircl_core_sysfs_guid("/sys/class/infiniband", name);
    sircl_core_sysfs_netdev("/sys/class/infiniband", name, d->netdev, sizeof d->netdev);
#endif
    if (sircl_core_query_gid(ctx, d->port, d->gid_index, &d->gid)) {
      WARN("net-sircl: %s has no GID at index %d", name, d->gid_index);
      sircl_core_close_device(ctx);
      free(d->pci_path);
      continue;
    }
    if (!d->guid) memcpy(&d->guid, d->gid.raw + 8, 8);
    d->pd = sircl_core_alloc_pd(ctx);
    if (!d->pd) {
      sircl_core_close_device(ctx);
      free(d->pci_path);
      continue;
    }
    INFO("net-sircl: device %d %s, GID index %d, netdev %s", g.ndevs, name, d->gid_index,
         d->netdev[0] ? d->netdev : "-");
    g.ndevs++;
  }
  ibv_free_device_list(list);
  return ncclSuccess;
}

/* NCCL owns communicator geometry. Its net ABI cannot set a collective channel cap;
 * changing process environment here would also affect unrelated communicators. */
static ncclResult_t sircl_init(void **ctx, uint64_t commId, ncclNetCommConfig_v12_t *config,
                               ncclDebugLogger_t logFunction, ncclProfilerCallback_t profFunction) {
  (void)commId; (void)config; (void)profFunction;
  logger = logFunction;
  pthread_mutex_lock(&g.lock);
  ncclResult_t r = ncclSuccess;
  if (g.refs++ == 0) {
    profile_init();
    /* Defaults follow NVIDIA NCCL v2.32.3-1, src/transport/net_ib/connect.cc (see NOTICE).
     * Plugin settings are independent of NCCL_IB_*; all peers use this local QP policy. */
    r = env_range("NET_SIRCL_IB_TIMEOUT", 20, 1, 31, &g.ib_timeout);
    if (r == ncclSuccess)
      r = env_range("NET_SIRCL_IB_RETRY_CNT", 7, 0, 7, &g.ib_retry_cnt);
    if (r == ncclSuccess)
      r = env_range("NET_SIRCL_RELAY_BUDGET", RELAY_BUDGET_DEFAULT, 65536, RELAY_BUDGET_DEFAULT, &g.relay_budget);
    if (r == ncclSuccess)
      r = env_range("NET_SIRCL_POSITION", -1, 0, MAX_POSITIONS - 1, &g.position);
    if (r == ncclSuccess)
      r = env_range("NET_SIRCL_CONNECT_TIMEOUT_MS", 60000, 1, INT_MAX, &g.connect_timeout_ms);
    if (r == ncclSuccess)
      r = env_range("NET_SIRCL_REQUEST_TIMEOUT_MS", 60000, 1, INT_MAX, &g.request_timeout_ms);
    if (r != ncclSuccess) goto init_done;
    g.owner_fd = -1;
    g.nhosts = 0;
    memset(g.relay_windows, 0, sizeof g.relay_windows);
    for (int d = 0; d < NET_SIRCL_MAX_DEVS; ++d)
      pthread_mutex_init(&g.relay_windows[d].lock, NULL);
    g.chunk = (uint32_t)env_long("NET_SIRCL_CHUNK", 65536);
    g.speed_mbps = (int)env_long("NET_SIRCL_SPEED_MBPS", 100000);
    g.traffic_class = (int)env_long("NET_SIRCL_TC", 0);
    g.relaxed_ordering = (int)env_long("NET_SIRCL_RELAXED_ORDERING", 1);
    g.oob_addr = oob_address();
    if (g.chunk < 4096) g.chunk = 4096;
    memset(g.routes, 0, sizeof g.routes);
    g.nwindows = 0;
    r = open_devices();
    if (r == ncclSuccess && g.ndevs == 0) {
      WARN("net-sircl: no active RoCE device (NET_SIRCL_HCA=%s)", getenv("NET_SIRCL_HCA") ? getenv("NET_SIRCL_HCA") : "");
      r = ncclSystemError;
    }
    if (r == ncclSuccess) {
      const char *topo = getenv("NET_SIRCL_TOPOLOGY");
      if (!topo || !*topo) {
        WARN("net-sircl: NET_SIRCL_TOPOLOGY is required to classify direct and relayed peers");
        r = ncclInvalidArgument;
      } else if (routes_from_topology(topo)) r = ncclInvalidArgument;
    }
    if (r == ncclSuccess) {
      r = route_overrides();
      if (r == ncclSuccess && claim_host()) r = ncclSystemError;
    }
    if (r == ncclSuccess) {
      INFO("net-sircl: directional relay budget %d, hosts %d, model %s", g.relay_budget,
           g.nhosts, g.route_derived ? "routes" : "aggregate");
      for (int d = 0; d < g.ndevs; ++d)
        INFO("net-sircl: relay allowance %s %u bytes, model %s", g.devs[d].name, g.relay_windows[d].cap,
             g.route_derived ? "routes" : "aggregate");
      for (int d = 0; d < g.ndevs; ++d)
        INFO("net-sircl: ACK allowance %s %u bytes", g.devs[d].name, g.relay_windows[d].ack_cap);
      INFO("net-sircl transport-core %s ABI %u source %s", SIRCL_CORE_VERSION, SIRCL_CORE_ABI, SIRCL_CORE_BUILD_ID);
      INFO("net-sircl %s (wire version %d): %d devices, position %d, chunk %u, relaxed ordering %d, "
           "IB timeout %d, retry count %d", NET_SIRCL_RELEASE, NET_SIRCL_VERSION, g.ndevs, g.position,
           g.chunk, g.relaxed_ordering, g.ib_timeout, g.ib_retry_cnt);
    }
    if (r != ncclSuccess) {
      for (int d = 0; d < g.ndevs; ++d) {
        sircl_core_dealloc_pd(g.devs[d].pd);
        sircl_core_close_device(g.devs[d].ctx);
        free(g.devs[d].pci_path);
      }
      g.ndevs = 0;
      if (g.owner_fd >= 0) close(g.owner_fd);
      for (int d = 0; d < NET_SIRCL_MAX_DEVS; ++d) pthread_mutex_destroy(&g.relay_windows[d].lock);
    }
init_done:
    if (r != ncclSuccess) g.refs = 0;
  }
  pthread_mutex_unlock(&g.lock);
  if (r == ncclSuccess) *ctx = &g;
  return r;
}

static ncclResult_t sircl_finalize(void *ctx) {
  (void)ctx;
  pthread_mutex_lock(&g.lock);
  if (g.refs > 0 && --g.refs == 0) {
    for (int d = 0; d < g.ndevs; ++d) {
      sircl_core_dealloc_pd(g.devs[d].pd);
      sircl_core_close_device(g.devs[d].ctx);
      free(g.devs[d].pci_path);
    }
    if (profile_enabled) { profile_flush(); profile_reset(); }
    g.ndevs = 0;
    for (int i = 0; i < g.nwindows; ++i) pthread_mutex_destroy(&g.windows[i].lock);
    for (int d = 0; d < NET_SIRCL_MAX_DEVS; ++d) pthread_mutex_destroy(&g.relay_windows[d].lock);
    if (g.owner_fd >= 0) close(g.owner_fd);
    g.owner_fd = -1;
    g.nwindows = 0;
  }
  pthread_mutex_unlock(&g.lock);
  return ncclSuccess;
}

static ncclResult_t sircl_devices(int *ndev) {
  *ndev = g.ndevs;
  return ncclSuccess;
}

#include "net_sircl_tuner.h"

static ncclResult_t sircl_get_properties(int d, ncclNetProperties_v12_t *p) {
  if (d < 0 || d >= g.ndevs) return ncclInvalidArgument;
  memset(p, 0, sizeof *p);
  p->name = g.devs[d].label;
  p->pciPath = g.devs[d].pci_path;
  p->guid = g.devs[d].guid;
  /* GB10 offers the NIC no GPU-memory registration: NCCL stages through host buffers it registers. */
  p->ptrSupport = NCCL_PTR_HOST;
  p->regIsGlobal = 0;
  p->forceFlush = 0;
  p->speed = g.speed_mbps;
  p->port = g.devs[d].port;
  p->latency = 0;
  p->maxComms = 65536;
  p->maxRecvs = 1;
  p->netDeviceType = NCCL_NET_DEVICE_HOST;
  p->netDeviceVersion = NCCL_NET_DEVICE_INVALID_VERSION;
  p->vProps.ndevs = 1;
  p->vProps.devs[0] = d;
  p->maxP2pBytes = MAX_NET_BYTES;
  p->maxCollBytes = MAX_NET_BYTES;
  p->maxMultiRequestSize = 1;
  p->railId = -1;
  p->planeId = 0; /* one plane: NCCL may pair any two functions; reachability is the plugin's */
  return ncclSuccess;
}

/* -- sockets: small, ordered control messages -- */
static int set_nonblocking(int fd) {
  int flags = fcntl(fd, F_GETFL, 0);
  return flags < 0 ? -1 : fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static int send_all(int fd, const void *buf, size_t len) {
  const char *p = buf;
  int64_t deadline = now_ms() + 10000;
  while (len) {
    ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
    if (n < 0 && (errno == EAGAIN || errno == EINTR)) {
      if (now_ms() > deadline) return -1;
      struct pollfd pf = {fd, POLLOUT, 0};
      poll(&pf, 1, 100);
      continue;
    }
    if (n <= 0) return -1;
    p += n;
    len -= (size_t)n;
  }
  return 0;
}

/* Progress a receive of `len` bytes; 1 when complete, 0 when more is due, -1 on failure or end of stream. */
static int recv_some(int fd, void *buf, size_t len, size_t *got) {
  while (*got < len) {
    ssize_t n = recv(fd, (char *)buf + *got, len - *got, 0);
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) return -1;
    *got += (size_t)n;
  }
  return 1;
}

/* The peer's end of the comm's socket closed: its process ended or it closed the comm. */
static int peer_gone(struct comm *c) {
  if (c->fd < 0) return 0;
  struct pollfd pf = {c->fd, POLLIN | POLLRDHUP, 0};
  if (poll(&pf, 1, 0) <= 0) return 0;
  if (pf.revents & (POLLHUP | POLLERR | POLLRDHUP)) return 1;
  char b;
  return (pf.revents & POLLIN) && recv(c->fd, &b, 1, MSG_PEEK) == 0;
}

/* -- queue pairs -- */
static void window_join(struct window *w, struct comm *c) {
  pthread_mutex_lock(&w->lock);
  c->next = w->members;
  w->members = c;
  pthread_mutex_unlock(&w->lock);
}

static void comm_destroy(struct comm *c) {
  if (!c) return;
  if (c->win) pthread_mutex_lock(&c->win->lock);
  /* Stop the QP before retiring its local accounting. An abandoned relay write may still be
   * in the fabric: poison the allowance instead of lending its credit to another comm. */
  if (c->qp) sircl_core_destroy_qp(c->qp);
  if (c->win) {
    /* Queued requests belong to the comm; never retain them after free. */
    struct req **pending = &c->win->pending_head;
    c->win->pending_tail = NULL;
    while (*pending) {
      if ((*pending)->c == c) *pending = (*pending)->pending_next;
      else {
        c->win->pending_tail = *pending;
        pending = &(*pending)->pending_next;
      }
    }
    struct comm **link = &c->win->members;
    while (*link && *link != c) link = &(*link)->next;
    if (*link) *link = c->next;
    if (c->inflight) {
      if (c->win->relay) c->win->error = ncclRemoteError;
      else sircl_core_window_release(&c->win->inflight, c->inflight);
    }
    pthread_mutex_unlock(&c->win->lock);
  }
  if (profile_enabled) profile_close(c);
  if (c->cq) sircl_core_destroy_cq(c->cq);
  if (c->ctrl_mr) sircl_core_dereg_mr(c->ctrl_mr);
  shared_free(c->ctrl);
  if (c->fd >= 0) close(c->fd);
  free(c);
}

static struct comm *comm_create(int d, int is_send) {
  struct comm *c = calloc(1, sizeof *c);
  if (!c) return NULL;
  c->dev = d;
  c->is_send = is_send;
  c->fd = -1;
  c->lane = -1;
  struct dev *dv = &g.devs[d];
  c->cq = sircl_core_create_cq(dv->ctx, CQ_DEPTH);
  c->qp = c->cq ? sircl_core_create_rc_qp(dv->pd, c->cq, SEND_DEPTH, 16) : NULL;
  c->ctrl = shared_alloc(sizeof *c->ctrl);
  c->ctrl_mr = c->ctrl ? sircl_core_reg_mr(dv->pd, c->ctrl, sizeof *c->ctrl,
                                    IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE) : NULL;
  if (!c->cq || !c->qp || !c->ctrl || !c->ctrl_mr) {
    WARN("net-sircl: queue pair setup on %s failed", dv->name);
    comm_destroy(c);
    return NULL;
  }
  if (sircl_core_qp_init(c->qp, dv->port, IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE)) {
    WARN("net-sircl: queue pair to INIT on %s failed", dv->name);
    comm_destroy(c);
    return NULL;
  }
  return c;
}

static void meta_fill(struct comm *c, struct net_sircl_meta *m) {
  struct dev *dv = &g.devs[c->dev];
  memset(m, 0, sizeof *m);
  m->magic = NET_SIRCL_MAGIC;
  m->qpn = c->qp->qp_num;
  memcpy(m->gid, dv->gid.raw, 16);
  m->mtu = (uint32_t)dv->mtu;
  m->ctrl_rkey = c->ctrl_mr->rkey;
  m->ctrl_addr = (uint64_t)(uintptr_t)c->ctrl;
  m->position = g.position;
  m->lane = c->lane;
  m->policy = g.policy;
}

/* Fail fast when this function's interface has no route to the peer's address: a peer beyond a cable needs
 * a relay table on the hosts between, and without one RTR would only time out after about a minute. */
static ncclResult_t route_check(struct comm *c, const struct net_sircl_meta *r) {
#ifdef NET_SIRCL_STANDIN
  (void)c; (void)r;
  return ncclSuccess;
#else
  struct dev *dv = &g.devs[c->dev];
  static const uint8_t mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};
  if (!dv->netdev[0] || memcmp(r->gid, mapped, 12) || !env_long("NET_SIRCL_ROUTE_CHECK", 1)) return ncclSuccess;
  int fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (fd < 0) return ncclSuccess;
  ncclResult_t result = ncclSuccess;
  if (setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, dv->netdev, (socklen_t)strlen(dv->netdev)) == 0) {
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons(4791);
    memcpy(&sa.sin_addr.s_addr, r->gid + 12, 4);
    if (connect(fd, (struct sockaddr *)&sa, sizeof sa) && (errno == ENETUNREACH || errno == EHOSTUNREACH)) {
      char ip[INET_ADDRSTRLEN];
      inet_ntop(AF_INET, &sa.sin_addr, ip, sizeof ip);
      WARN("net-sircl: no route from %s (%s) to %s, the peer at position %d: a peer beyond a cable needs a relay "
           "table on the hosts between (README, Peers beyond a cable)", dv->name, dv->netdev, ip, r->position);
      result = ncclSystemError;
    }
  }
  close(fd);
  return result;
#endif
}

static ncclResult_t comm_ready(struct comm *c, const struct net_sircl_meta *r) {
  struct dev *dv = &g.devs[c->dev];
  if (r->magic != NET_SIRCL_MAGIC) {
    WARN("net-sircl: peer metadata has magic %08x", r->magic);
    return ncclRemoteError;
  }
  if (r->policy != g.policy || r->position < 0 || r->position >= g.nhosts ||
      !g.routes[r->position].ndev) {
    WARN("net-sircl: peer topology/budget disagreement or unknown position %d", r->position);
    return ncclRemoteError;
  }
  ncclResult_t rc = route_check(c, r);
  if (rc != ncclSuccess) return rc;
  struct sircl_core_qp_peer peer = {
    .qpn = r->qpn, .mtu = r->mtu < (uint32_t)dv->mtu ? r->mtu : (uint32_t)dv->mtu,
    .port = dv->port, .gid_index = (uint8_t)dv->gid_index, .hop_limit = 64,
    .traffic_class = (uint8_t)g.traffic_class
  };
  if (peer.mtu < IBV_MTU_256 || peer.mtu > IBV_MTU_4096) return ncclRemoteError;
  c->mtu_bytes = 128u << peer.mtu;
  memcpy(peer.gid, r->gid, 16);
  if (sircl_core_qp_rtr(c->qp, &peer)) {
    WARN("net-sircl: queue pair to RTR on %s failed: %s (peer position %d unreachable from this function?)",
         dv->name, strerror(errno), r->position);
    return ncclSystemError;
  }
  if (sircl_core_qp_rts(c->qp, (unsigned)g.ib_timeout, (unsigned)g.ib_retry_cnt)) {
    WARN("net-sircl: queue pair to RTS on %s failed: %s", dv->name, strerror(errno));
    return ncclSystemError;
  }
  c->rctrl_addr = r->ctrl_addr;
  c->rctrl_rkey = r->ctrl_rkey;
  c->peer_position = r->position;
  const struct route *route = &g.routes[r->position];
  c->win = route->relays ? &g.relay_windows[c->dev] :
           c->is_send ? window_for(c->dev, r->position, route->window) : NULL;
  if (c->is_send && route->window && !c->win) return ncclSystemError;
  if (c->win) window_join(c->win, c);
  return ncclSuccess;
}

static const struct route *route_of(int position) {
  if (position < 0 || position >= MAX_POSITIONS || g.routes[position].ndev == 0) return NULL;
  return &g.routes[position];
}

/* The connector's lane and function toward `position`: NCCL's device when it is one of the route's lanes
 * (NCCL spreads its channels over the devices, which keeps both PCI functions of a cable busy), else the
 * route's lanes in turn. Without a route, NCCL's device and no lane. */
static int connect_dev(int position, int nccl_dev, int *lane) {
  struct route *r = (struct route *)route_of(position);
  *lane = -1;
  if (!r) return nccl_dev;
  for (int l = 0; l < r->ndev; ++l)
    if (r->dev[l] == nccl_dev) {
      *lane = l;
      return nccl_dev;
    }
  pthread_mutex_lock(&g.lock);
  *lane = (int)(r->next++ % (unsigned)r->ndev);
  pthread_mutex_unlock(&g.lock);
  return r->dev[*lane];
}

/* The acceptor's function: the connector's lane of this side's route toward the connector, so both ends of
 * the queue pair are the same function class on the same path. */
static int accept_dev(int position, int lane, int listen_dev) {
  const struct route *r = route_of(position);
  if (!r) return listen_dev;
  if (lane >= 0 && lane < r->ndev) return r->dev[lane];
  for (int l = 0; l < r->ndev; ++l)
    if (r->dev[l] == listen_dev) return listen_dev;
  return r->dev[0];
}

/* -- listen / connect / accept -- */
static ncclResult_t sircl_listen(void *ctx, int d, void *handle, void **listenComm) {
  (void)ctx;
  if (d < 0 || d >= g.ndevs) return ncclInvalidArgument;
  struct net_sircl_handle *h = handle;
  memset(handle, 0, NCCL_NET_HANDLE_MAXSIZE);
  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return ncclSystemError;
  struct sockaddr_in sa;
  memset(&sa, 0, sizeof sa);
  sa.sin_family = AF_INET;
  sa.sin_addr.s_addr = htonl(INADDR_ANY);
  socklen_t len = sizeof sa;
  if (bind(fd, (struct sockaddr *)&sa, sizeof sa) || listen(fd, 128) ||
      getsockname(fd, (struct sockaddr *)&sa, &len) || set_nonblocking(fd)) {
    close(fd);
    return ncclSystemError;
  }
  h->magic = NET_SIRCL_MAGIC;
  h->version = NET_SIRCL_VERSION;
  h->port = sa.sin_port;
  h->addr = g.oob_addr;
  h->position = g.position;
  h->pending = 0;
  struct listen_comm *l = calloc(1, sizeof *l);
  if (!l) {
    close(fd);
    return ncclSystemError;
  }
  l->fd = fd;
  l->dev = d;
  *listenComm = l;
  return ncclSuccess;
}

static void pending_drop(struct pending *p) {
  if (!p) return;
  if (p->c) comm_destroy(p->c);
  if (p->fd >= 0) close(p->fd);
  free(p);
}

static ncclResult_t connect_fail(struct net_sircl_handle *h, struct pending *p, ncclResult_t r) {
  h->pending = 0;
  pending_drop(p);
  return r;
}

static ncclResult_t sircl_connect(void *ctx, int d, void *handle, void **sendComm,
                                  ncclNetDeviceHandle_v12_t **sendDevComm) {
  (void)ctx;
  (void)sendDevComm;
  *sendComm = NULL;
  struct net_sircl_handle *h = handle;
  if (h->magic != NET_SIRCL_MAGIC || h->version != NET_SIRCL_VERSION) {
    WARN("net-sircl: handle magic %08x version %u", h->magic, h->version);
    return ncclInvalidArgument;
  }
  if (!route_of(h->position)) return ncclInvalidArgument;
  struct pending *p = (struct pending *)(uintptr_t)h->pending;
  if (!p) {
    p = calloc(1, sizeof *p);
    if (!p) return ncclSystemError;
    p->deadline_ms = now_ms() + g.connect_timeout_ms;
    int lane = -1;
    p->dev = connect_dev(h->position, d, &lane);
    p->fd = socket(AF_INET, SOCK_STREAM, 0);
    if (p->fd < 0 || set_nonblocking(p->fd)) return connect_fail(h, p, ncclSystemError);
    int one = 1;
    setsockopt(p->fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = h->port;
    sa.sin_addr.s_addr = h->addr;
    if (connect(p->fd, (struct sockaddr *)&sa, sizeof sa) && errno != EINPROGRESS) {
      WARN("net-sircl: connecting to the listener: %s", strerror(errno));
      return connect_fail(h, p, ncclSystemError);
    }
    p->c = comm_create(p->dev, 1);
    if (!p->c) return connect_fail(h, p, ncclSystemError);
    p->c->lane = lane;
    p->state = P_CONNECTING;
    h->pending = (uint64_t)(uintptr_t)p;
  }
  if (now_ms() > p->deadline_ms) {
    WARN("net-sircl: connecting to position %d timed out after %d ms (NET_SIRCL_CONNECT_TIMEOUT_MS)", h->position,
         g.connect_timeout_ms);
    return connect_fail(h, p, ncclSystemError);
  }
  if (p->state == P_CONNECTING) {
    struct pollfd pf = {p->fd, POLLOUT, 0};
    if (poll(&pf, 1, 0) <= 0) return ncclSuccess;
    int err = 0;
    socklen_t len = sizeof err;
    getsockopt(p->fd, SOL_SOCKET, SO_ERROR, &err, &len);
    if (err) {
      WARN("net-sircl: connecting to the listener: %s", strerror(err));
      return connect_fail(h, p, ncclSystemError);
    }
    meta_fill(p->c, &p->mine);
    if (send_all(p->fd, &p->mine, sizeof p->mine)) return connect_fail(h, p, ncclSystemError);
    p->state = P_WAIT_META;
  }
  if (p->state == P_WAIT_META) {
    int r = recv_some(p->fd, &p->theirs, sizeof p->theirs, &p->got);
    if (r < 0) return connect_fail(h, p, ncclRemoteError);
    if (r == 0) return ncclSuccess;
    ncclResult_t rr = comm_ready(p->c, &p->theirs);
    if (rr != ncclSuccess) return connect_fail(h, p, rr);
    /* Ready: this queue pair is at RTS, so the acceptor may let its side write (as NCCL's IB transport
     * finishes its connect). */
    uint8_t ready = 1;
    if (send_all(p->fd, &ready, 1)) return connect_fail(h, p, ncclSystemError);
    p->c->fd = p->fd;
    p->fd = -1;
    *sendComm = p->c;
    INFO("net-sircl: send comm on %s (lane %d) to position %d, window %u", g.devs[p->c->dev].name, p->c->lane,
         p->c->peer_position, p->c->win ? p->c->win->cap : 0u);
    p->c = NULL;
    h->pending = 0;
    free(p);
  }
  return ncclSuccess;
}

static ncclResult_t accept_fail(struct listen_comm *l, ncclResult_t r) {
  pending_drop(l->accepting);
  l->accepting = NULL;
  return r;
}

static ncclResult_t sircl_accept(void *listenComm, void **recvComm, ncclNetDeviceHandle_v12_t **recvDevComm) {
  (void)recvDevComm;
  *recvComm = NULL;
  struct listen_comm *l = listenComm;
  struct pending *p = l->accepting;
  if (!p) {
    int fd = accept(l->fd, NULL, NULL);
    if (fd < 0) return (errno == EAGAIN || errno == EWOULDBLOCK) ? ncclSuccess : ncclSystemError;
    p = calloc(1, sizeof *p);
    if (!p) {
      close(fd);
      return ncclSystemError;
    }
    p->fd = fd;
    set_nonblocking(fd);
    p->state = P_WAIT_META;
    p->deadline_ms = now_ms() + g.connect_timeout_ms;
    l->accepting = p;
  }
  if (now_ms() > p->deadline_ms) {
    WARN("net-sircl: accepting a connection timed out after %d ms (NET_SIRCL_CONNECT_TIMEOUT_MS)",
         g.connect_timeout_ms);
    return accept_fail(l, ncclSystemError);
  }
  if (p->state == P_WAIT_META) {
    int r = recv_some(p->fd, &p->theirs, sizeof p->theirs, &p->got);
    if (r < 0) return accept_fail(l, ncclRemoteError);
    if (r == 0) return ncclSuccess;
    p->c = comm_create(accept_dev(p->theirs.position, p->theirs.lane, l->dev), 0);
    if (!p->c) return accept_fail(l, ncclSystemError);
    p->c->lane = p->theirs.lane;
    meta_fill(p->c, &p->mine);
    if (send_all(p->fd, &p->mine, sizeof p->mine)) return accept_fail(l, ncclSystemError);
    ncclResult_t rr = comm_ready(p->c, &p->theirs);
    if (rr != ncclSuccess) return accept_fail(l, rr);
    p->state = P_WAIT_READY;
  }
  int r = recv_some(p->fd, &p->ready, 1, &p->ready_got);
  if (r < 0) return accept_fail(l, ncclRemoteError);
  if (r == 0) return ncclSuccess;
  p->c->fd = p->fd;
  p->fd = -1;
  *recvComm = p->c;
  p->c = NULL;
  l->accepting = NULL;
  free(p);
  return ncclSuccess;
}

/* -- memory -- */
static ncclResult_t sircl_reg_mr(void *comm, void *data, size_t size, int type, void **mhandle) {
  struct comm *c = comm;
  if (type != NCCL_PTR_HOST) {
    WARN("net-sircl: only host memory is registered (type %d)", type);
    return ncclInternalError;
  }
  struct mr *m = calloc(1, sizeof *m);
  if (!m) return ncclSystemError;
  m->base = data;
  m->size = size;
  int access = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE;
#ifdef NET_SIRCL_STANDIN
  m->shadow = shared_alloc(size);
  void *target = m->shadow;
#else
  /* Relaxed ordering lets the PCIe root complex reorder the NIC's writes into host memory, which NCCL's own
   * IB transport also asks for ("[RO]" in its INIT log); providers that cannot do it ignore the flag. */
  if (g.relaxed_ordering) access |= IBV_ACCESS_RELAXED_ORDERING;
  void *target = data;
#endif
  m->ib = target ? sircl_core_reg_mr(g.devs[c->dev].pd, target, size, access) : NULL;
  if (!m->ib) {
    WARN("net-sircl: registering %zu bytes: %s", size, strerror(errno));
    shared_free(m->shadow);
    free(m);
    return ncclSystemError;
  }
  *mhandle = m;
  return ncclSuccess;
}

static ncclResult_t sircl_reg_mr_dmabuf(void *comm, void *data, size_t size, int type, uint64_t offset, int fd,
                                        void **mhandle) {
  (void)comm; (void)data; (void)size; (void)type; (void)offset; (void)fd; (void)mhandle;
  WARN("net-sircl: DMA-BUF registration is not offered (ptrSupport is host only)");
  return ncclInternalError;
}

static ncclResult_t sircl_dereg_mr(void *comm, void *mhandle) {
  (void)comm;
  struct mr *m = mhandle;
  if (!m) return ncclSuccess;
  if (m->ib) sircl_core_dereg_mr(m->ib);
  shared_free(m->shadow);
  free(m);
  return ncclSuccess;
}

/* The NIC's address for `p` inside registration `m` (the shadow's under the stand-in). */
static uint64_t nic_addr(const struct mr *m, const void *p) {
  if (!m) return (uint64_t)(uintptr_t)p;
  size_t off = (size_t)((const uint8_t *)p - m->base);
  return (uint64_t)(uintptr_t)((m->shadow ? m->shadow : m->base) + off);
}

/* -- data path -- */
static int post_write(struct comm *c, uint64_t laddr, uint32_t lkey, uint32_t len, uint64_t raddr, uint32_t rkey,
                      int inline_data, int signaled, uint64_t wr_id) {
  if (profile_enabled)
    return profile_post(c, laddr, lkey, len, raddr, rkey,
                        (signaled ? IBV_SEND_SIGNALED : 0) | (inline_data ? IBV_SEND_INLINE : 0), wr_id);
  return sircl_core_post_write(c->qp, laddr, lkey, len, raddr, rkey,
                               (signaled ? IBV_SEND_SIGNALED : 0) | (inline_data ? IBV_SEND_INLINE : 0), wr_id);
}

/* Charge 64-byte buffer strides plus 128 bytes of frame overhead and one 128-byte ACK
 * per packet. RC send completion proves both the write and its ACK have left the relay path.
 * The reserve counts both directions, even if the hardware gives them independent queues. */
static uint64_t write_charge(struct comm *c, uint32_t bytes) {
  if (!c->win || !c->win->relay) return bytes;
  uint64_t packets = bytes ? ((uint64_t)bytes + c->mtu_bytes - 1) / c->mtu_bytes : 1;
  return (((uint64_t)bytes + 63u) & ~UINT64_C(63)) + packets * 256u;
}

static uint64_t ack_charge(struct comm *c, uint32_t bytes) {
  if (!c->win || !c->win->ack_cap) return 0;
  return 128u * (bytes ? ((uint64_t)bytes + c->mtu_bytes - 1) / c->mtu_bytes : 1);
}

static int window_take(struct window *w, uint64_t bytes) {
  return !w->error && sircl_core_window_claim(&w->inflight, &w->max_inflight, w->cap, bytes, 0);
}

/* Reserve both resources atomically under the shared window lock. No WR is
 * posted while holding only one resource, so there is no resource wait cycle. */
static int window_take_ack(struct window *w, uint64_t bytes, uint64_t ack) {
  if (w->ack_cap && ack > w->ack_cap - w->ack_inflight) return 0;
  if (!window_take(w, bytes)) return 0;
  w->ack_inflight += ack;
  return 1;
}

/* Caller holds the window lock. Fit the available charge, retaining at least two
 * independently completed pieces per full allowance when two 4 KiB pieces fit.
 * Avoid tiny residual writes unless they finish a request or the entire allowance
 * is smaller: otherwise packet/CQE overhead defeats the larger completion chunks. */
static uint32_t chunk_bytes(struct comm *c, uint32_t left) {
  uint32_t hi = left < g.chunk ? left : g.chunk, lo = 0;
  if (c->win->ack_cap) {
    uint64_t packet_bytes = (c->win->ack_cap - c->win->ack_inflight) / 128u * c->mtu_bytes;
    if (packet_bytes < hi) hi = (uint32_t)packet_bytes;
  }
  uint64_t cap = c->win->cap;
  uint64_t available = cap - c->win->inflight;
  uint64_t target = cap >= 2 * write_charge(c, 4096) ? cap / 2 : cap;
  if (available < target) target = available;
  /* Saturated channels are the common retry case: avoid a binary search when
   * even the smallest useful piece must wait for a completion. */
  uint32_t minimum = cap >= write_charge(c, 4096) ? (left < 4096 ? left : 4096) : 1;
  if (target < write_charge(c, minimum)) return 0;
  if (hi < minimum) return 0;
  if (write_charge(c, hi) <= target) return hi;
  while (lo < hi) {
    uint32_t mid = lo + (hi - lo + 1) / 2;
    if (write_charge(c, mid) <= target) lo = mid;
    else hi = mid - 1;
  }
  if (lo < left) lo &= ~63u;
  if (lo < 4096 && lo < left && cap >= write_charge(c, 4096)) return 0;
  return lo;
}

/* Post what the window allows of a send, then its size and done words. */
static ncclResult_t send_progress(struct comm *c, struct req *q) {
  while (q->posted < q->total) {
    uint32_t left = q->total - q->posted;
    uint32_t piece = c->win ? chunk_bytes(c, left) : left;
    if (profile_enabled) profile_wait(c, q, !piece);
    if (!piece) return ncclSuccess;
    uint64_t charge = write_charge(c, piece);
    uint64_t ack = ack_charge(c, piece);
    if (c->win && !window_take_ack(c->win, charge, ack)) return c->win->error;
    if (post_write(c, q->laddr + q->posted, q->lkey, piece, q->raddr + q->posted, q->rkey, 0, c->win != NULL,
                   WR_ACK_ID(WR_DATA, charge, q->slot, ack))) {
      if (c->win) {
        sircl_core_window_release(&c->win->inflight, charge);
        c->win->ack_inflight -= ack;
        c->win->error = ncclSystemError;
      }
      WARN("net-sircl: posting %u payload bytes: %s", piece, strerror(errno));
      return ncclSystemError;
    }
    q->posted += piece;
    c->chunks++;
    if (c->win) c->inflight += charge;
  }
  if (!q->final_posted) {
    uint64_t charge = c->win && c->win->relay ? 2 * write_charge(c, 4) : 0;
    uint64_t ack = 2 * ack_charge(c, 4);
    int blocked = charge && !window_take_ack(c->win, charge, ack);
    if (profile_enabled) profile_wait(c, q, blocked);
    if (blocked) return c->win->error;
    c->inflight += charge;
    uint32_t size = q->total, seq = q->seq + 1;
    uint64_t base = c->rctrl_addr;
    if (post_write(c, (uint64_t)(uintptr_t)&size, 0, 4, base + offsetof(struct net_sircl_ctrl, done_size) + 4 * q->slot,
                   c->rctrl_rkey, 1, 0, 0) ||
        post_write(c, (uint64_t)(uintptr_t)&seq, 0, 4, base + offsetof(struct net_sircl_ctrl, done_seq) + 4 * q->slot,
                   c->rctrl_rkey, 1, 1, WR_ACK_ID(WR_FINAL, charge, q->slot, ack))) {
      if (c->win) c->win->error = ncclSystemError; /* a partial pair cannot return credit */
      WARN("net-sircl: posting a completion word: %s", strerror(errno));
      return ncclSystemError;
    }
    q->final_posted = 1;
  }
  return ncclSuccess;
}

static ncclResult_t check_wait(struct comm *c, int64_t started_ms) {
  if (c->error) return c->error;
  if (c->waits++ % PEER_CHECK_EVERY) return ncclSuccess;
  if (peer_gone(c)) {
    WARN("net-sircl: the peer at position %d closed its connection with requests outstanding", c->peer_position);
  } else if (now_ms() - started_ms >= g.request_timeout_ms) {
    WARN("net-sircl: request to position %d timed out after %d ms; check peer progress and fabric-tool verify",
         c->peer_position, g.request_timeout_ms);
  } else return ncclSuccess;
  c->error = ncclRemoteError;
  if (c->win) c->win->error = ncclRemoteError;
  return ncclRemoteError;
}

static ncclResult_t submit_progress(struct comm *c, struct req *q);

static ncclResult_t isend_locked(void *sendComm, void *data, size_t size, int tag, void *mhandle, void *phandle,
                                void **request) {
  (void)phandle;
  struct comm *c = sendComm;
  *request = NULL;
  uint32_t slot = c->seq % NET_SIRCL_SLOTS;
  struct req *q = &c->reqs[slot];
  if (q->used) return ncclSuccess;
  if (__atomic_load_n(&c->ctrl->cts_seq[slot], __ATOMIC_ACQUIRE) != c->seq + 1) {
    if (!c->cts_wait_ms) c->cts_wait_ms = now_ms();
    return check_wait(c, c->cts_wait_ms);
  }
  c->cts_wait_ms = 0;
  struct net_sircl_cts e = c->ctrl->cts[slot];
  if ((int)e.tag != tag) {
    WARN("net-sircl: send tag %d meets receive tag %u in slot %u", tag, e.tag, slot);
    return ncclInternalError;
  }
  if (size > e.size) {
    WARN("net-sircl: send of %zu bytes meets a receive of %u", size, e.size);
    return ncclInternalError;
  }
  struct mr *m = mhandle;
  memset(q, 0, sizeof *q);
  q->started_ms = now_ms();
  q->used = q->is_send = 1;
  q->c = c;
  q->seq = c->seq++;
  q->slot = slot;
  q->total = (uint32_t)size;
  q->laddr = nic_addr(m, data);
  q->lkey = m ? m->ib->lkey : 0;
  q->raddr = e.addr;
  q->rkey = e.rkey;
  if (m && m->shadow && size) memcpy((void *)(uintptr_t)q->laddr, data, size);
  ncclResult_t r = submit_progress(c, q);
  if (r != ncclSuccess) return r;
  *request = q;
  return ncclSuccess;
}

static ncclResult_t poll_cq(struct comm *c);

static ncclResult_t sircl_isend(void *sendComm, void *data, size_t size, int tag, void *mhandle,
                                  void *phandle, void **request) {
  *request = NULL;
  struct comm *c = sendComm;
  if (c->win) pthread_mutex_lock(&c->win->lock);
  /* Submission retries must make progress even before NCCL owns a request
   * to test. No CUDA operation or stream wait participates in credit return. */
  ncclResult_t r = c->error ? c->error : (c->win ? poll_cq(c) : ncclSuccess);
  if (r == ncclSuccess) r = isend_locked(sendComm, data, size, tag, mhandle, phandle, request);
  if (r == ncclRemoteError || r == ncclSystemError) c->error = r;
  if (c->win) pthread_mutex_unlock(&c->win->lock);
  return r;
}

static ncclResult_t recv_progress(struct comm *c, struct req *q) {
  if (q->cts_posted) return ncclSuccess;
  uint64_t charge = c->win ? write_charge(c, sizeof(struct net_sircl_cts)) + write_charge(c, 4) : 0;
  uint64_t ack = ack_charge(c, sizeof(struct net_sircl_cts)) + ack_charge(c, 4);
  if (charge && !window_take_ack(c->win, charge, ack)) return c->win->error;
  c->inflight += charge;
  struct net_sircl_cts *e = &c->ctrl->cts[q->slot];
  uint32_t seq = q->seq + 1;
  uint64_t base = c->rctrl_addr;
  if (post_write(c, (uint64_t)(uintptr_t)e, c->ctrl_mr->lkey, sizeof *e,
                 base + offsetof(struct net_sircl_ctrl, cts) + sizeof *e * q->slot, c->rctrl_rkey, 0, 0, 0) ||
      post_write(c, (uint64_t)(uintptr_t)&seq, 0, 4, base + offsetof(struct net_sircl_ctrl, cts_seq) + 4 * q->slot,
                 c->rctrl_rkey, 1, 1, WR_ACK_ID(WR_CTS, charge, q->slot, ack))) {
    if (c->win) c->win->error = ncclSystemError;
    WARN("net-sircl: posting a clear-to-send entry: %s", strerror(errno));
    return ncclSystemError;
  }
  q->cts_posted = 1;
  return ncclSuccess;
}

static void pending_append(struct window *w, struct req *q) {
  q->pending_next = NULL;
  if (w->pending_tail) w->pending_tail->pending_next = q;
  else w->pending_head = q;
  w->pending_tail = q;
}

/* FIFO requests, independent of CQ ownership. A blocked head retains its turn
 * until all its writes are posted. Both resources are acquired together by
 * the ordinary posting functions. Enqueueing reserves no credit; posted WRs
 * keep their charge until RC completion. A visit stops when credit is short. */
static ncclResult_t advance_pending(struct window *w) {
  while (w->pending_head) {
    struct req *q = w->pending_head;
    ncclResult_t r = q->is_send ? send_progress(q->c, q) : recv_progress(q->c, q);
    if (r != ncclSuccess) return r;
    int finished = q->is_send ? q->final_posted : q->cts_posted;
    if (!finished) return ncclSuccess;
    w->pending_head = q->pending_next;
    if (!w->pending_head) w->pending_tail = NULL;
    q->pending_next = NULL;
    if (profile_enabled) profile_wait(q->c, q, 0);
  }
  return ncclSuccess;
}

static ncclResult_t submit_progress(struct comm *c, struct req *q) {
  if (!c->win || !c->win->relay)
    return q->is_send ? send_progress(c, q) : recv_progress(c, q);
  pending_append(c->win, q);
  if (profile_enabled && q->is_send && c->win->pending_head != q) profile_wait(c, q, 1);
  return advance_pending(c->win);
}

static ncclResult_t irecv_locked(void *recvComm, int n, void **data, size_t *sizes, int *tags, void **mhandles,
                                void **phandles, void **request) {
  (void)phandles;
  struct comm *c = recvComm;
  *request = NULL;
  if (n != 1) {
    WARN("net-sircl: grouped receives (%d) are not offered (maxRecvs 1)", n);
    return ncclInternalError;
  }
  uint32_t slot = c->seq % NET_SIRCL_SLOTS;
  struct req *q = &c->reqs[slot];
  if (q->used) return ncclSuccess;
  struct mr *m = mhandles ? mhandles[0] : NULL;
  struct net_sircl_cts *e = &c->ctrl->cts[slot];
  e->addr = nic_addr(m, data[0]);
  e->rkey = m ? m->ib->rkey : 0;
  e->size = sizes[0] > MAX_NET_BYTES ? MAX_NET_BYTES : (uint32_t)sizes[0];
  e->tag = (uint32_t)tags[0];
  memset(q, 0, sizeof *q);
  q->started_ms = now_ms();
  q->used = 1;
  q->c = c;
  q->seq = c->seq++;
  q->slot = slot;
  q->user = data[0];
  q->shadow = (m && m->shadow) ? (uint8_t *)(uintptr_t)e->addr : NULL;
  ncclResult_t r = submit_progress(c, q);
  if (r != ncclSuccess) return r;
  *request = q;
  return ncclSuccess;
}

static ncclResult_t sircl_irecv(void *recvComm, int n, void **data, size_t *sizes, int *tags, void **mhandles,
                                void **phandles, void **request) {
  *request = NULL;
  struct comm *c = recvComm;
  if (c && c->win) pthread_mutex_lock(&c->win->lock);
  ncclResult_t r = c && c->error ? c->error : (c && c->win ? poll_cq(c) : ncclSuccess);
  if (r == ncclSuccess) r = irecv_locked(recvComm, n, data, sizes, tags, mhandles, phandles, request);
  if (c && (r == ncclRemoteError || r == ncclSystemError)) c->error = r;
  if (c && c->win) pthread_mutex_unlock(&c->win->lock);
  return r;
}

static ncclResult_t sircl_iflush(void *recvComm, int n, void **data, int *sizes, void **mhandles, void **request) {
  (void)recvComm; (void)n; (void)data; (void)sizes; (void)mhandles;
  *request = NULL; /* host buffers only: nothing to flush toward the GPU */
  return ncclSuccess;
}

/* Reap one bounded batch from a comm. Every progress sweep revisits active
 * QPs, so remaining CQEs retain their charge until a later sweep. A nonempty
 * batch does not need a trailing empty poll before requests can use credit. */
static ncclResult_t drain(struct comm *c) {
  struct ibv_wc wc[32];
  int n;
  if ((n = profile_enabled ? profile_poll(c, wc) : sircl_core_poll_cq(c->cq, 32, wc)) > 0) {
    for (int i = 0; i < n; ++i) {
      if (wc[i].status != IBV_WC_SUCCESS) {
        WARN("net-sircl: completion error %d (%s) on %s toward position %d%s", (int)wc[i].status,
             ibv_wc_status_str(wc[i].status), g.devs[c->dev].name, c->peer_position,
             wc[i].status == IBV_WC_RETRY_EXC_ERR
               ? "; ACK retry budget exhausted: raise NET_SIRCL_IB_TIMEOUT (maximum 31); check peer and path health"
               : "");
        if (c->win) c->win->error = ncclRemoteError;
        return ncclRemoteError;
      }
      uint64_t id = wc[i].wr_id;
      int kind = (int)(id >> 56);
      uint32_t slot = (uint32_t)(id & 0xff);
      if (c->win) {
        uint64_t bytes = WR_CHARGE(id);
        c->win->ack_inflight -= WR_ACK(id);
        c->inflight -= bytes;
        sircl_core_window_release(&c->win->inflight, bytes);
      }
      if (profile_enabled) profile_complete(c, id);
      if (kind == WR_FINAL && slot < NET_SIRCL_SLOTS) c->reqs[slot].done = 1;
      if (kind == WR_CTS && slot < NET_SIRCL_SLOTS) c->reqs[slot].cts_done = 1;
    }
  }
  return n < 0 ? ncclSystemError : ncclSuccess;
}

/* Post a windowed comm's waiting payload or CTS writes, oldest request first. */
static ncclResult_t advance(struct comm *c) {
  for (uint32_t s = c->head; s != c->seq; ++s) {
    struct req *q = &c->reqs[s % NET_SIRCL_SLOTS];
    if (!q->used) continue;
    ncclResult_t r = q->is_send ? send_progress(c, q) : recv_progress(c, q);
    if (r != ncclSuccess) return r;
    if (q->is_send ? !q->final_posted : !q->cts_posted) break;
  }
  return ncclSuccess;
}

/* Caller holds the shared window lock across progress and request retirement. */
static ncclResult_t poll_cq(struct comm *c) {
  if (c->error) return c->error;
  if (!c->win) return drain(c);
  struct window *w = c->win;
  if (w->error) return w->error;
  for (struct comm *m = w->members; m; m = m->next) {
    /* Every relay CQE carries positive charge, including CTS and final pairs.
     * A zero balance proves there is nothing to reap; receive done words are
     * still checked by test_locked. Explicit direct windows have zero-charge
     * final CQEs and must retain unconditional polling. */
    ncclResult_t r = ncclSuccess;
    if (!w->relay || m->inflight) r = drain(m);
    if (r != ncclSuccess) return w->error = r;
    /* Reuse completed credit before polling unrelated CQs, granting it to
     * the oldest waiting request rather than preferentially to the CQ owner. */
    r = w->relay ? advance_pending(w) : advance(m);
    if (r != ncclSuccess) return w->error = r;
  }
  /* Rotate CQ visits; relay admission has its own FIFO order. */
  if (w->members && w->members->next) {
    struct comm *first = w->members, *last = first;
    while (last->next) last = last->next;
    w->members = first->next;
    first->next = NULL;
    last->next = first;
  }
  return ncclSuccess;
}

static ncclResult_t test_locked(void *request, int *done, int *sizes) {
  struct req *q = request;
  struct comm *c = q->c;
  *done = 0;
  ncclResult_t r = poll_cq(c);
  if (r != ncclSuccess) return r;
  if (q->is_send) {
    if (!q->done) return check_wait(c, q->started_ms);
    if (sizes) *sizes = (int)q->total;
  } else {
    if (!q->cts_done) return check_wait(c, q->started_ms);
    if (__atomic_load_n(&c->ctrl->done_seq[q->slot], __ATOMIC_ACQUIRE) != q->seq + 1)
      return check_wait(c, q->started_ms);
    uint32_t got = __atomic_load_n(&c->ctrl->done_size[q->slot], __ATOMIC_ACQUIRE);
    if (q->shadow && got) memcpy(q->user, q->shadow, got);
    if (sizes) *sizes = (int)got;
  }
  q->used = 0;
  *done = 1;
  c->waits = 0;
  while (c->head != c->seq && !c->reqs[c->head % NET_SIRCL_SLOTS].used) c->head++;
  return ncclSuccess;
}

static ncclResult_t sircl_test(void *request, int *done, int *sizes) {
  struct comm *c = ((struct req *)request)->c;
  if (c->win) pthread_mutex_lock(&c->win->lock);
  ncclResult_t r = test_locked(request, done, sizes);
  if (r != ncclSuccess) c->error = r;
  if (c->win) pthread_mutex_unlock(&c->win->lock);
  return r;
}

/* Closing never waits for the peer: outstanding requests are abandoned with their queue pair. */
static ncclResult_t sircl_close_send(void *sendComm) {
  comm_destroy(sendComm);
  return ncclSuccess;
}

static ncclResult_t sircl_close_recv(void *recvComm) {
  comm_destroy(recvComm);
  return ncclSuccess;
}

static ncclResult_t sircl_close_listen(void *listenComm) {
  struct listen_comm *l = listenComm;
  if (!l) return ncclSuccess;
  pending_drop(l->accepting);
  close(l->fd);
  free(l);
  return ncclSuccess;
}

static ncclResult_t sircl_set_net_attr(void *ctx, ncclNetAttr_v12_t *attr) {
  (void)ctx; (void)attr;
  return ncclSuccess;
}

void net_sircl_stats(void *send_comm, uint64_t *max_inflight, uint64_t *chunks) {
  struct comm *c = send_comm;
  *max_inflight = c->win ? __atomic_load_n(&c->win->max_inflight, __ATOMIC_RELAXED) : 0;
  *chunks = c->chunks;
}

int net_sircl_comm_device(void *comm, char *name, size_t len) {
  struct comm *c = comm;
  snprintf(name, len, "%s", g.devs[c->dev].name);
  return c->lane;
}

/* makeVDevice is NULL: NCCL then uses the physical devices and never merges them (graph/topo.cc:1854 at
 * v2.32.3-1). getDeviceMr and irecvConsumed are NULL, as in NCCL's own IB transport. */
__attribute__((visibility("default"))) const ncclNet_v12_t ncclNetPlugin_v12 = {
    .name = "sircl",
    .init = sircl_init,
    .devices = sircl_devices,
    .getProperties = sircl_get_properties,
    .listen = sircl_listen,
    .connect = sircl_connect,
    .accept = sircl_accept,
    .regMr = sircl_reg_mr,
    .regMrDmaBuf = sircl_reg_mr_dmabuf,
    .deregMr = sircl_dereg_mr,
    .isend = sircl_isend,
    .irecv = sircl_irecv,
    .iflush = sircl_iflush,
    .test = sircl_test,
    .closeSend = sircl_close_send,
    .closeRecv = sircl_close_recv,
    .closeListen = sircl_close_listen,
    .getDeviceMr = NULL,
    .irecvConsumed = NULL,
    .makeVDevice = NULL,
    .finalize = sircl_finalize,
    .setNetAttr = sircl_set_net_attr,
};

#include "net_sircl_compat.h"
