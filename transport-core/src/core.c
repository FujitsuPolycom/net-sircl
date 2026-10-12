/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 SparkRing contributors. */
#include <sircl_core/core.h>
#include <errno.h>
#include <limits.h>
#ifndef SIRCL_CORE_BUILD_ID
#define SIRCL_CORE_BUILD_ID "unrecorded"
#endif
const char *sircl_core_source_id(void) { return SIRCL_CORE_BUILD_ID; }
unsigned sircl_core_abi(void) { return SIRCL_CORE_ABI; }
const char *sircl_core_version(void) { return SIRCL_CORE_VERSION; }
void sircl_core_request_abort(struct sircl_core_abort *s, int reason) {
  if (s && reason) {
    int expected = 0;
    __atomic_compare_exchange_n(&s->reason, &expected, reason, 0, __ATOMIC_RELEASE, __ATOMIC_RELAXED);
  }
}
int sircl_core_abort_reason(const struct sircl_core_abort *s) {
  return s ? __atomic_load_n(&s->reason, __ATOMIC_ACQUIRE) : 0;
}
int sircl_core_progress_poll(struct sircl_core_progress_source *sources, size_t count, unsigned budget) {
  if ((!sources && count) || !budget) return -EINVAL;
  int total = 0;
  for (size_t i = 0; i < count; ++i) {
    if (!sources[i].poll) return -EINVAL;
    int n = sources[i].poll(sources[i].context, budget);
    if (n < 0) return n;
    if ((unsigned)n > budget) return -EPROTO;
    total = n > INT_MAX - total ? INT_MAX : total + n;
  }
  return total;
}
