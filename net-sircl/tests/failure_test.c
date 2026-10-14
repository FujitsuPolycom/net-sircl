/* Exercise the public submit/test/close seam with a live but silent peer,
 * vanished peers, and a provider that loses all forwarded writes/CQEs. */
#include "../net_sircl.c"
#include <assert.h>

int __wrap_sccl_emu_ibv_post_send(struct ibv_qp *qp, struct ibv_send_wr *wr, struct ibv_send_wr **bad) {
  (void)qp; (void)wr; (void)bad;
  return 0; /* Accepted by provider, no delivery or completion: lost relay path. */
}

int main(int argc, char **argv) {
  assert(argc == 4);
  setenv("NET_SIRCL_TOPOLOGY", argv[1], 1);
  setenv("NET_SIRCL_POSITION", "0", 1);
  setenv("NET_SIRCL_REQUEST_TIMEOUT_MS", "100", 1);
  int sending = strcmp(argv[2], "recv") != 0;
  int peer = atoi(argv[3]);
  void *ctx = NULL;
  assert(sircl_init(&ctx, 0, NULL, NULL, NULL) == ncclSuccess);
  struct comm *c = comm_create(0, sending);
  assert(c);
  struct net_sircl_meta remote;
  meta_fill(c, &remote); remote.position = peer;
  assert(comm_ready(c, &remote) == ncclSuccess);
  int pair[2];
  assert(!socketpair(AF_UNIX, SOCK_STREAM, 0, pair));
  c->fd = pair[0];
  assert(!set_nonblocking(c->fd));
  if (!strcmp(argv[2], "lost")) { close(pair[1]); pair[1] = -1; }
  char data[1024] = {0};
  void *request = NULL, *buf = data, *mr = NULL;
  size_t size = sizeof data;
  int tag = 0;
  if (!strcmp(argv[2], "flight")) {
    c->ctrl->cts_seq[0] = 1;
    c->ctrl->cts[0].size = sizeof data;
  }
  int64_t start = now_ms();
  ncclResult_t result = ncclSuccess;
  int done = 0;
  do {
    if (request) result = sircl_test(request, &done, NULL);
    else if (sending) result = sircl_isend(c, data, sizeof data, 0, NULL, NULL, &request);
    else result = sircl_irecv(c, 1, &buf, &size, &tag, &mr, NULL, &request);
    assert(!done);
  } while (result == ncclSuccess && now_ms() - start < 2000);
  assert(result == ncclRemoteError);
  assert(now_ms() - start < 1500);
  if (strcmp(argv[2], "lost")) assert(now_ms() - start >= 100);
  /* Failure is sticky even on a direct comm with no shared window. */
  void *again = (void *)1;
  assert(sircl_isend(c, data, 1, 0, NULL, NULL, &again) == ncclRemoteError && !again);
  if (c->win) assert(c->win->error == ncclRemoteError);
  int64_t closing = now_ms();
  comm_destroy(c);
  if (pair[1] >= 0) close(pair[1]);
  assert(sircl_finalize(ctx) == ncclSuccess);
  assert(now_ms() - closing < 500);
  printf("failure %s peer %d: bounded error and close\n", argv[2], peer);
  return 0;
}
