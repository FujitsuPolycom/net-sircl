/* Public verbs lifecycle contracts with barriers at worker and membership transitions. */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#include <sircl_core/shm_verbs.h>
#define SCCL_RENAME_VERBS
#include <sircl_core/legacy_proxy_names.h>
#include <sircl_core/verbs.h>

static pthread_mutex_t barrier_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t barrier = PTHREAD_COND_INITIALIZER;
enum { NORMAL, MEMBERSHIP, WORK };
static int mode, retiring, creating, executing, released;

void sircl_emu_test_hook(const char *event) {
  pthread_mutex_lock(&barrier_lock);
  if (mode == MEMBERSHIP && !strcmp(event, "retire_locked")) {
    retiring = 1;
    pthread_cond_broadcast(&barrier);
    while (!released) pthread_cond_wait(&barrier, &barrier_lock);
  } else if (mode == MEMBERSHIP && !strcmp(event, "create_waiting")) {
    creating = 1;
    pthread_cond_broadcast(&barrier);
  } else if (mode == WORK && !strcmp(event, "executing")) {
    executing = 1;
    pthread_cond_broadcast(&barrier);
    while (!released) pthread_cond_wait(&barrier, &barrier_lock);
  } else if (mode == WORK && !strcmp(event, "retire_waiting")) {
    retiring = 1;
    pthread_cond_broadcast(&barrier);
  }
  pthread_mutex_unlock(&barrier_lock);
}

static void wait_event(int *event) {
  pthread_mutex_lock(&barrier_lock);
  while (!*event) pthread_cond_wait(&barrier, &barrier_lock);
  pthread_mutex_unlock(&barrier_lock);
}

static void release_barrier(void) {
  pthread_mutex_lock(&barrier_lock);
  released = 1;
  pthread_cond_broadcast(&barrier);
  pthread_mutex_unlock(&barrier_lock);
}

static void io(int fd, void *data, size_t size, int writing) {
  size_t offset = 0;
  while (offset < size) {
    ssize_t n = writing ? write(fd, (char *)data + offset, size - offset)
                        : read(fd, (char *)data + offset, size - offset);
    assert(n > 0);
    offset += (size_t)n;
  }
}

struct endpoint { struct ibv_context *ctx; struct ibv_pd *pd; struct ibv_cq *cq; struct ibv_qp *qp; uint8_t gid[16]; };
static struct endpoint endpoint(unsigned id) {
  struct endpoint e = {0};
  e.gid[15] = (uint8_t)id;
  char error[256], name[32];
  assert(!sccl_emu_attach(error, sizeof error));
  snprintf(name, sizeof name, "lifetime%u", id);
  assert(sccl_emu_add_device(name, e.gid) >= 0);
  e.ctx = sircl_core_open_named_device(name); assert(e.ctx);
  e.pd = sircl_core_alloc_pd(e.ctx); assert(e.pd);
  e.cq = sircl_core_create_cq(e.ctx, 32); assert(e.cq);
  e.qp = sircl_core_create_rc_qp(e.pd, e.cq, 32, 16); assert(e.qp);
  return e;
}

static void connect_qp(struct endpoint *e, uint32_t peer, const uint8_t *gid) {
  assert(!sircl_core_qp_init(e->qp, 1, IBV_ACCESS_REMOTE_WRITE));
  struct sircl_core_qp_peer remote = {.qpn = peer, .mtu = IBV_MTU_4096, .port = 1, .hop_limit = 64};
  memcpy(remote.gid, gid, 16);
  assert(!sircl_core_qp_rtr(e->qp, &remote));
  assert(!sircl_core_qp_rts(e->qp, 14, 7));
}

static void completion(struct endpoint *e) {
  struct ibv_wc wc;
  int n = 0;
  for (int i = 0; i < 10000 && !n; ++i) {
    n = sircl_core_poll_cq(e->cq, 1, &wc);
    if (!n) usleep(100);
  }
  assert(n == 1 && wc.status == IBV_WC_SUCCESS);
}

static void cleanup(struct endpoint *e) {
  assert(!sircl_core_destroy_qp(e->qp));
  assert(!sircl_core_destroy_cq(e->cq));
  assert(!sircl_core_dealloc_pd(e->pd));
  assert(!sircl_core_close_device(e->ctx));
}

struct region { uint64_t address; uint32_t key; };
static void mapping_lifetimes(void) {
  int forward[2], back[2];
  assert(!pipe(forward) && !pipe(back));
  pid_t child = fork(); assert(child >= 0);
  if (!child) {
    close(forward[1]); close(back[0]);
    struct endpoint e = endpoint(2);
    io(back[1], &e.qp->qp_num, sizeof(uint32_t), 1);
    uint32_t peer; io(forward[0], &peer, sizeof peer, 0);
    uint8_t gid[16] = {0}; gid[15] = 1;
    connect_qp(&e, peer, gid);
    for (unsigned i = 0; i < 530; ++i) {
      char error[256];
      uint32_t *mem = sccl_emu_segment_alloc(4096, error, sizeof error); assert(mem);
      struct ibv_mr *mr = sircl_core_reg_mr(e.pd, mem, 4096, IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE); assert(mr);
      struct region r = {(uintptr_t)mem, mr->rkey};
      io(back[1], &r, sizeof r, 1);
      uint32_t expected; io(forward[0], &expected, sizeof expected, 0);
      assert(*mem == expected);
      assert(!sircl_core_dereg_mr(mr));
      sccl_emu_segment_free(mem);
    }
    cleanup(&e);
    _exit(0);
  }
  close(forward[0]); close(back[1]);
  struct endpoint e = endpoint(1);
  uint32_t peer; io(back[0], &peer, sizeof peer, 0);
  io(forward[1], &e.qp->qp_num, sizeof(uint32_t), 1);
  uint8_t gid[16] = {0}; gid[15] = 2;
  connect_qp(&e, peer, gid);
  for (unsigned i = 0; i < 530; ++i) {
    struct region r; io(back[0], &r, sizeof r, 0);
    uint32_t value = i + 1;
    assert(!sircl_core_post_write(e.qp, (uintptr_t)&value, 0, 4, r.address, r.key,
                                 IBV_SEND_INLINE | IBV_SEND_SIGNALED, i));
    completion(&e);
    io(forward[1], &value, sizeof value, 1);
  }
  char error[256];
  void *owned = sccl_emu_segment_alloc(4096, error, sizeof error); assert(owned);
  sccl_emu_segment_free(owned);
  cleanup(&e);
  int status; assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status));
  close(forward[1]); close(back[0]);
}

static void *retire(void *arg) { assert(!sircl_core_destroy_qp(arg)); return NULL; }
static void *create(void *arg) {
  struct endpoint *e = arg;
  e->qp = sircl_core_create_rc_qp(e->pd, e->cq, 32, 16); assert(e->qp);
  return NULL;
}

static void worker_lifetime(void) {
  struct endpoint e = endpoint(3);
  struct ibv_qp *original = e.qp;
  pthread_t destroying, creator;
  mode = MEMBERSHIP;
  assert(!pthread_create(&destroying, NULL, retire, original));
  wait_event(&retiring);
  assert(!pthread_create(&creator, NULL, create, &e));
  wait_event(&creating);
  release_barrier();
  assert(!pthread_join(destroying, NULL) && !pthread_join(creator, NULL));
  mode = NORMAL;
  connect_qp(&e, e.qp->qp_num, e.gid);
  char error[256];
  uint32_t *mem = sccl_emu_segment_alloc(4096, error, sizeof error); assert(mem);
  struct ibv_mr *mr = sircl_core_reg_mr(e.pd, mem, 4096, IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE); assert(mr);
  mem[0] = 73;
  assert(!sircl_core_post_write(e.qp, (uintptr_t)mem, mr->lkey, 4, (uintptr_t)(mem + 1), mr->rkey, IBV_SEND_SIGNALED, 1));
  completion(&e); assert(mem[1] == 73);
  struct ibv_qp *keeper = sircl_core_create_rc_qp(e.pd, e.cq, 32, 16); assert(keeper);
  pthread_mutex_lock(&barrier_lock);
  mode = WORK; retiring = executing = released = 0;
  pthread_mutex_unlock(&barrier_lock);
  assert(!sircl_core_post_write(e.qp, (uintptr_t)mem, mr->lkey, 4, (uintptr_t)(mem + 1), mr->rkey, IBV_SEND_SIGNALED, 2));
  wait_event(&executing);
  assert(!pthread_create(&destroying, NULL, retire, e.qp));
  wait_event(&retiring);
  assert(pthread_tryjoin_np(destroying, NULL) == EBUSY);
  release_barrier();
  assert(!pthread_join(destroying, NULL));
  completion(&e);
  mode = NORMAL;
  e.qp = keeper;
  assert(!sircl_core_dereg_mr(mr)); sccl_emu_segment_free(mem);
  cleanup(&e);
}

int main(void) {
  char fabric[80]; snprintf(fabric, sizeof fabric, "/sircl-lifetime-%d", (int)getpid());
  assert(!setenv("SIRCL_EMU_FABRIC", fabric, 1));
  mapping_lifetimes();
  worker_lifetime();
  assert(!shm_unlink(fabric));
  puts("emulator: 530 remote lifetimes, serialized membership and work retirement exact");
  return 0;
}
