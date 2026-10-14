/* Included in the net DSO so NCCL's documented tuner fallback discovers it.
 * No NCCL environment mutation and no change to algorithm/protocol costs.
 * See docs/channels.md for upstream call sites and policy limitations. */
#include "nccl/tuner.h"

struct sircl_tuning {
  int channels;
  ncclDebugLogger_t log;
};

static ncclResult_t tuner_init(size_t ranks, size_t nodes, ncclDebugLogger_t log, void **context) {
  (void)ranks;
  *context = NULL;
  struct sircl_tuning *t = calloc(1, sizeof *t);
  if (!t) return ncclSystemError;
  t->log = log;
  /* A communicator-wide decision must agree at middle and end ranks. The
   * tuner API has no peer positions, so use the complete topology, not the
   * local rank's routes (the middle of a path of three has no relayed peer).
   * A two-host topology is a direct pair and retains NCCL's tuning. */
  pthread_mutex_lock(&g.lock);
  if (g.refs && g.nhosts > 2 && nodes > 1) t->channels = 8;
  pthread_mutex_unlock(&g.lock);
  int explicit_channels;
  ncclResult_t r = env_range("NET_SIRCL_CHANNELS", t->channels, 0, 64, &explicit_channels);
  if (r != ncclSuccess) { free(t); return r; }
  t->channels = explicit_channels;
  const char *overrides[] = {"NCCL_MAX_NCHANNELS", "NCCL_MIN_NCHANNELS", "NCCL_MAX_NRINGS",
                            "NCCL_MIN_NRINGS", "NCCL_MAX_CTAS", "NCCL_MIN_CTAS"};
  for (unsigned i = 0; i < sizeof overrides / sizeof overrides[0]; ++i)
    if (getenv(overrides[i])) t->channels = 0; /* including malformed entries: NCCL owns parsing */
  if (log) log(NCCL_LOG_INFO, NCCL_TUNING, __FILE__, __LINE__,
               "net-sircl tuner: collective channel limit %d (0: NCCL/user policy)", t->channels);
  *context = t;
  return ncclSuccess;
}

static ncclResult_t tuner_coll(void *context, ncclFunc_t coll, size_t bytes, int pipes,
                               float **costs, int algos, int protos, int registered, int *channels) {
  (void)coll; (void)bytes; (void)pipes; (void)costs; (void)algos; (void)protos; (void)registered;
  struct sircl_tuning *t = context;
  if (t->channels) *channels = t->channels;
  return ncclSuccess;
}

static ncclResult_t tuner_destroy(void *context) {
  free(context);
  return ncclSuccess;
}

__attribute__((visibility("default"))) const ncclTuner_v4_t ncclTunerPlugin_v4 = {
  .name = "sircl", .init = tuner_init, .getCollInfo = tuner_coll, .destroy = tuner_destroy
};
