/* Deterministic provider-cost model: real plugin scheduling with completion waves.
 * Counts work, not hardware time. Payload correctness uses the separate stand-in tests. */
#include "../net_sircl.c"
#include <assert.h>

#define COMMS 64
#ifndef REQUEST_BYTES
#define REQUEST_BYTES (1u << 20)
#endif
#define BYTES REQUEST_BYTES
struct provider {
  struct comm *c;
  struct ibv_wc wc[2048];
  unsigned posted, ready, consumed;
  uint64_t unsignaled;
};
static struct provider providers[COMMS];
static uint64_t polls, writes, completions, outstanding[2], peak[2];
static int check_refill, released_first, reposted_first;
static int check_oldest, first_neighbor = -1;

int __wrap_sccl_emu_ibv_post_send(struct ibv_qp *qp, struct ibv_send_wr *wr, struct ibv_send_wr **bad) {
  (void)bad;
  struct provider *p = NULL;
  for (int i = 0; i < COMMS; ++i) if (providers[i].c && providers[i].c->qp == qp) p = &providers[i];
  assert(p);
  if (check_oldest && p->c->dev == 0 && p != &providers[0] && first_neighbor < 0)
    first_neighbor = (int)(p - providers);
  if (check_refill && released_first) reposted_first = 1;
  for (; wr; wr = wr->next) {
    uint32_t bytes = wr->num_sge ? wr->sg_list[0].length : 0;
    uint64_t charge = write_charge(p->c, bytes);
    int d = p->c->dev;
    outstanding[d] += charge;
    if (outstanding[d] > peak[d]) peak[d] = outstanding[d];
    assert(outstanding[d] <= p->c->win->cap);
    p->unsignaled += charge;
    writes++;
    if (wr->send_flags & IBV_SEND_SIGNALED) {
      assert(WR_CHARGE(wr->wr_id) == p->unsignaled);
      assert(p->posted - p->consumed < 2048);
      p->wc[p->posted++ % 2048] = (struct ibv_wc){.status = IBV_WC_SUCCESS, .wr_id = wr->wr_id};
      p->unsignaled = 0;
    }
  }
  return 0;
}

int __wrap_sccl_emu_ibv_poll_cq(struct ibv_cq *cq, int n, struct ibv_wc *wc) {
  polls++;
  for (int i = 0; i < COMMS; ++i) {
    struct provider *p = &providers[i];
    if (!p->c || p->c->cq != cq) continue;
    if (check_refill && p == &providers[2]) assert(reposted_first);
    int got = 0;
    while (got < n && p->consumed != p->ready) {
      wc[got] = p->wc[p->consumed++ % 2048];
      outstanding[p->c->dev] -= WR_CHARGE(wc[got].wr_id);
      got++;
    }
    completions += got;
    if (check_refill && p == &providers[0] && got) released_first = 1;
    return got;
  }
  assert(0);
  return -1;
}

int main(int argc, char **argv) {
  assert(argc == 3);
  setenv("NET_SIRCL_TOPOLOGY", argv[1], 1);
  setenv("NET_SIRCL_POSITION", "0", 1);
  void *ctx = NULL;
  assert(sircl_init(&ctx, 0, NULL, NULL, NULL) == ncclSuccess);
  for (int i = 0; i < COMMS; ++i) {
    struct comm *c = providers[i].c = comm_create(i % 2, 1);
    assert(c);
    struct net_sircl_meta peer;
    meta_fill(c, &peer);
    peer.position = 2 + i % 2;
    assert(comm_ready(c, &peer) == ncclSuccess);
  }
  if (!strcmp(argv[2], "batch")) {
    struct comm *c = providers[0].c;
    uint64_t charge = write_charge(c, 64);
    for (int i = 0; i < 96; i++) {
      assert(window_take(c->win, charge));
      assert(!post_write(c, 0, 0, 64, 0, 0, 0, 1, WR_ID(WR_DATA, charge, 0)));
      c->inflight += charge;
    }
    providers[0].ready = providers[0].posted;
    for (int i = 1; i <= 3; i++) {
      assert(drain(c) == ncclSuccess);
      assert(polls == (uint64_t)i && completions == (uint64_t)i * 32);
      assert(c->win->inflight == (96u - 32u * i) * charge);
    }
    assert(!c->inflight && !outstanding[0]);
    for (int i = 0; i < COMMS; i++) comm_destroy(providers[i].c);
    assert(sircl_finalize(ctx) == ncclSuccess);
    puts("three bounded CQ batches: exact credit return, no trailing empty polls");
    return 0;
  }
  /* Real retry call pattern with no CTS and no outstanding WRs. */
  for (int i = 0; i < COMMS; ++i) {
    void *q = NULL;
    assert(sircl_isend(providers[i].c, NULL, 0, 0, NULL, NULL, &q) == ncclSuccess && !q);
  }
  printf("idle provider polls: %llu\n", (unsigned long long)polls);
  fflush(stdout);
  if (!strcmp(argv[2], "idle")) assert(polls == 0);
  polls = 0;

  /* A shared registration is unnecessary: the deterministic provider never dereferences payloads. */
  unsigned char payload = 0;
  void *requests[COMMS];
  for (int i = 0; i < COMMS; ++i) {
    struct comm *c = providers[i].c;
    c->ctrl->cts[0] = (struct net_sircl_cts){.size = BYTES};
    c->ctrl->cts_seq[0] = 1;
    assert(sircl_isend(c, &payload, BYTES, 0, NULL, NULL, &requests[i]) == ncclSuccess && requests[i]);
  }
  uint32_t filled = providers[0].c->reqs[0].posted;
  printf("initial payload per function: %u, charged: %llu\n", filled, (unsigned long long)outstanding[0]);
  fflush(stdout);
  if (!strcmp(argv[2], "fill")) assert(filled > 9u * 16384u);
  if (!strcmp(argv[2], "route")) {
    assert(g.route_derived && providers[0].c->win->cap == 294912);
    assert(filled == 277504 && outstanding[0] == 294912);
  }

  if (!strcmp(argv[2], "refill")) {
    /* Give a second QP one completed chunk's room, with the first request
     * still waiting. Both QPs now have real outstanding provider completions. */
    struct comm *first = providers[0].c, *second = providers[2].c;
    providers[0].ready = 1;
    assert(drain(first) == ncclSuccess);
    assert(advance(second) == ncclSuccess && second->inflight);
    struct comm *members = NULL;
    for (int i = COMMS - 2; i >= 0; i -= 2) {
      providers[i].c->next = members;
      members = providers[i].c;
    }
    first->win->members = members;
    for (int i = 0; i < COMMS; ++i) providers[i].ready = providers[i].posted;
    check_refill = 1;
    assert(poll_cq(first) == ncclSuccess && released_first && reposted_first);
    check_refill = 0;
    puts("replacement posted before the next active QP is polled");
  }

  if (!strcmp(argv[2], "fair")) {
    /* Finish the first request while visiting younger channels before the
     * oldest waiting neighbor. CQ list order must not determine admission. */
    check_oldest = 1;
    for (int turn = 0; turn < 100 && first_neighbor < 0; ++turn) {
      struct comm *members = NULL;
      for (int i = 2; i < COMMS; i += 2) {
        providers[i].c->next = members;
        members = providers[i].c;
      }
      providers[0].c->next = members;
      providers[0].c->win->members = providers[0].c;
      providers[0].ready = providers[0].posted;
      assert(poll_cq(providers[0].c) == ncclSuccess);
    }
    check_oldest = 0;
    assert(first_neighbor == 2);
    assert(providers[2].posted > 0);
    puts("oldest waiting request posts before younger channels despite reverse CQ order");
  }

  unsigned rounds;
  for (rounds = 0; rounds < 100000; ++rounds) {
    for (int i = 0; i < COMMS; ++i) providers[i].ready = providers[i].posted;
    int remaining = 0;
    for (int i = 0; i < COMMS; ++i) if (requests[i]) {
      int done = 0;
      assert(sircl_test(requests[i], &done, NULL) == ncclSuccess);
      if (done) requests[i] = NULL;
      else remaining++;
    }
    if (!remaining) break;
  }
  assert(rounds < 100000);
  assert(!outstanding[0] && !outstanding[1]);
  printf("%u MiB, 64 channels, two functions: rounds=%u writes=%llu CQEs=%llu polls=%llu peak=%llu/%llu\n",
         (unsigned)((uint64_t)COMMS * BYTES >> 20), rounds + 1, (unsigned long long)writes, (unsigned long long)completions,
         (unsigned long long)polls, (unsigned long long)peak[0], (unsigned long long)peak[1]);
  fflush(stdout);
  if (!strcmp(argv[2], "bulk")) {
    assert(writes < 2048);
    /* A nonempty batch must not force a trailing empty provider poll. */
    assert(polls < 13300);
  }
  for (int i = 0; i < COMMS; ++i) comm_destroy(providers[i].c);
  assert(sircl_finalize(ctx) == ncclSuccess);
  return 0;
}
