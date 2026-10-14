/* Provider-boundary regression: hold completions while many receive comms issue CTS traffic.
 * No GPU or physical NIC. A payload-only limiter admits an unbounded control burst. */
#include "../net_sircl.c"
#include <assert.h>
#include <sys/wait.h>

static uint64_t admitted;
static int hold = 1;
static int completion_quota = -1;
static unsigned fail_after;
int __real_sccl_emu_ibv_post_send(struct ibv_qp *, struct ibv_send_wr *, struct ibv_send_wr **);
int __wrap_sccl_emu_ibv_post_send(struct ibv_qp *qp, struct ibv_send_wr *wr, struct ibv_send_wr **bad) {
  if (fail_after && !--fail_after) { errno = EIO; return -1; }
  int rc = __real_sccl_emu_ibv_post_send(qp, wr, bad);
  if (!rc) for (struct ibv_send_wr *w = wr; w; w = w->next) {
    unsigned bytes = w->num_sge ? w->sg_list[0].length : 0;
    __atomic_fetch_add(&admitted, ((bytes + 63u) & ~63u) + 256u, __ATOMIC_RELAXED);
  }
  return rc;
}
int __real_sccl_emu_ibv_poll_cq(struct ibv_cq *, int, struct ibv_wc *);
int __wrap_sccl_emu_ibv_poll_cq(struct ibv_cq *cq, int n, struct ibv_wc *wc) {
  if (hold || !completion_quota) return 0;
  if (completion_quota > 0 && n > completion_quota) n = completion_quota;
  int result = __real_sccl_emu_ibv_poll_cq(cq, n, wc);
  if (completion_quota > 0 && result > 0) completion_quota -= result;
  return result;
}

static void *issue_receives(void *arg) {
  struct comm **comms = arg;
  for (int i = 0; i < 24; ++i)
    for (int j = 0; j < NET_SIRCL_SLOTS; ++j) {
      void *data = NULL, *request = NULL;
      size_t size = 0;
      int tag = j;
      assert(sircl_irecv(comms[i], 1, &data, &size, &tag, NULL, NULL, &request) == ncclSuccess);
      assert(request);
    }
  return NULL;
}

int main(int argc, char **argv) {
  assert(argc == 2 || argc == 3);
  setenv("NET_SIRCL_TOPOLOGY", argv[1], 1);
  setenv("NET_SIRCL_POSITION", "0", 1);
  void *ctx = NULL;
  if (argc == 3) {
    assert(sircl_init(&ctx, 0, NULL, NULL, NULL) == ncclSystemError);
    assert(!ctx && !g.refs && !g.ndevs);
    return 0;
  }
  assert(sircl_init(&ctx, 0, NULL, NULL, NULL) == ncclSuccess);
  pid_t child = fork();
  assert(child >= 0);
  if (!child) {
    execl(argv[0], argv[0], argv[1], "owner", (char *)NULL);
    _exit(100);
  }
  int status;
  assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status));
  struct comm *comms[96];
  for (int i = 0; i < 96; ++i) {
    struct comm *c = comms[i] = comm_create(i % 2, 0);
    assert(c);
    struct net_sircl_meta peer;
    meta_fill(c, &peer);
    peer.position = 2 + i % 2;
    assert(comm_ready(c, &peer) == ncclSuccess);
  }
  pthread_t threads[4];
  for (int i = 0; i < 4; ++i) assert(!pthread_create(&threads[i], NULL, issue_receives, comms + 24 * i));
  for (int i = 0; i < 4; ++i) assert(!pthread_join(threads[i], NULL));
  printf("CTS charged bytes before completions: %llu (two function allowances)\n",
         (unsigned long long)admitted);
  fflush(stdout);
  assert(admitted <= 2 * g.relay_windows[0].cap);
  for (int d = 0; d < 2; ++d)
    assert(g.relay_windows[d].max_inflight <= g.relay_windows[d].cap);
  hold = 0;
  /* A proxy may retry isend before it has a request to test. Those retries must
   * reclaim credits belonging to other connections, without GPU/stream work. */
  struct comm *waiting = comm_create(0, 1);
  struct net_sircl_meta waiting_peer;
  meta_fill(waiting, &waiting_peer);
  waiting_peer.position = 3;
  assert(comm_ready(waiting, &waiting_peer) == ncclSuccess);
  for (int tries = 0; tries < 5000; ++tries) {
    void *pending = NULL;
    assert(sircl_isend(waiting, NULL, 0, 0, NULL, NULL, &pending) == ncclSuccess);
    assert(!pending);
    if (comms[0]->reqs[NET_SIRCL_SLOTS - 1].cts_done) break;
    usleep(100);
  }
  assert(comms[0]->reqs[NET_SIRCL_SLOTS - 1].cts_done);
  comm_destroy(waiting);
  /* Any member drains and advances every other member, including those beyond 64. */
  for (int tries = 0; tries < 50000; ++tries) {
    assert(poll_cq(comms[0]) == ncclSuccess);
    assert(poll_cq(comms[1]) == ncclSuccess);
    if (!g.relay_windows[0].inflight && !g.relay_windows[1].inflight) break;
    usleep(100);
  }
  for (int i = 0; i < 96; ++i)
    for (int j = 0; j < NET_SIRCL_SLOTS; ++j) assert(comms[i]->reqs[j].cts_done);
  assert(!g.relay_windows[0].inflight && !g.relay_windows[1].inflight);
  for (int i = 0; i < 96; ++i) comm_destroy(comms[i]);

  /* Hold every CQE: one request must fill several completion chunks without
   * borrowing beyond its function allowance. Release exactly one CQE and
   * require a replacement chunk before the whole request can complete. */
  struct comm *pipeline = comm_create(0, 1);
  struct net_sircl_meta pipeline_peer;
  meta_fill(pipeline, &pipeline_peer);
  pipeline_peer.position = 3;
  assert(comm_ready(pipeline, &pipeline_peer) == ncclSuccess);
  unsigned char *payload = malloc(1u << 20);
  assert(payload);
  memset(payload, 73, 1u << 20);
  void *pipeline_mr = NULL, *pipeline_request = NULL;
  assert(sircl_reg_mr(pipeline, payload, 1u << 20, NCCL_PTR_HOST, &pipeline_mr) == ncclSuccess);
  struct mr *pm = pipeline_mr;
  pipeline->ctrl->cts[0] = (struct net_sircl_cts){.addr = nic_addr(pm, payload), .rkey = pm->ib->rkey,
                                               .size = 1u << 20, .tag = 9};
  pipeline->ctrl->cts_seq[0] = 1;
  hold = 1;
  assert(sircl_isend(pipeline, payload, 1u << 20, 9, pm, NULL, &pipeline_request) == ncclSuccess);
  uint32_t before = pipeline->reqs[0].posted;
  assert(before >= 2 * g.chunk && before < (1u << 20));
  assert(pipeline->inflight <= pipeline->win->cap);
  completion_quota = 1;
  hold = 0;
  for (int tries = 0; completion_quota && tries < 5000; ++tries) {
    assert(poll_cq(pipeline) == ncclSuccess);
    usleep(100);
  }
  assert(!completion_quota && pipeline->reqs[0].posted == before + g.chunk);
  assert(!pipeline->reqs[0].done && pipeline->inflight <= pipeline->win->cap);
  completion_quota = -1;
  int pipeline_done = 0;
  for (int tries = 0; !pipeline_done && tries < 50000; ++tries) {
    assert(sircl_test(pipeline_request, &pipeline_done, NULL) == ncclSuccess);
    usleep(100);
  }
  assert(pipeline_done && !pipeline->inflight);
  assert(sircl_dereg_mr(pipeline, pm) == ncclSuccess);
  comm_destroy(pipeline);
  free(payload);

  /* Negotiate the minimum verbs MTU and request a chunk much larger than the allowance. */
  struct comm *c = comm_create(0, 1);
  struct net_sircl_meta peer;
  meta_fill(c, &peer);
  peer.position = 3;
  peer.mtu = IBV_MTU_256;
  assert(comm_ready(c, &peer) == ncclSuccess);
  /* Exhaust available credit at all verbs MTUs and representative topology shares.
   * The selected charge must fit the remainder, even with another channel's
   * uncompleted writes. Minimum-sized shares must still be able to advance. */
  const uint32_t caps[] = {1024, 10880, 32768, 65536, 131072, 163840, 327680};
  const uint32_t chunks[] = {4096, 16384, 65536, 1u << 30};
  for (unsigned m = 256; m <= 4096; m *= 2)
    for (unsigned ci = 0; ci < sizeof caps / sizeof caps[0]; ++ci)
      for (unsigned gi = 0; gi < sizeof chunks / sizeof chunks[0]; ++gi) {
        c->mtu_bytes = m;
        c->win->cap = caps[ci];
        g.chunk = chunks[gi];
        for (uint64_t used = 0; used <= c->win->cap; used += 64) {
          c->win->inflight = used;
          uint32_t piece = chunk_bytes(c, 1u << 20);
          assert(piece <= g.chunk);
          if (piece) assert(write_charge(c, piece) <= c->win->cap - used);
          if (!used) assert(piece);
        }
      }
  c->win->inflight = 0;
  c->mtu_bytes = 256;
  g.chunk = 1u << 30;
  g.relay_windows[0].cap = 1024;
  assert(chunk_bytes(c, 1u << 30) == 512);
  assert(write_charge(c, 512) == 1024);
  assert(!window_take(c->win, 1025));
  unsigned char input[8192];
  memset(input, 37, sizeof input);
  void *mr = NULL;
  assert(sircl_reg_mr(c, input, sizeof input, NCCL_PTR_HOST, &mr) == ncclSuccess);
  struct mr *m = mr;
  c->ctrl->cts[0] = (struct net_sircl_cts){.addr = nic_addr(m, input), .rkey = m->ib->rkey,
                                       .size = sizeof input, .tag = 7};
  c->ctrl->cts_seq[0] = 1;
  hold = 1;
  admitted = 0;
  void *request = NULL;
  assert(sircl_isend(c, input, sizeof input, 7, mr, NULL, &request) == ncclSuccess && request);
  assert(c->inflight == 1024 && c->reqs[0].posted == 512 && !c->reqs[0].final_posted);
  /* A single data completion returns credit before final request completion.
   * Replay schedules may have no other completed NCCL request to retire. */
  completion_quota = 1;
  hold = 0;
  for (int tries = 0; completion_quota && tries < 5000; ++tries) {
    assert(poll_cq(c) == ncclSuccess);
    usleep(100);
  }
  assert(!completion_quota && c->reqs[0].posted == 1024 && !c->reqs[0].done);
  assert(c->inflight == 1024);
  completion_quota = -1;
  int done = 0;
  for (int tries = 0; !done && tries < 50000; ++tries) {
    assert(sircl_test(request, &done, NULL) == ncclSuccess);
    usleep(100);
  }
  assert(done && !c->inflight && !c->win->inflight);
  assert(sircl_dereg_mr(c, mr) == ncclSuccess);
  comm_destroy(c);

  /* A partial two-write control post fails closed and its credit is never lent again. */
  c = comm_create(1, 0);
  meta_fill(c, &peer);
  peer.position = 2;
  assert(comm_ready(c, &peer) == ncclSuccess);
  fail_after = 2;
  void *data = NULL;
  size_t size = 0;
  int tag = 0;
  request = NULL;
  assert(sircl_irecv(c, 1, &data, &size, &tag, NULL, NULL, &request) == ncclSystemError);
  assert(!request && c->inflight == 640 && c->win->error == ncclSystemError);
  comm_destroy(c);
  assert(g.relay_windows[1].inflight == 640 && g.relay_windows[1].error != ncclSuccess);
  assert(sircl_finalize(ctx) == ncclSuccess);

  /* Abandonment without a posting failure also cannot return unproven relay credit. */
  assert(sircl_init(&ctx, 0, NULL, NULL, NULL) == ncclSuccess);
  c = comm_create(0, 0);
  meta_fill(c, &peer);
  peer.position = 3;
  assert(comm_ready(c, &peer) == ncclSuccess);
  hold = 1;
  assert(sircl_irecv(c, 1, &data, &size, &tag, NULL, NULL, &request) == ncclSuccess);
  assert(c->inflight == 640 && c->win->error == ncclSuccess);
  comm_destroy(c);
  assert(g.relay_windows[0].inflight == 640 && g.relay_windows[0].error == ncclRemoteError);
  assert(sircl_finalize(ctx) == ncclSuccess);
  return 0;
}
