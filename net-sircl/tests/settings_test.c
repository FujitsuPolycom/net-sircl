/* Exercise initialization, actual RTS attributes and completion diagnostics over the CPU stand-in.
 * Link wrappers observe the provider boundary without changing the shared emulator. */
#include "../net_sircl.c"
#include <assert.h>

static struct ibv_qp_attr observed;
static int rts_calls, injected_status, advisories;
static char last_log[1024];

int __real_sccl_emu_ibv_modify_qp(struct ibv_qp *, struct ibv_qp_attr *, int);
int __wrap_sccl_emu_ibv_modify_qp(struct ibv_qp *qp, struct ibv_qp_attr *attr, int mask) {
  if ((mask & IBV_QP_STATE) && attr->qp_state == IBV_QPS_RTS) {
    assert((mask & (IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY)) ==
           (IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY));
    observed = *attr;
    ++rts_calls;
  }
  return __real_sccl_emu_ibv_modify_qp(qp, attr, mask);
}

int __real_sccl_emu_ibv_poll_cq(struct ibv_cq *, int, struct ibv_wc *);
int __wrap_sccl_emu_ibv_poll_cq(struct ibv_cq *cq, int count, struct ibv_wc *wc) {
  if (injected_status) {
    memset(wc, 0, sizeof *wc);
    wc->status = injected_status;
    injected_status = 0;
    return 1;
  }
  return __real_sccl_emu_ibv_poll_cq(cq, count, wc);
}

static void capture(ncclDebugLogLevel level, unsigned long flags, const char *file, int line,
                    const char *fmt, ...) {
  (void)level; (void)flags; (void)file; (void)line;
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(last_log, sizeof last_log, fmt, ap);
  if (strstr(last_log, "recommended job default NCCL_MAX_NCHANNELS=8")) ++advisories;
  va_end(ap);
}

static void setting(const char *name, const char *value) {
  if (value) assert(!setenv(name, value, 1));
  else assert(!unsetenv(name));
}

static void check_qps(int timeout, int retry) {
  /* Both data senders and receive-side CTS senders enter RTS. */
  for (int position = 1; position <= 3; position += 2)
  for (int send = 0; send <= 1; ++send) {
    struct comm *c = comm_create(0, send);
    assert(c);
    struct net_sircl_meta peer;
    meta_fill(c, &peer);
    peer.position = position;
    int before = rts_calls;
    assert(comm_ready(c, &peer) == ncclSuccess);
    assert(rts_calls == before + 1);
    assert(observed.timeout == timeout && observed.retry_cnt == retry);
    assert(observed.rnr_retry == 7); /* RNR is independent of ACK retries. */
    injected_status = IBV_WC_RETRY_EXC_ERR;
    assert(drain(c) == ncclRemoteError);
    assert(strstr(last_log, "completion error 12"));
    assert(strstr(last_log, "NET_SIRCL_IB_TIMEOUT"));
    assert(strstr(last_log, position == 1 ? "position 1" : "position 3"));
    injected_status = IBV_WC_LOC_PROT_ERR;
    assert(drain(c) == ncclRemoteError);
    assert(!strstr(last_log, "raise NET_SIRCL_IB_TIMEOUT"));
    comm_destroy(c);
    g.relay_windows[0].error = ncclSuccess;
  }
}

static void valid(const char *timeout, const char *retry, int want_timeout, int want_retry) {
  setting("NET_SIRCL_IB_TIMEOUT", timeout);
  setting("NET_SIRCL_IB_RETRY_CNT", retry);
  void *ctx = NULL, *other = NULL;
  assert(sircl_init(&ctx, 0, NULL, capture, NULL) == ncclSuccess);
  char expected[80];
  snprintf(expected, sizeof expected, "IB timeout %d, retry count %d", want_timeout, want_retry);
  assert(strstr(last_log, expected));
  check_qps(want_timeout, want_retry);
  /* Live communicators share a snapshot; later environment edits cannot alter their QPs. */
  setting("NET_SIRCL_IB_TIMEOUT", "invalid");
  assert(sircl_init(&other, 0, NULL, capture, NULL) == ncclSuccess);
  assert(ctx == other);
  check_qps(want_timeout, want_retry);
  assert(sircl_finalize(other) == ncclSuccess);
  assert(g.refs == 1);
  assert(sircl_finalize(ctx) == ncclSuccess);
  assert(!g.refs && !g.ndevs);
}

static void check_channel_advisory(void) {
  const char *caps[] = {"NCCL_MAX_NCHANNELS", "NCCL_MAX_NRINGS", "NCCL_MAX_CTAS"};
  for (unsigned i = 0; i < 3; ++i) setting(caps[i], NULL);
  void *ctx = NULL, *other = NULL;
  assert(sircl_init(&ctx, 0, NULL, capture, NULL) == ncclSuccess);
  assert(advisories == 1);
  assert(sircl_init(&other, 0, NULL, capture, NULL) == ncclSuccess);
  assert(advisories == 1); /* Shared initialization emits only one advisory. */
  assert(!getenv("NCCL_MAX_NCHANNELS")); /* Never mutate NCCL's configuration. */
  for (unsigned i = 0; i < 3; ++i) {
    setting(caps[i], "24");
    advise_relay_channels();
    assert(advisories == 1);
    setting(caps[i], NULL);
  }
  /* A topology containing only direct peers has no recommendation. */
  struct route saved[MAX_POSITIONS];
  memcpy(saved, g.routes, sizeof saved);
  for (int peer = 0; peer < g.nhosts; ++peer) g.routes[peer].relays = 0;
  advise_relay_channels();
  assert(advisories == 1);
  memcpy(g.routes, saved, sizeof saved);
  assert(sircl_finalize(other) == ncclSuccess);
  assert(sircl_finalize(ctx) == ncclSuccess);
}

int main(void) {
  setting("NET_SIRCL_OOB_ADDR", "127.0.0.1");
  check_channel_advisory();
  setting("NCCL_IB_TIMEOUT", "13");
  setting("NCCL_IB_RETRY_CNT", "2");
  valid(NULL, NULL, 20, 7); /* NCCL's environment does not override plugin settings. */
  valid("14", "7", 14, 7);
  valid("1", "0", 1, 0);
  valid("31", "7", 31, 7);
  valid("21", "3", 21, 3);
  const char *names[] = {"NET_SIRCL_IB_TIMEOUT", "NET_SIRCL_IB_RETRY_CNT"};
  const char *bad[] = {"", " ", "-1", "+2", "1x", "1.5", "0x14", "2 ", " 2",
                       "32", "256", "999999999999999999999999999999999999999"};
  for (unsigned n = 0; n < sizeof names / sizeof names[0]; ++n) {
    for (unsigned i = 0; i < sizeof bad / sizeof bad[0] + 1; ++i) {
      setting(names[0], NULL);
      setting(names[1], NULL);
      setting(names[n], i < sizeof bad / sizeof bad[0] ? bad[i] : (n ? "8" : "0"));
      void *ctx = NULL;
      assert(sircl_init(&ctx, 0, NULL, capture, NULL) == ncclInvalidArgument);
      assert(!ctx && !g.refs && !g.ndevs);
      assert(strstr(last_log, names[n]));
    }
  }
  valid(NULL, NULL, 20, 7); /* Invalid initialization does not poison a subsequent one. */
  const char *invalid_budgets[] = {"", "0", "65535", "327681", "393216", "-1", "+262144", "262144 ", " 262144",
                                  "1x", "999999999999999999999999999"};
  setting("NET_SIRCL_IB_TIMEOUT", NULL);
  for (unsigned i = 0; i < sizeof invalid_budgets / sizeof invalid_budgets[0]; ++i) {
    setting("NET_SIRCL_RELAY_BUDGET", invalid_budgets[i]);
    void *ctx = NULL;
    assert(sircl_init(&ctx, 0, NULL, capture, NULL) == ncclInvalidArgument);
    assert(!ctx && !g.refs && !g.ndevs);
  }
  const char *valid_budgets[] = {"65536", "262144", "327680"};
  for (unsigned i = 0; i < 3; ++i) {
    setting("NET_SIRCL_RELAY_BUDGET", valid_budgets[i]);
    valid(NULL, NULL, 20, 7);
  }
  setting("NET_SIRCL_RELAY_BUDGET", NULL);
  setting("NET_SIRCL_IB_TIMEOUT", NULL);
  const char *bad_windows[] = {"", "3=0", "3=-1", "3=+1024", "3=1023", "3=1024,3=2048", "3=65536,",
                               "3=65536junk", "63=65536", "3=65536,,4=65536"};
  for (unsigned i = 0; i < sizeof bad_windows / sizeof bad_windows[0]; ++i) {
    setting("NET_SIRCL_WINDOWS", bad_windows[i]);
    void *ctx = NULL;
    assert(sircl_init(&ctx, 0, NULL, capture, NULL) == ncclInvalidArgument);
    assert(!ctx && !g.refs && !g.ndevs);
  }
  setting("NET_SIRCL_WINDOWS", "3=1073741824");
  void *ctx = NULL;
  assert(sircl_init(&ctx, 0, NULL, capture, NULL) == ncclSuccess);
  assert(g.relay_windows[0].cap <= RELAY_BUDGET_DEFAULT / (unsigned)(g.nhosts - 2));
  struct net_sircl_handle old = {.magic = NET_SIRCL_MAGIC, .version = 4};
  void *old_comm = NULL;
  assert(sircl_connect(ctx, 0, &old, &old_comm, NULL) == ncclInvalidArgument && !old_comm);
  struct comm *c = comm_create(0, 1);
  struct net_sircl_meta peer;
  meta_fill(c, &peer);
  peer.position = 3;
  peer.policy ^= 1;
  assert(comm_ready(c, &peer) == ncclRemoteError);
  peer.policy ^= 1;
  peer.position = 63;
  assert(comm_ready(c, &peer) == ncclRemoteError);
  comm_destroy(c);
  assert(sircl_finalize(ctx) == ncclSuccess);
  setting("NET_SIRCL_WINDOWS", NULL);
  setting("NET_SIRCL_ROUTES", "3=emu1/emu0");
  assert(sircl_init(&ctx, 0, NULL, capture, NULL) == ncclInvalidArgument);
  assert(!g.refs && !g.ndevs);
  setting("NET_SIRCL_ROUTES", NULL);
  setting("NET_SIRCL_TOPOLOGY", NULL);
  assert(sircl_init(&ctx, 0, NULL, capture, NULL) == ncclInvalidArgument);
  assert(!g.refs && !g.ndevs);
  puts("net-sircl settings: ranges, lifetime, RTS attributes and error hints exact");
  return 0;
}
