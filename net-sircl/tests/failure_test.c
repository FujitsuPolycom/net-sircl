/* Exercise the public submit/test/close seam with a live but silent peer, vanished peers, and a
 * provider that loses all forwarded writes/CQEs.
 *
 *   failure_test <topology> <mode> <peer position>
 *
 *   send          the peer never posts its receive and NET_SIRCL_PEER_WAIT_TIMEOUT_MS is set: the send
 *                 fails at that bound
 *   recv          the clear-to-send post never completes: the receive fails at
 *                 NET_SIRCL_REQUEST_TIMEOUT_MS
 *   flight        clear-to-send arrived and the payload writes never complete: the send fails at
 *                 NET_SIRCL_REQUEST_TIMEOUT_MS
 *   lost          the peer closed its connection: the request fails at once
 *   patient-send  the peer never posts its receive and no peer-wait bound is set: the send keeps
 *                 waiting well past NET_SIRCL_REQUEST_TIMEOUT_MS, as NCCL itself would, and fails only
 *                 when the peer closes its connection
 *   patient-recv  the clear-to-send post completes and the peer never sends: the receive keeps
 *                 waiting likewise
 *
 * Every failure is sticky, shuts this side of the connection down so the peer learns of it, and
 * leaves a comm that closes promptly. */
#include "../net_sircl.c"
#include <assert.h>

static int swallow = 1;

int __real_sccl_emu_ibv_post_send(struct ibv_qp *qp, struct ibv_send_wr *wr, struct ibv_send_wr **bad);
int __wrap_sccl_emu_ibv_post_send(struct ibv_qp *qp, struct ibv_send_wr *wr, struct ibv_send_wr **bad) {
  if (!swallow) return __real_sccl_emu_ibv_post_send(qp, wr, bad);
  return 0; /* Accepted by provider, no delivery or completion: lost relay path. */
}

#define TRANSPORT_MS 100
#define PATIENCE_MS (6 * TRANSPORT_MS)

int main(int argc, char **argv) {
  assert(argc == 4);
  const char *mode = argv[2];
  int patient = !strncmp(mode, "patient-", 8);
  setenv("NET_SIRCL_TOPOLOGY", argv[1], 1);
  setenv("NET_SIRCL_POSITION", "0", 1);
  setenv("NET_SIRCL_REQUEST_TIMEOUT_MS", "100", 1);
  /* Only `send` bounds the wait for the peer application; the other modes keep the default (none). */
  if (!strcmp(mode, "send")) setenv("NET_SIRCL_PEER_WAIT_TIMEOUT_MS", "100", 1);
  int sending = !strcmp(mode, "send") || !strcmp(mode, "flight") || !strcmp(mode, "lost") ||
                !strcmp(mode, "patient-send");
  int peer = atoi(argv[3]);
  void *ctx = NULL;
  assert(sircl_init(&ctx, 0, NULL, NULL, NULL) == ncclSuccess);
  assert(g.request_timeout_ms == TRANSPORT_MS);
  assert(g.peer_wait_timeout_ms == (!strcmp(mode, "send") ? 100 : 0));
  struct comm *c = comm_create(0, sending);
  assert(c);
  struct net_sircl_meta remote;
  meta_fill(c, &remote); remote.position = peer;
  assert(comm_ready(c, &remote) == ncclSuccess);
  int pair[2];
  assert(!socketpair(AF_UNIX, SOCK_STREAM, 0, pair));
  c->fd = pair[0];
  assert(!set_nonblocking(c->fd));
  if (!strcmp(mode, "lost")) { close(pair[1]); pair[1] = -1; }
  /* The receive's clear-to-send is delivered (the comm is connected to itself) and completes. */
  if (!strcmp(mode, "patient-recv")) swallow = 0;
  char data[1024] = {0};
  void *request = NULL, *buf = data, *mr = NULL;
  size_t size = sizeof data;
  int tag = 0;
  if (!strcmp(mode, "flight")) {
    c->ctrl->cts_seq[0] = 1;
    c->ctrl->cts[0].size = sizeof data;
  }
  int64_t start = now_ms();
  ncclResult_t result = ncclSuccess;
  int done = 0, closed_at_peer = 0;
  int64_t limit = patient ? PATIENCE_MS + 2000 : 2000;
  do {
    if (request) result = sircl_test(request, &done, NULL);
    else if (sending) result = sircl_isend(c, data, sizeof data, 0, NULL, NULL, &request);
    else result = sircl_irecv(c, 1, &buf, &size, &tag, &mr, NULL, &request);
    assert(!done);
    if (patient && !closed_at_peer && now_ms() - start >= PATIENCE_MS) {
      /* Six transport deadlines have passed with the peer alive and silent: still waiting. */
      assert(result == ncclSuccess && !c->error);
      if (sending) assert(!request);
      else assert(request && ((struct req *)request)->cts_done);
      close(pair[1]); pair[1] = -1; closed_at_peer = 1;
    }
  } while (result == ncclSuccess && now_ms() - start < limit);
  assert(result == ncclRemoteError);
  if (patient) {
    assert(closed_at_peer);
    assert(now_ms() - start < PATIENCE_MS + 1500);
  } else {
    assert(now_ms() - start < 1500);
    if (strcmp(mode, "lost")) assert(now_ms() - start >= TRANSPORT_MS);
  }
  /* Failure is sticky even on a direct comm with no shared window. */
  void *again = (void *)1;
  assert(sircl_isend(c, data, 1, 0, NULL, NULL, &again) == ncclRemoteError && !again);
  if (c->win) assert(c->win->error == ncclRemoteError);
  /* The failed comm hung up: a peer still holding the connection reads end of stream. */
  assert(c->hung_up);
  if (pair[1] >= 0) {
    char b;
    assert(recv(pair[1], &b, 1, MSG_DONTWAIT) == 0);
  }
  int64_t closing = now_ms();
  comm_destroy(c);
  if (pair[1] >= 0) close(pair[1]);
  assert(sircl_finalize(ctx) == ncclSuccess);
  assert(now_ms() - closing < 500);
  printf("failure %s peer %d: %s\n", mode, peer,
         patient ? "waited for the silent peer past the transport deadline, failed when it left"
                 : "bounded error and close");
  return 0;
}
