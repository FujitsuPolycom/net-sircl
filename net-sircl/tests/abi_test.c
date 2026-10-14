/* Exercise each public ABI with its exact header types and caller-owned property storage.
 * The implementation is included to check shared lifetime accounting and unchanged v12 dispatch. */
#include "../net_sircl.c"
#include <assert.h>
#include <limits.h>
#include <stdarg.h>

#define CAT_(A, B) A##B
#define CAT(A, B) CAT_(A, B)
#define NET CAT(ncclNetPlugin_v, TEST_ABI)
#define PROP_(V) ncclNetProperties_v##V##_t
#define PROP(V) PROP_(V)
#define OK(CALL) assert((CALL) == ncclSuccess)
#if TEST_ABI <= 8
typedef int transfer_size;
#else
typedef size_t transfer_size;
#endif

static char init_log[256];
static void log_line(ncclDebugLogLevel level, unsigned long flags, const char *file, int line, const char *fmt, ...) {
  (void)level; (void)flags; (void)file; (void)line;
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(init_log, sizeof init_log, fmt, ap);
  va_end(ap);
}

static void *initialize(void) {
  void *ctx = NULL;
#if TEST_ABI <= 9
  OK(NET.init(log_line));
#elif TEST_ABI == 10
  OK(NET.init(log_line, NULL));
#else
  CAT(CAT(ncclNetCommConfig_v, TEST_ABI), _t) config = { .trafficClass = 42 };
  OK(NET.init(&ctx, 7, &config, log_line, NULL));
#endif
  assert(g.traffic_class == 17); /* NET_SIRCL_TC, including when config is supplied */
  return ctx;
}

static ncclResult_t listen_at(void *ctx, int dev, void *handle, void **comm) {
#if TEST_ABI <= 10
  (void)ctx;
  return NET.listen(dev, handle, comm);
#else
  return NET.listen(ctx, dev, handle, comm);
#endif
}

static ncclResult_t connect_to(void *ctx, void *handle, void **comm) {
  ncclNetDeviceHandle_t *device = NULL; /* host transport: no offload requested */
  ncclResult_t r;
#if TEST_ABI <= 9
  (void)ctx;
  r = NET.connect(0, handle, comm, &device);
#elif TEST_ABI == 10
  (void)ctx;
  ncclNetCommConfig_v10_t config = { .trafficClass = 42 };
  r = NET.connect(0, &config, handle, comm, &device);
#else
  r = NET.connect(ctx, 0, handle, comm, &device);
#endif
  assert(device == NULL);
  return r;
}

static ncclResult_t send_data(void *comm, void *data, transfer_size size, void *mr, void **req) {
#if TEST_ABI <= 9
  return NET.isend(comm, data, size, 23, mr, req);
#else
  return NET.isend(comm, data, size, 23, mr, NULL, req);
#endif
}

static ncclResult_t recv_data(void *comm, int n, void **data, transfer_size *sizes, void **mrs, void **req) {
  int tag = 23;
#if TEST_ABI <= 9
  return NET.irecv(comm, n, data, sizes, &tag, mrs, req);
#else
  return NET.irecv(comm, n, data, sizes, &tag, mrs, NULL, req);
#endif
}

int main(void) {
  alarm(30);
  setenv("NET_SIRCL_STANDIN_DEVICES", "2", 1);
  setenv("NET_SIRCL_OOB_ADDR", "127.0.0.1", 1);
  setenv("NET_SIRCL_TC", "17", 1);
  assert(!strcmp(NET.name, "sircl"));
  assert(NET.init && NET.devices && NET.getProperties && NET.listen && NET.connect && NET.accept);
  assert(NET.regMr && NET.regMrDmaBuf && NET.deregMr && NET.isend && NET.irecv && NET.iflush);
  assert(NET.test && NET.closeSend && NET.closeRecv && NET.closeListen);
  assert(!NET.getDeviceMr && !NET.irecvConsumed);
#if TEST_ABI >= 9
  assert(!NET.makeVDevice);
#endif
#if TEST_ABI >= 11
  assert(NET.finalize && NET.setNetAttr);
#endif
  void *ctx = initialize();
  /* The ABI fixture loops back within one process; give its local endpoint a test route. */
  g.routes[g.position] = g.routes[1];
#if TEST_ABI < 12
  char expected[32];
  snprintf(expected, sizeof expected, "ext-net v%d", TEST_ABI);
  assert(strstr(init_log, expected));
#endif
  void *ctx2 = initialize();
  (void)ctx2;
#if TEST_ABI <= 10
  assert(g.refs == 1); /* no unbounded reference growth without a finalize API */
#else
  assert(g.refs == 2);
  OK(NET.finalize(ctx2));
#endif
  int count = 0;
  OK(NET.devices(&count));
  assert(count == 2);
  struct { uint64_t before; PROP(TEST_ABI) p; uint64_t after; } guarded;
  memset(&guarded, 0xa5, sizeof guarded);
  for (int d = 0; d < count; ++d) {
    OK(NET.getProperties(d, &guarded.p));
    assert(guarded.before == UINT64_C(0xa5a5a5a5a5a5a5a5));
    assert(guarded.after == UINT64_C(0xa5a5a5a5a5a5a5a5));
    ncclNetProperties_v12_t p;
    OK(ncclNetPlugin_v12.getProperties(d, &p));
#define SAME(F) assert(guarded.p.F == p.F)
    SAME(name); SAME(pciPath); SAME(guid); SAME(ptrSupport); SAME(speed); SAME(port);
    SAME(latency); SAME(maxComms); SAME(maxRecvs); SAME(netDeviceType); SAME(netDeviceVersion);
#if TEST_ABI >= 8
    SAME(regIsGlobal);
#endif
#if TEST_ABI >= 9
    SAME(forceFlush); SAME(vProps.ndevs); SAME(vProps.devs[0]); SAME(maxP2pBytes); SAME(maxCollBytes);
#endif
#if TEST_ABI >= 11
    SAME(maxMultiRequestSize);
#endif
#undef SAME
  }
  assert(NET.getProperties(-1, &guarded.p) == ncclInvalidArgument);
  assert(NET.getProperties(count, &guarded.p) == ncclInvalidArgument);
  void *unused = NULL;
  assert(NET.regMr(NULL, NULL, 1, NCCL_PTR_CUDA, &unused) == ncclInternalError);
  assert(NET.regMrDmaBuf(NULL, NULL, 1, NCCL_PTR_DMABUF, 0, -1, &unused) == ncclInternalError);
  void *req = (void *)1;
  OK(NET.iflush(NULL, 0, NULL, NULL, NULL, &req));
  assert(req == NULL);
  assert(recv_data(NULL, 2, NULL, NULL, NULL, &req) == ncclInternalError && !req);
#if TEST_ABI <= 8
  transfer_size negative = -1;
  assert(send_data(NULL, NULL, -1, NULL, &req) == ncclInvalidArgument && !req);
  assert(recv_data(NULL, 1, NULL, &negative, NULL, &req) == ncclInvalidArgument && !req);
#endif
#if TEST_ABI == 7
  assert(NET.regMr(NULL, NULL, -1, NCCL_PTR_HOST, &unused) == ncclInvalidArgument);
#endif

  char handle[NCCL_NET_HANDLE_MAXSIZE];
  void *listener = NULL, *sender = NULL, *receiver = NULL;
  OK(listen_at(ctx, 0, handle, &listener));
  while (!sender || !receiver) {
    if (!sender) OK(connect_to(ctx, handle, &sender));
    if (!receiver) {
      ncclNetDeviceHandle_t *device = NULL;
      OK(NET.accept(listener, &receiver, &device));
      assert(!device);
    }
  }
  unsigned char input[8193], output[8194];
  for (size_t i = 0; i < sizeof input; ++i) input[i] = (unsigned char)(i * 17);
  memset(output, 0xcc, sizeof output);
  void *smr = NULL, *rmr = NULL;
  OK(NET.regMr(sender, input, sizeof input, NCCL_PTR_HOST, &smr));
  OK(NET.regMr(receiver, output, sizeof output, NCCL_PTR_HOST, &rmr));
  for (int iteration = 0; iteration < 40; ++iteration) {
    transfer_size size = iteration == 0 ? 0 : sizeof input;
    void *data = output, *sr = NULL, *rr = NULL;
    OK(recv_data(receiver, 1, &data, &size, &rmr, &rr));
    assert(rr);
    while (!sr) OK(send_data(sender, input, size, smr, &sr));
    int sd = 0, rd = 0, bytes = -1;
    while (!sd || !rd) {
      if (!sd) OK(NET.test(sr, &sd, NULL));
      if (!rd) OK(NET.test(rr, &rd, &bytes));
    }
    assert(bytes == (int)size && !memcmp(input, output, size));
    assert(output[sizeof input] == 0xcc);
  }
  OK(NET.deregMr(sender, smr)); OK(NET.deregMr(receiver, rmr));
  OK(NET.closeSend(sender)); OK(NET.closeRecv(receiver)); OK(NET.closeListen(listener));
#if TEST_ABI >= 11
  OK(NET.setNetAttr(ctx, NULL));
  OK(NET.finalize(ctx));
  assert(g.refs == 0 && g.ndevs == 0);
#else
  /* v12 users can coexist without releasing the process-wide legacy reference. */
  void *v12ctx = NULL;
  OK(ncclNetPlugin_v12.init(&v12ctx, 1, NULL, log_line, NULL));
  assert(g.refs == 2);
  OK(ncclNetPlugin_v12.finalize(v12ctx));
  assert(g.refs == 1 && g.ndevs == 2);
#endif
  printf("ext-net v%d: properties, callbacks, lifetime and 40 transfers exact\n", TEST_ABI);
  return 0;
}
