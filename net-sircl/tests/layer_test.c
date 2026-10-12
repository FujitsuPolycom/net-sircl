/* net-sircl layer tests: the plugin's entry points between two processes of one host, over libsircl's
 * shared-memory verbs stand-in (the plugin built with NET_SIRCL_STANDIN). No NCCL, no GPU, no RoCE device.
 *
 *   layer_test plain|windowed|lanes|teardown|peerloss|timeout|routeless
 *
 * The parent listens and receives; the child connects and sends. Each mode prints one line starting
 * "net-sircl layer test <mode>:" and exits 0 when its checks hold:
 *   plain      0 B, 1 B, 16 B, 4097 B, 64 KiB, 1 MiB and 4 MiB exact with their sizes, nothing written past a
 *              message, then 32 requests in flight at once
 *   windowed   two send comms toward one relayed peer share one 64 KiB window (16 KiB chunks): 8 MiB each,
 *              interleaved, exact, and never more than 64 KiB in flight through the function
 *   lanes      routes on both sides (emu0/emu1): a connection keeps NCCL's device when it is a lane, takes
 *              the lanes in turn when it is not, and both ends of every connection use the same lane
 *   routeless  no routes: every connection uses the device NCCL passed
 *   teardown   comms closed with receives and a send outstanding, after traffic: every close and finalize
 *              returns within 2 s
 *   peerloss   the sender's process ends after connecting: the receiver's waiting request fails with
 *              ncclRemoteError within 5 s instead of waiting forever
 *   timeout    a listener that never accepts: connect fails within NET_SIRCL_CONNECT_TIMEOUT_MS (1.5 s) */
#define _GNU_SOURCE
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "shm_verbs.h"
#include "nccl/net.h"
#include "../net_sircl.h"

extern const ncclNet_v12_t ncclNetPlugin_v12;
#define NET ncclNetPlugin_v12

static const size_t SIZES[] = {0, 1, 16, 4097, 65536, 1u << 20, 4u << 20};
#define NSIZES (sizeof SIZES / sizeof SIZES[0])
#define PIPE_N 32
#define PIPE_BYTES 8192
#define SLACK 64
#define REGION ((size_t)(8u << 20) + SLACK + (size_t)PIPE_N * PIPE_BYTES)
#define H NCCL_NET_HANDLE_MAXSIZE

static const char *mode;

static void logger(ncclDebugLogLevel level, unsigned long flags, const char *file, int line, const char *fmt, ...) {
  (void)flags;
  if (level != NCCL_LOG_WARN || getenv("LAYER_QUIET")) return;
  va_list ap;
  va_start(ap, fmt);
  fprintf(stderr, "[%d] WARN %s:%d ", (int)getpid(), file, line);
  vfprintf(stderr, fmt, ap);
  fputc('\n', stderr);
  va_end(ap);
}

static void die(const char *what) {
  fprintf(stderr, "[%d] layer test %s failure: %s\n", (int)getpid(), mode, what);
  exit(1);
}

static double now_s(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static uint8_t pattern(size_t message, size_t j) { return (uint8_t)(message * 131u + j * 7u + (j >> 8)); }

static void *region(void) {
  void *p = malloc(REGION); /* ordinary memory: the plugin registers NCCL's buffers the same way */
  if (!p) die("malloc");
  memset(p, 0, REGION);
  return p;
}

static void wait_done(void *request, int *size) {
  int done = 0;
  double deadline = now_s() + 30;
  while (!done) {
    if (NET.test(request, &done, size) != ncclSuccess) die("test");
    if (now_s() > deadline) die("a request never completed");
  }
}

static void *init(void) {
  void *ctx = NULL;
  if (NET.init(&ctx, 1, NULL, logger, NULL) != ncclSuccess) die("init");
  int n = 0;
  if (NET.devices(&n) != ncclSuccess || n < 1) die("devices");
  ncclNetProperties_v12_t p;
  if (NET.getProperties(0, &p) != ncclSuccess) die("getProperties");
  if (p.ptrSupport != NCCL_PTR_HOST || p.maxRecvs != 1 || strncmp(p.name, "sircl:", 6) || p.maxP2pBytes == 0)
    die("properties");
  return ctx;
}

static int dev_named(const char *name) {
  int n = 0;
  NET.devices(&n);
  for (int d = 0; d < n; ++d) {
    ncclNetProperties_v12_t p;
    NET.getProperties(d, &p);
    if (!strcmp(p.name + 6, name)) return d;
  }
  die("no such device");
  return -1;
}

static void *accept_one(void *listen) {
  void *comm = NULL;
  double deadline = now_s() + 30;
  while (!comm) {
    if (NET.accept(listen, &comm, NULL) != ncclSuccess) die("accept");
    if (now_s() > deadline) die("accept never completed");
  }
  return comm;
}

static void *connect_one(void *ctx, int dev, void *handle) {
  void *comm = NULL;
  double deadline = now_s() + 30;
  while (!comm) {
    if (NET.connect(ctx, dev, handle, &comm, NULL) != ncclSuccess) die("connect");
    if (now_s() > deadline) die("connect never completed");
  }
  return comm;
}

static void *post_recv(void *comm, void *data, size_t cap, int tag, void *mr) {
  void *req = NULL;
  while (!req)
    if (NET.irecv(comm, 1, &data, &cap, &tag, &mr, NULL, &req) != ncclSuccess) die("irecv");
  return req;
}

static void *post_send(void *comm, void *data, size_t size, int tag, void *mr) {
  void *req = NULL;
  double deadline = now_s() + 30;
  while (!req) {
    if (NET.isend(comm, data, size, tag, mr, NULL, &req) != ncclSuccess) die("isend");
    if (now_s() > deadline) die("a send never matched its receive");
  }
  return req;
}

static void send_handles(int fd, char handles[][H], int n) {
  if (write(fd, handles, (size_t)n * H) != (ssize_t)(n * H)) die("handing the handles over");
}

static void read_handles(int fd, char handles[][H], int n) {
  size_t got = 0;
  while (got < (size_t)n * H) {
    ssize_t r = read(fd, (char *)handles + got, (size_t)n * H - got);
    if (r <= 0) die("reading the handles");
    got += (size_t)r;
  }
}

static int sync_byte(int fd, int write_it) {
  char b = 1;
  return write_it ? (int)write(fd, &b, 1) : (int)read(fd, &b, 1);
}

/* -- plain -- */
static void plain_recv(void *comm) {
  uint8_t *buf = region();
  void *mr = NULL;
  if (NET.regMr(comm, buf, REGION, NCCL_PTR_HOST, &mr) != ncclSuccess) die("regMr (receiver)");
  for (size_t i = 0; i < NSIZES; ++i) {
    memset(buf, 0xee, SIZES[i] + SLACK);
    int got = -1;
    wait_done(post_recv(comm, buf, SIZES[i] + SLACK, (int)i, mr), &got);
    if ((size_t)got != SIZES[i]) die("a receive reported the wrong size");
    for (size_t j = 0; j < SIZES[i]; ++j)
      if (buf[j] != pattern(i, j)) die("a received byte differs");
    if (buf[SIZES[i]] != 0xee) die("a receive wrote past the message");
  }
  void *reqs[PIPE_N];
  for (int k = 0; k < PIPE_N; ++k) reqs[k] = post_recv(comm, buf + (size_t)k * PIPE_BYTES, PIPE_BYTES, 100 + k, mr);
  for (int k = 0; k < PIPE_N; ++k) {
    int got = -1;
    wait_done(reqs[k], &got);
    if (got != PIPE_BYTES) die("a pipelined receive reported the wrong size");
    for (size_t j = 0; j < PIPE_BYTES; ++j)
      if (buf[(size_t)k * PIPE_BYTES + j] != pattern(100 + (size_t)k, j)) die("a pipelined byte differs");
  }
  NET.deregMr(comm, mr);
  free(buf);
}

static void plain_send(void *comm) {
  uint8_t *buf = region();
  void *mr = NULL;
  if (NET.regMr(comm, buf, REGION, NCCL_PTR_HOST, &mr) != ncclSuccess) die("regMr (sender)");
  for (size_t i = 0; i < NSIZES; ++i) {
    for (size_t j = 0; j < SIZES[i]; ++j) buf[j] = pattern(i, j);
    wait_done(post_send(comm, buf, SIZES[i], (int)i, mr), NULL);
  }
  for (int k = 0; k < PIPE_N; ++k)
    for (size_t j = 0; j < PIPE_BYTES; ++j) buf[(size_t)k * PIPE_BYTES + j] = pattern(100 + (size_t)k, j);
  void *reqs[PIPE_N];
  for (int k = 0; k < PIPE_N; ++k) reqs[k] = post_send(comm, buf + (size_t)k * PIPE_BYTES, PIPE_BYTES, 100 + k, mr);
  for (int k = 0; k < PIPE_N; ++k) wait_done(reqs[k], NULL);
  uint64_t max_inflight = 0, chunks = 0;
  net_sircl_stats(comm, &max_inflight, &chunks);
  printf("net-sircl layer test plain: %zu sizes and %d pipelined messages exact; %llu payload writes\n", NSIZES,
         PIPE_N, (unsigned long long)chunks);
  NET.deregMr(comm, mr);
  free(buf);
}

/* -- windowed: two comms, one shared window -- */
#define WIN 65536
#define WIN_MSG (1u << 20)
#define WIN_N 8

static void windowed_recv(void *comms[2]) {
  uint8_t *buf[2];
  void *mr[2];
  for (int c = 0; c < 2; ++c) {
    buf[c] = region();
    if (NET.regMr(comms[c], buf[c], REGION, NCCL_PTR_HOST, &mr[c]) != ncclSuccess) die("regMr");
  }
  void *reqs[2][WIN_N];
  for (int k = 0; k < WIN_N; ++k)
    for (int c = 0; c < 2; ++c) reqs[c][k] = post_recv(comms[c], buf[c] + (size_t)k * WIN_MSG, WIN_MSG, k, mr[c]);
  for (int k = 0; k < WIN_N; ++k)
    for (int c = 0; c < 2; ++c) {
      int got = -1;
      wait_done(reqs[c][k], &got);
      if (got != (int)WIN_MSG) die("a windowed receive reported the wrong size");
      for (size_t j = 0; j < WIN_MSG; ++j)
        if (buf[c][(size_t)k * WIN_MSG + j] != pattern((size_t)(c * 64 + k), j)) die("a windowed byte differs");
    }
  for (int c = 0; c < 2; ++c) {
    NET.deregMr(comms[c], mr[c]);
    free(buf[c]);
  }
}

static void windowed_send(void *comms[2]) {
  uint8_t *buf[2];
  void *mr[2];
  for (int c = 0; c < 2; ++c) {
    buf[c] = region();
    if (NET.regMr(comms[c], buf[c], REGION, NCCL_PTR_HOST, &mr[c]) != ncclSuccess) die("regMr");
    for (int k = 0; k < WIN_N; ++k)
      for (size_t j = 0; j < WIN_MSG; ++j) buf[c][(size_t)k * WIN_MSG + j] = pattern((size_t)(c * 64 + k), j);
  }
  void *reqs[2][WIN_N];
  for (int k = 0; k < WIN_N; ++k)
    for (int c = 0; c < 2; ++c) reqs[c][k] = post_send(comms[c], buf[c] + (size_t)k * WIN_MSG, WIN_MSG, k, mr[c]);
  for (int k = 0; k < WIN_N; ++k)
    for (int c = 0; c < 2; ++c) wait_done(reqs[c][k], NULL);
  uint64_t max0 = 0, max1 = 0, chunks0 = 0, chunks1 = 0;
  net_sircl_stats(comms[0], &max0, &chunks0);
  net_sircl_stats(comms[1], &max1, &chunks1);
  if (max0 != max1) die("the two comms did not share one window");
  if (max0 > WIN) die("the shared window was exceeded");
  if (chunks0 < WIN_N * (WIN_MSG / 16384)) die("the windowed sends were not chunked");
  printf("net-sircl layer test windowed: 2 comms x %d MiB exact; shared window max in flight %llu B of %d; %llu "
         "payload writes\n", WIN_N, (unsigned long long)max0, WIN, (unsigned long long)(chunks0 + chunks1));
  for (int c = 0; c < 2; ++c) {
    NET.deregMr(comms[c], mr[c]);
    free(buf[c]);
  }
}

/* -- one message per comm, to show a connection carries data -- */
static void one_recv(void *comm, int tag) {
  uint8_t *buf = malloc(4096);
  void *mr = NULL;
  if (NET.regMr(comm, buf, 4096, NCCL_PTR_HOST, &mr) != ncclSuccess) die("regMr");
  int got = -1;
  wait_done(post_recv(comm, buf, 4096, tag, mr), &got);
  if (got != 1000) die("one message: wrong size");
  for (size_t j = 0; j < 1000; ++j)
    if (buf[j] != pattern((size_t)tag, j)) die("one message: a byte differs");
  NET.deregMr(comm, mr);
  free(buf);
}

static void one_send(void *comm, int tag) {
  uint8_t *buf = malloc(4096);
  for (size_t j = 0; j < 1000; ++j) buf[j] = pattern((size_t)tag, j);
  void *mr = NULL;
  if (NET.regMr(comm, buf, 4096, NCCL_PTR_HOST, &mr) != ncclSuccess) die("regMr");
  wait_done(post_send(comm, buf, 1000, tag, mr), NULL);
  NET.deregMr(comm, mr);
  free(buf);
}

/* lanes: expected (device, lane) per connection, in connect order. */
static const char *const LANE_DEV[3] = {"emu1", "emu0", "emu0"};
static const int LANE_IDX[3] = {1, 0, 0};

static int receiver(int to_child, int from_child) {
  int listens = !strcmp(mode, "windowed") ? 2 : !strcmp(mode, "lanes") || !strcmp(mode, "routeless") ? 3 : 1;
  if (!strcmp(mode, "windowed") || !strcmp(mode, "lanes")) setenv("NET_SIRCL_POSITION", "3", 1);
  if (!strcmp(mode, "lanes")) {
    setenv("NET_SIRCL_STANDIN_DEVICES", "3", 1);
    setenv("NET_SIRCL_ROUTES", "5=emu0/emu1", 1);
  }
  if (!strcmp(mode, "routeless")) setenv("NET_SIRCL_STANDIN_DEVICES", "3", 1);
  void *ctx = init();
  char handles[3][H];
  void *listen[3], *comms[3] = {NULL, NULL, NULL};
  for (int i = 0; i < listens; ++i)
    if (NET.listen(ctx, 0, handles[i], &listen[i]) != ncclSuccess) die("listen");
  send_handles(to_child, handles, listens);
  if (!strcmp(mode, "timeout")) {
    int status = 0;
    wait(&status); /* never accept; the child's connect must give up */
    NET.closeListen(listen[0]);
    NET.finalize(ctx);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
  }
  for (int i = 0; i < listens; ++i) comms[i] = accept_one(listen[i]);
  if (!strcmp(mode, "plain")) {
    plain_recv(comms[0]);
  } else if (!strcmp(mode, "windowed")) {
    windowed_recv(comms);
  } else if (!strcmp(mode, "lanes") || !strcmp(mode, "routeless")) {
    for (int i = 0; i < 3; ++i) {
      char name[64];
      int lane = net_sircl_comm_device(comms[i], name, sizeof name);
      if (!strcmp(mode, "lanes") && (strcmp(name, LANE_DEV[i]) || lane != LANE_IDX[i]))
        die("an accepted comm is not on the connector's lane");
      if (!strcmp(mode, "routeless") && (strcmp(name, "emu0") || lane != -1))
        die("an accepted comm without routes left the listening device");
      one_recv(comms[i], 40 + i);
    }
  } else if (!strcmp(mode, "teardown")) {
    uint8_t *buf = region();
    void *mr = NULL;
    if (NET.regMr(comms[0], buf, REGION, NCCL_PTR_HOST, &mr) != ncclSuccess) die("regMr");
    for (int k = 0; k < 4; ++k) post_recv(comms[0], buf + (size_t)k * PIPE_BYTES, PIPE_BYTES, k, mr);
    sync_byte(to_child, 1);   /* receives posted */
    sync_byte(from_child, 0); /* the sender has sent twice and left a send outstanding */
    double t0 = now_s();
    NET.deregMr(comms[0], mr);
    NET.closeRecv(comms[0]);
    NET.closeListen(listen[0]);
    NET.finalize(ctx);
    double took = now_s() - t0;
    free(buf);
    if (took > 2.0) die("closing with receives outstanding took more than 2 s");
    int status = 0;
    wait(&status);
    printf("net-sircl layer test teardown: receiver closed with 2 receives outstanding in %.3f s\n", took);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
  } else if (!strcmp(mode, "peerloss")) {
    uint8_t *buf = region();
    void *mr = NULL;
    if (NET.regMr(comms[0], buf, REGION, NCCL_PTR_HOST, &mr) != ncclSuccess) die("regMr");
    void *req = post_recv(comms[0], buf, 4096, 0, mr);
    int status = 0;
    wait(&status); /* the sender's process has ended */
    double t0 = now_s();
    int done = 0;
    ncclResult_t r = ncclSuccess;
    while (!done && r == ncclSuccess && now_s() - t0 < 5.0) r = NET.test(req, &done, NULL);
    double took = now_s() - t0;
    if (r != ncclRemoteError) die("a receive from a vanished peer did not fail with ncclRemoteError within 5 s");
    printf("net-sircl layer test peerloss: the waiting receive failed with ncclRemoteError %.3f s after the "
           "peer ended\n", took);
    NET.closeRecv(comms[0]);
    NET.closeListen(listen[0]);
    NET.finalize(ctx);
    free(buf);
    return 0;
  }
  for (int i = 0; i < listens; ++i) {
    NET.closeRecv(comms[i]);
    NET.closeListen(listen[i]);
  }
  NET.finalize(ctx);
  int status = 0;
  wait(&status);
  return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}

static int sender(int from_parent, int to_parent) {
  int listens = !strcmp(mode, "windowed") ? 2 : !strcmp(mode, "lanes") || !strcmp(mode, "routeless") ? 3 : 1;
  if (!strcmp(mode, "windowed")) {
    char w[32];
    snprintf(w, sizeof w, "3=%d", WIN);
    setenv("NET_SIRCL_WINDOWS", w, 1);
    setenv("NET_SIRCL_ROUTES", "3=emu0", 1);
    setenv("NET_SIRCL_CHUNK", "16384", 1);
  }
  if (!strcmp(mode, "lanes")) {
    setenv("NET_SIRCL_POSITION", "5", 1);
    setenv("NET_SIRCL_STANDIN_DEVICES", "3", 1);
    setenv("NET_SIRCL_ROUTES", "3=emu0/emu1", 1);
  }
  if (!strcmp(mode, "routeless")) setenv("NET_SIRCL_STANDIN_DEVICES", "3", 1);
  if (!strcmp(mode, "timeout")) setenv("NET_SIRCL_CONNECT_TIMEOUT_MS", "1500", 1);
  void *ctx = init();
  char handles[3][H];
  read_handles(from_parent, handles, listens);
  if (!strcmp(mode, "timeout")) {
    void *comm = NULL;
    double t0 = now_s();
    ncclResult_t r = ncclSuccess;
    while (!comm && r == ncclSuccess && now_s() - t0 < 10.0) r = NET.connect(ctx, 0, handles[0], &comm, NULL);
    double took = now_s() - t0;
    if (r == ncclSuccess || took > 4.0) die("connect to a listener that never accepts did not fail within 4 s");
    printf("net-sircl layer test timeout: connect failed after %.3f s (deadline 1.5 s)\n", took);
    NET.finalize(ctx);
    return 0;
  }
  void *comms[3] = {NULL, NULL, NULL};
  if (!strcmp(mode, "lanes") || !strcmp(mode, "routeless")) {
    /* NCCL's devices: emu1 (a lane), emu0 (a lane), emu2 (not a lane: the lanes in turn, so lane 0). */
    int devs[3] = {dev_named("emu1"), dev_named("emu0"), dev_named("emu2")};
    for (int i = 0; i < 3; ++i) comms[i] = connect_one(ctx, devs[i], handles[i]);
    for (int i = 0; i < 3; ++i) {
      char name[64];
      int lane = net_sircl_comm_device(comms[i], name, sizeof name);
      const char *want = !strcmp(mode, "lanes") ? LANE_DEV[i] : (i == 0 ? "emu1" : i == 1 ? "emu0" : "emu2");
      int want_lane = !strcmp(mode, "lanes") ? LANE_IDX[i] : -1;
      if (strcmp(name, want) || lane != want_lane) die("a connection chose the wrong device or lane");
      one_send(comms[i], 40 + i);
    }
    printf("net-sircl layer test %s: 3 connections on %s, both ends agree, each carries a message exactly\n",
           mode, !strcmp(mode, "lanes") ? "emu1/lane 1, emu0/lane 0, emu0/lane 0 (emu2 is not a lane)"
                                        : "the devices NCCL passed (emu1, emu0, emu2)");
  } else {
    for (int i = 0; i < listens; ++i) comms[i] = connect_one(ctx, 0, handles[i]);
  }
  if (!strcmp(mode, "plain")) {
    plain_send(comms[0]);
  } else if (!strcmp(mode, "windowed")) {
    windowed_send(comms);
  } else if (!strcmp(mode, "teardown")) {
    uint8_t *buf = region();
    void *mr = NULL;
    if (NET.regMr(comms[0], buf, REGION, NCCL_PTR_HOST, &mr) != ncclSuccess) die("regMr");
    sync_byte(from_parent, 0);
    for (int k = 0; k < 2; ++k) wait_done(post_send(comms[0], buf, 1000, k, mr), NULL);
    void *pending = NULL;
    if (NET.isend(comms[0], buf, 1000, 2, mr, NULL, &pending) != ncclSuccess) die("isend");
    (void)pending; /* left outstanding */
    sync_byte(to_parent, 1);
    double t0 = now_s();
    NET.deregMr(comms[0], mr);
    NET.closeSend(comms[0]);
    NET.finalize(ctx);
    double took = now_s() - t0;
    free(buf);
    if (took > 2.0) die("closing with a send outstanding took more than 2 s");
    return 0;
  } else if (!strcmp(mode, "peerloss")) {
    _exit(0); /* the process ends with its comm open, as a crashed rank does */
  }
  for (int i = 0; i < listens; ++i) NET.closeSend(comms[i]);
  NET.finalize(ctx);
  return 0;
}

int main(int argc, char **argv) {
  static const char *const modes[] = {"plain", "windowed", "lanes", "routeless", "teardown", "peerloss", "timeout"};
  int known = 0;
  for (size_t i = 0; argc == 2 && i < sizeof modes / sizeof modes[0]; ++i) known |= !strcmp(argv[1], modes[i]);
  if (!known) {
    fprintf(stderr, "usage: %s plain|windowed|lanes|routeless|teardown|peerloss|timeout\n", argv[0]);
    return 2;
  }
  mode = argv[1];
  int down[2], up[2];
  if (pipe(down) || pipe(up)) die("pipe");
  alarm(120);
  pid_t child = fork();
  if (child < 0) die("fork");
  if (child == 0) {
    close(down[1]);
    close(up[0]);
    return sender(down[0], up[1]);
  }
  close(down[0]);
  close(up[1]);
  int rc = receiver(down[1], up[0]);
  if (strcmp(mode, "timeout") && strcmp(mode, "teardown") && strcmp(mode, "peerloss")) {
    int status = 0;
    if (waitpid(child, &status, WNOHANG) == 0) waitpid(child, &status, 0);
  }
  return rc;
}
