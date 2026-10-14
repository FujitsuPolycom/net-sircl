#include <sircl_core/core.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "line %d: %s\n", __LINE__, #x); exit(1); } } while (0)
static uint64_t flight, high;
static void *worker(void *unused) {
  (void)unused;
  for (int i = 0; i < 10000; ++i) {
    while (!sircl_core_window_claim(&flight, &high, 64, 8, 0)) sched_yield();
    CHECK(__atomic_load_n(&flight, __ATOMIC_ACQUIRE) <= 64);
    sircl_core_window_release(&flight, 8);
  }
  return NULL;
}
static int poller(void *ctx, unsigned budget) { (*(unsigned *)ctx)++; return (int)budget; }
static int broken(void *ctx, unsigned budget) { (void)ctx; return (int)budget + 1; }
int main(void) {
  CHECK(sircl_core_abi() == 1 && !strcmp(sircl_core_version(), "0.1.1"));
  CHECK(sircl_core_window_room(64, 64) == 0 && sircl_core_window_room(64, 65) == 0);
  CHECK(sircl_core_window_room(0, 10) == UINT64_MAX);
  CHECK(!sircl_core_window_claim(&flight, &high, 64, 65, 0));
  CHECK(sircl_core_window_claim(&flight, &high, 64, 65, 1));
  CHECK(!sircl_core_window_claim(&flight, &high, 64, 1, 1));
  sircl_core_window_release(&flight, 65);
  high = 0;
  pthread_t threads[8];
  for (int i = 0; i < 8; ++i) CHECK(!pthread_create(&threads[i], NULL, worker, NULL));
  for (int i = 0; i < 8; ++i) CHECK(!pthread_join(threads[i], NULL));
  CHECK(!flight && high <= 64);
  flight = UINT64_MAX;
  CHECK(!sircl_core_window_claim(&flight, NULL, 0, 1, 0));
  struct sircl_core_abort abort = {0};
  sircl_core_request_abort(&abort, 7); sircl_core_request_abort(&abort, 8);
  CHECK(sircl_core_abort_reason(&abort) == 7);
  unsigned a = 0, b = 0;
  struct sircl_core_progress_source sources[] = {{poller, &a}, {poller, &b}};
  CHECK(sircl_core_progress_poll(sources, 2, 3) == 6 && a == 1 && b == 1);
  sources[0].poll = broken;
  CHECK(sircl_core_progress_poll(sources, 2, 3) == -EPROTO);
  CHECK(sircl_core_progress_poll(NULL, 1, 3) == -EINVAL);
  puts("core: concurrent bounded windows, overflow, abort and progress exact");
  return 0;
}
