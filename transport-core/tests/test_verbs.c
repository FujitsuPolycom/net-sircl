#define _GNU_SOURCE
#define SCCL_RENAME_VERBS 1
#include <sircl_core/legacy_proxy_names.h>
#include <sircl_core/verbs.h>
#include <sircl_core/shm_verbs.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/mman.h>
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "line %d: %s errno %d\n", __LINE__, #x, errno); exit(1); } } while (0)
int main(void) {
  char name[80], error[256];
  snprintf(name, sizeof name, "/sircl-emu-core-%d", getpid());
  CHECK(!setenv("SIRCL_EMU_FABRIC", name, 1));
  CHECK(!sccl_emu_attach(error, sizeof error));
  uint8_t gid[16] = {0}; gid[15] = 1;
  CHECK(sccl_emu_add_device("core0", gid) >= 0);
  struct ibv_context *ctx = sircl_core_open_named_device("core0"); CHECK(ctx);
  struct ibv_pd *pd = sircl_core_alloc_pd(ctx); CHECK(pd);
  struct ibv_cq *cq = sircl_core_create_cq(ctx, 32); CHECK(cq);
  struct ibv_qp *a = sircl_core_create_rc_qp(pd, cq, 32, 16);
  struct ibv_qp *b = sircl_core_create_rc_qp(pd, cq, 32, 16); CHECK(a && b);
  CHECK(!sircl_core_qp_init(a, 1, IBV_ACCESS_REMOTE_WRITE));
  CHECK(!sircl_core_qp_init(b, 1, IBV_ACCESS_REMOTE_WRITE));
  struct sircl_core_qp_peer peer = {.qpn = b->qp_num, .mtu = IBV_MTU_4096, .port = 1, .hop_limit = 64};
  memcpy(peer.gid, gid, 16);
  CHECK(!sircl_core_qp_rtr(a, &peer)); CHECK(!sircl_core_qp_rts(a, 14, 7));
  peer.qpn = a->qp_num;
  CHECK(!sircl_core_qp_rtr(b, &peer)); CHECK(!sircl_core_qp_rts(b, 14, 7));
  unsigned char *mem = sccl_emu_segment_alloc(8192, error, sizeof error); CHECK(mem);
  struct ibv_mr *mr = sircl_core_reg_mr(pd, mem, 8192, IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE); CHECK(mr);
  for (int i = 0; i < 4097; ++i) mem[i] = (unsigned char)(i % 251);
  CHECK(!sircl_core_post_write(a, (uintptr_t)mem, mr->lkey, 4095, (uintptr_t)(mem + 4097), mr->rkey, IBV_SEND_SIGNALED, 42));
  struct ibv_wc wc; int n = 0;
  for (int i = 0; i < 10000 && !(n = sircl_core_poll_cq(cq, 1, &wc)); ++i) usleep(100);
  CHECK(n == 1 && wc.status == IBV_WC_SUCCESS && wc.wr_id == 42);
  CHECK(!memcmp(mem, mem + 4097, 4095));
  CHECK(!sircl_core_destroy_qp(a)); CHECK(!sircl_core_destroy_qp(b));
  CHECK(!sircl_core_destroy_cq(cq)); CHECK(!sircl_core_dereg_mr(mr));
  sccl_emu_segment_free(mem); CHECK(!sircl_core_dealloc_pd(pd)); CHECK(!sircl_core_close_device(ctx));
  CHECK(!shm_unlink(name));
  puts("core verbs: registered odd-size ordered write exact through emulation provider");
  return 0;
}
