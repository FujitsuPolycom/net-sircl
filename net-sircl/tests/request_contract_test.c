/* Private protocol seam: registration ownership and receive capacity under provider faults. */
#include "../net_sircl.c"
#include <assert.h>

int main(int argc, char **argv) {
  assert(argc == 2);
  assert(!setenv("NET_SIRCL_TOPOLOGY", argv[1], 1));
  assert(!setenv("NET_SIRCL_POSITION", "0", 1));
  void *context = NULL;
  assert(sircl_init(&context, 0, NULL, NULL, NULL) == ncclSuccess);
  struct comm *c = comm_create(0, 0); assert(c);
  unsigned char data[16] = {0};
  void *registration = NULL;
  assert(sircl_reg_mr(c, data, sizeof data, NCCL_PTR_HOST, &registration) == ncclSuccess);
  struct mr *mr = registration;
  assert(mr->ib && mr->shadow);
  assert(!setenv("SIRCL_EMU_FAIL_DEREG", "1", 1));
  assert(sircl_dereg_mr(c, registration) == ncclSystemError);
  assert(mr->ib && mr->shadow && mr->size == sizeof data);
  assert(!unsetenv("SIRCL_EMU_FAIL_DEREG"));
  assert(sircl_dereg_mr(c, registration) == ncclSuccess);

  struct req *q = &c->reqs[0];
  q->used = 1; q->c = c; q->total = sizeof data; q->cts_done = 1;
  q->user = data; q->shadow = data; q->seq = 0; q->slot = 0;
  c->ctrl->done_seq[0] = 1;
  c->ctrl->done_size[0] = sizeof data + 1;
  int done = -1, size = -1;
  assert(sircl_test(q, &done, &size) == ncclRemoteError);
  assert(done == 0 && size == -1 && q->used);
  assert(c->error == ncclRemoteError);
  for (unsigned i = 0; i < sizeof data; ++i) assert(data[i] == 0);
  assert(!setenv("SIRCL_EMU_FAIL_DEREG", "1", 1));
  assert(sircl_close_recv(c) == ncclSystemError);
  assert(!c->qp && c->ctrl_mr && c->ctrl);
  assert(!unsetenv("SIRCL_EMU_FAIL_DEREG"));
  assert(sircl_close_recv(c) == ncclSuccess);
  assert(sircl_finalize(context) == ncclSuccess);
  puts("request contracts: failed registration release retains ownership; oversized completion rejected");
  return 0;
}
