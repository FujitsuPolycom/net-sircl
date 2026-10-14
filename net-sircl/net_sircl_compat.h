/* ext-net ABI adapters. Included after the v12 implementation; no wire or progress state lives here. */
#ifndef NET_SIRCL_COMPAT_H
#define NET_SIRCL_COMPAT_H

/* v7-v10 have process-wide init and no finalize. Pin exactly one core reference until dlclose/exit,
 * including when NCCL initializes multiple communicators. A failed init remains retryable. */
static pthread_mutex_t compat_lock = PTHREAD_MUTEX_INITIALIZER;
static void *compat_ctx;

static ncclResult_t compat_init(int version, ncclDebugLogger_t log, ncclProfilerCallback_t prof) {
  pthread_mutex_lock(&compat_lock);
  ncclResult_t r = compat_ctx ? ncclSuccess : sircl_init(&compat_ctx, 0, NULL, log, prof);
  if (r == ncclSuccess && log)
    log(NCCL_LOG_INFO, NCCL_INIT, __FILE__, __LINE__, "net-sircl %s: ext-net v%d", NET_SIRCL_RELEASE, version);
  pthread_mutex_unlock(&compat_lock);
  return r;
}

__attribute__((destructor)) static void compat_finalize(void) {
  if (compat_ctx) sircl_finalize(compat_ctx);
}

#define COMPAT_INIT(V) \
  static ncclResult_t compat_init_v##V(ncclDebugLogger_t log) { return compat_init(V, log, NULL); }
COMPAT_INIT(7)
COMPAT_INIT(8)
COMPAT_INIT(9)
#undef COMPAT_INIT

static ncclResult_t compat_init_v10(ncclDebugLogger_t log, ncclProfilerCallback_t prof) {
  return compat_init(10, log, prof);
}

static ncclResult_t compat_init_v11(void **ctx, uint64_t id, ncclNetCommConfig_v11_t *config,
                                   ncclDebugLogger_t log, ncclProfilerCallback_t prof) {
  ncclNetCommConfig_v12_t cfg = { .trafficClass = config ? config->trafficClass : NCCL_NET_TRAFFIC_CLASS_UNDEF };
  ncclResult_t r = sircl_init(ctx, id, config ? &cfg : NULL, log, prof);
  if (r == ncclSuccess && log)
    log(NCCL_LOG_INFO, NCCL_INIT, __FILE__, __LINE__, "net-sircl %s: ext-net v11", NET_SIRCL_RELEASE);
  return r;
}

/* Properties are projected field by field: layouts differ, and copying a v12 struct would overwrite
 * the caller's allocation. All advertised devices are physical, host-only, single-receive NICs. */
#define PROPS_BASE \
  p->name = src.name; p->pciPath = src.pciPath; p->guid = src.guid; \
  p->ptrSupport = src.ptrSupport; p->speed = src.speed; p->port = src.port; \
  p->latency = src.latency; p->maxComms = src.maxComms; p->maxRecvs = src.maxRecvs; \
  p->netDeviceType = src.netDeviceType; p->netDeviceVersion = src.netDeviceVersion;
#define PROPS_V8 PROPS_BASE p->regIsGlobal = src.regIsGlobal;
#define PROPS_V9 PROPS_V8 \
  p->forceFlush = src.forceFlush; p->vProps.ndevs = src.vProps.ndevs; \
  for (int i = 0; i < src.vProps.ndevs; ++i) p->vProps.devs[i] = src.vProps.devs[i]; \
  p->maxP2pBytes = src.maxP2pBytes; p->maxCollBytes = src.maxCollBytes;
#define PROPS(V, FIELDS) \
  static ncclResult_t compat_props_v##V(int dev, ncclNetProperties_v##V##_t *p) { \
    ncclNetProperties_v12_t src; \
    ncclResult_t r = sircl_get_properties(dev, &src); \
    if (r != ncclSuccess) return r; \
    memset(p, 0, sizeof *p); \
    FIELDS \
    return ncclSuccess; \
  }
PROPS(7, PROPS_BASE)
PROPS(8, PROPS_V8)
PROPS(9, PROPS_V9)
PROPS(10, PROPS_V9)
PROPS(11, PROPS_V9 p->maxMultiRequestSize = src.maxMultiRequestSize;)
#undef PROPS
#undef PROPS_V9
#undef PROPS_V8
#undef PROPS_BASE

static ncclResult_t compat_listen(int dev, void *handle, void **comm) {
  return sircl_listen(NULL, dev, handle, comm);
}

/* Device handles v7-v12 are typedef aliases in NVIDIA's headers. */
static ncclResult_t compat_connect(int dev, void *handle, void **comm, ncclNetDeviceHandle_v7_t **devcomm) {
  return sircl_connect(NULL, dev, handle, comm, devcomm);
}

static ncclResult_t compat_connect_v10(int dev, ncclNetCommConfig_v10_t *config, void *handle,
                                      void **comm, ncclNetDeviceHandle_v10_t **devcomm) {
  /* v12 takes config at init and uses NET_SIRCL_TC, not the caller's traffic class. */
  (void)config;
  return compat_connect(dev, handle, comm, devcomm);
}

static ncclResult_t compat_reg_v7(void *comm, void *data, int size, int type, void **mr) {
  if (size < 0) return ncclInvalidArgument;
  return sircl_reg_mr(comm, data, (size_t)size, type, mr);
}

static ncclResult_t compat_send_v8(void *comm, void *data, int size, int tag, void *mr, void **req) {
  if (size < 0) { *req = NULL; return ncclInvalidArgument; }
  return sircl_isend(comm, data, (size_t)size, tag, mr, NULL, req);
}

static ncclResult_t compat_recv_v8(void *comm, int n, void **data, int *sizes, int *tags, void **mrs, void **req) {
  /* maxRecvs is one. Reject invalid groups before reading their arrays, as v12 does. */
  if (n != 1) return sircl_irecv(comm, n, data, NULL, tags, mrs, NULL, req);
  if (sizes[0] < 0) { *req = NULL; return ncclInvalidArgument; }
  size_t size = (size_t)sizes[0];
  return sircl_irecv(comm, n, data, &size, tags, mrs, NULL, req);
}

static ncclResult_t compat_send_v9(void *comm, void *data, size_t size, int tag, void *mr, void **req) {
  return sircl_isend(comm, data, size, tag, mr, NULL, req);
}

static ncclResult_t compat_recv_v9(void *comm, int n, void **data, size_t *sizes, int *tags, void **mrs, void **req) {
  return sircl_irecv(comm, n, data, sizes, tags, mrs, NULL, req);
}

static ncclResult_t compat_attr_v11(void *ctx, ncclNetAttr_v11_t *attr) {
  /* setNetAttr is a no-op in v12; no scheduling hints are consumed. */
  (void)attr;
  return sircl_set_net_attr(ctx, NULL);
}

#define COMPAT_COMMON \
  .name = "sircl", .devices = sircl_devices, .accept = sircl_accept, \
  .regMrDmaBuf = sircl_reg_mr_dmabuf, .deregMr = sircl_dereg_mr, \
  .iflush = sircl_iflush, .test = sircl_test, .closeSend = sircl_close_send, \
  .closeRecv = sircl_close_recv, .closeListen = sircl_close_listen, \
  .getDeviceMr = NULL, .irecvConsumed = NULL

__attribute__((visibility("default"))) const ncclNet_v7_t ncclNetPlugin_v7 = {
  COMPAT_COMMON, .init = compat_init_v7, .getProperties = compat_props_v7,
  .listen = compat_listen, .connect = compat_connect, .regMr = compat_reg_v7,
  .isend = compat_send_v8, .irecv = compat_recv_v8,
};
__attribute__((visibility("default"))) const ncclNet_v8_t ncclNetPlugin_v8 = {
  COMPAT_COMMON, .init = compat_init_v8, .getProperties = compat_props_v8,
  .listen = compat_listen, .connect = compat_connect, .regMr = sircl_reg_mr,
  .isend = compat_send_v8, .irecv = compat_recv_v8,
};
__attribute__((visibility("default"))) const ncclNet_v9_t ncclNetPlugin_v9 = {
  COMPAT_COMMON, .init = compat_init_v9, .getProperties = compat_props_v9,
  .listen = compat_listen, .connect = compat_connect, .regMr = sircl_reg_mr,
  .isend = compat_send_v9, .irecv = compat_recv_v9, .makeVDevice = NULL,
};
__attribute__((visibility("default"))) const ncclNet_v10_t ncclNetPlugin_v10 = {
  COMPAT_COMMON, .init = compat_init_v10, .getProperties = compat_props_v10,
  .listen = compat_listen, .connect = compat_connect_v10, .regMr = sircl_reg_mr,
  .isend = sircl_isend, .irecv = sircl_irecv, .makeVDevice = NULL,
};
__attribute__((visibility("default"))) const ncclNet_v11_t ncclNetPlugin_v11 = {
  COMPAT_COMMON, .init = compat_init_v11, .getProperties = compat_props_v11,
  .listen = sircl_listen, .connect = sircl_connect, .regMr = sircl_reg_mr,
  .isend = sircl_isend, .irecv = sircl_irecv, .makeVDevice = NULL,
  .finalize = sircl_finalize, .setNetAttr = compat_attr_v11,
};
#undef COMPAT_COMMON
#endif
