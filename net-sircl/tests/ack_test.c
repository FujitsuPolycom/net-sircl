/* Withheld CQEs saturate ACK debt before forward bytes. Later connections and
 * partial CQ batches must preserve both limits. The provider counts packets
 * independently of the reservation encoded in wr_id. */
#include "../net_sircl.c"
#include <assert.h>

static uint64_t forward, ack, pending_forward, pending_ack;
static struct ibv_wc completions[4096];
static unsigned posted, consumed;
static int quota, fail_after;
static struct comm *active;
int __wrap_sccl_emu_ibv_post_send(struct ibv_qp *qp, struct ibv_send_wr *wr, struct ibv_send_wr **bad) {
  (void)qp; (void)bad;
  if (fail_after && !--fail_after) return -1;
  uint32_t n = wr->sg_list[0].length;
  uint64_t packets = (n + active->mtu_bytes - 1) / active->mtu_bytes;
  uint64_t f = (n + 63u) / 64u * 64u + 256u * packets;
  forward += f; ack += 128u * packets;
  pending_forward += f; pending_ack += 128u * packets;
  assert(forward <= active->win->cap && ack <= active->win->ack_cap);
  if (wr->send_flags & IBV_SEND_SIGNALED) {
    assert(WR_CHARGE(wr->wr_id) == pending_forward);
    assert(WR_ACK(wr->wr_id) == pending_ack);
    completions[posted++] = (struct ibv_wc){.status=IBV_WC_SUCCESS, .wr_id=wr->wr_id};
    pending_forward = pending_ack = 0;
  }
  return 0;
}
int __wrap_sccl_emu_ibv_poll_cq(struct ibv_cq *cq, int n, struct ibv_wc *wc) {
  (void)cq;
  int count = 0;
  while (quota && count < n && consumed < posted) {
    wc[count] = completions[consumed++];
    forward -= WR_CHARGE(wc[count].wr_id);
    ack -= WR_ACK(wc[count].wr_id);
    count++; quota--;
  }
  return count;
}
int main(int argc, char **argv) {
  assert(argc == 2);
  setenv("NET_SIRCL_TOPOLOGY", argv[1], 1);
  setenv("NET_SIRCL_POSITION", "0", 1);
  void *ctx = NULL;
  assert(sircl_init(&ctx, 0, NULL, NULL, NULL) == ncclSuccess);
  active = comm_create(0, 0);
  struct net_sircl_meta peer;
  meta_fill(active, &peer); peer.position = 3;
  assert(comm_ready(active, &peer) == ncclSuccess);
  struct window *w = active->win;
  assert(w->cap == 294912 && w->ack_cap == 49152);
  struct req q = {.c=active};
  for (int i = 0; i < 300; ++i) {
    q.cts_posted = 0;
    assert(recv_progress(active, &q) == ncclSuccess);
    assert(q.cts_posted == (i < 192));
  }
  assert(forward == 122880 && ack == 49152);
  assert(w->ack_inflight == ack && w->inflight == forward);
  /* A later tree/P2P edge gets the same fixed pool; no transient enlargement. */
  struct comm *later = comm_create(0, 1);
  meta_fill(later, &peer); peer.position = 2;
  assert(comm_ready(later, &peer) == ncclSuccess && later->win == w);
  assert(w->cap == 294912 && w->ack_cap == 49152);
  assert(!chunk_bytes(later, 65536));
  quota = 1;
  assert(drain(active) == ncclSuccess);
  assert(w->ack_inflight == 49152 - 256);
  assert(chunk_bytes(later, 65536) == 8192);
  q.cts_posted = 0;
  assert(recv_progress(active, &q) == ncclSuccess && q.cts_posted);
  quota = 4096;
  while (active->inflight) assert(drain(active) == ncclSuccess);
  assert(!w->inflight && !w->ack_inflight && !forward && !ack);
  /* Every supported MTU must fit the remaining packet debt, including tails. */
  for (unsigned mtu=256; mtu<=4096; mtu*=2) {
    active->mtu_bytes = mtu;
    w->ack_inflight = w->ack_cap - 128;
    assert(chunk_bytes(active, 1) == 1);
    assert(chunk_bytes(active, 1u<<20) == (mtu < 4096 ? 0 : 4096));
  }
  w->ack_inflight = 0;
  /* A partial control pair retains both reservations and poisons the pool. */
  fail_after = 2; q.cts_posted = 0;
  assert(recv_progress(active, &q) == ncclSystemError);
  assert(w->error && w->inflight == 640 && w->ack_inflight == 256);
  assert(!window_take_ack(w, 320, 128));
  comm_destroy(later); comm_destroy(active);
  assert(sircl_finalize(ctx) == ncclSuccess);
  puts("ACK saturation, one-CQE reuse, later connection, MTU tails and partial failure: bounded");
  return 0;
}
