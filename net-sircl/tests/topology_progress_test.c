/* Concurrent all-to-all over both lanes, through real plugin entry points.
 * The stand-in transports writes directly; this tests host progress, not a NIC queue. */
#include "../net_sircl.c"
#include <assert.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/wait.h>

#define RANKS 8
#define LANES 2
#define PIPE 4
#define BYTES 262144
struct exchange {
  char handles[RANKS][RANKS][LANES][NCCL_NET_HANDLE_MAXSIZE];
  int ready[RANKS], finished[RANKS];
};
struct edge {
  void *listen, *send, *recv, *smr, *rmr;
  unsigned char *src, *dst;
  void *sq[PIPE], *rq[PIPE];
  int sent[PIPE], received[PIPE];
};

static void barrier(int *flags, int rank, int n) {
  __atomic_store_n(&flags[rank], 1, __ATOMIC_RELEASE);
  for (int p = 0; p < n; ++p)
    while (!__atomic_load_n(&flags[p], __ATOMIC_ACQUIRE)) usleep(100);
}

static void run_rank(struct exchange *x, int rank, int n) {
  alarm(45);
  char pos[16];
  snprintf(pos, sizeof pos, "%d", rank);
  setenv("NET_SIRCL_POSITION", pos, 1);
  setenv("NET_SIRCL_STANDIN_DEVICES", "4", 1);
  void *ctx = NULL;
  assert(sircl_init(&ctx, 0, NULL, NULL, NULL) == ncclSuccess);
  struct edge edges[RANKS][LANES] = {0};
  for (int p = 0; p < n; ++p) if (p != rank)
    for (int l = 0; l < LANES; ++l) {
      struct edge *e = &edges[p][l];
      assert(sircl_listen(ctx, g.routes[p].dev[l], x->handles[rank][p][l], &e->listen) == ncclSuccess);
    }
  barrier(x->ready, rank, n);
  int missing;
  do {
    missing = 0;
    for (int p = 0; p < n; ++p) if (p != rank)
      for (int l = 0; l < LANES; ++l) {
        struct edge *e = &edges[p][l];
        if (!e->send)
          assert(sircl_connect(ctx, g.routes[p].dev[l], x->handles[p][rank][l], &e->send, NULL) == ncclSuccess);
        if (!e->recv) assert(sircl_accept(e->listen, &e->recv, NULL) == ncclSuccess);
        missing += !e->send + !e->recv;
      }
  } while (missing);
  for (int p = 0; p < n; ++p) if (p != rank)
    for (int l = 0; l < LANES; ++l) {
      struct edge *e = &edges[p][l];
      e->src = malloc(PIPE * BYTES);
      e->dst = calloc(PIPE, BYTES);
      assert(e->src && e->dst);
      memset(e->src, 1 + rank * LANES + l, PIPE * BYTES);
      assert(sircl_reg_mr(e->send, e->src, PIPE * BYTES, NCCL_PTR_HOST, &e->smr) == ncclSuccess);
      assert(sircl_reg_mr(e->recv, e->dst, PIPE * BYTES, NCCL_PTR_HOST, &e->rmr) == ncclSuccess);
      for (int k = 0; k < PIPE; ++k) {
        void *ptr = e->dst + k * BYTES;
        size_t size = BYTES;
        assert(sircl_irecv(e->recv, 1, &ptr, &size, &k, &e->rmr, NULL, &e->rq[k]) == ncclSuccess);
        assert(e->rq[k]);
      }
    }
  do {
    missing = 0;
    /* Opposite iteration order on alternate ranks stresses asymmetric progress. */
    for (int i = 0; i < n; ++i) {
      int p = rank % 2 ? n - i - 1 : i;
      if (p == rank) continue;
      for (int l = 0; l < LANES; ++l) {
        struct edge *e = &edges[p][l];
        for (int k = 0; k < PIPE; ++k) {
          if (!e->sq[k]) {
            assert(sircl_isend(e->send, e->src + k * BYTES, BYTES, k, e->smr, NULL, &e->sq[k]) == ncclSuccess);
            if (!e->sq[k]) break;
          }
        }
        for (int k = 0; k < PIPE; ++k) {
          if (e->sq[k] && !e->sent[k]) assert(sircl_test(e->sq[k], &e->sent[k], NULL) == ncclSuccess);
          if (!e->received[k]) assert(sircl_test(e->rq[k], &e->received[k], NULL) == ncclSuccess);
          missing += !e->sent[k] + !e->received[k];
        }
      }
    }
  } while (missing);
  for (int d = 0; d < g.ndevs; ++d) {
    assert(!g.relay_windows[d].inflight);
    assert(!g.relay_windows[d].ack_inflight);
    assert(g.relay_windows[d].max_inflight <= g.relay_windows[d].cap);
  }
  barrier(x->finished, rank, n);
  for (int p = 0; p < n; ++p) if (p != rank)
    for (int l = 0; l < LANES; ++l) {
      struct edge *e = &edges[p][l];
      for (int j = 0; j < PIPE * BYTES; ++j) assert(e->dst[j] == 1 + p * LANES + l);
      assert(sircl_dereg_mr(e->send, e->smr) == ncclSuccess);
      assert(sircl_dereg_mr(e->recv, e->rmr) == ncclSuccess);
      comm_destroy(e->send);
      comm_destroy(e->recv);
      assert(sircl_close_listen(e->listen) == ncclSuccess);
      free(e->src);
      free(e->dst);
    }
  assert(sircl_finalize(ctx) == ncclSuccess);
}

int main(int argc, char **argv) {
  assert(argc == 2);
  int n = atoi(argv[1]);
  assert(n >= 2 && n <= RANKS);
  struct exchange *x = mmap(NULL, sizeof *x, PROT_READ | PROT_WRITE,
                            MAP_SHARED | MAP_ANONYMOUS, -1, 0);
  assert(x != MAP_FAILED);
  pid_t children[RANKS];
  for (int rank = 0; rank < n; ++rank) {
    children[rank] = fork();
    assert(children[rank] >= 0);
    if (!children[rank]) { run_rank(x, rank, n); _exit(0); }
  }
  int failed = 0;
  for (int rank = 0; rank < n; ++rank) {
    int status;
    assert(waitpid(children[rank], &status, 0) == children[rank]);
    failed |= !WIFEXITED(status) || WEXITSTATUS(status);
  }
  assert(!failed);
  munmap(x, sizeof *x);
  printf("%d ranks: both lanes, all pairs, %d pipelined messages exact and bounded\n", n, PIPE);
  return 0;
}
