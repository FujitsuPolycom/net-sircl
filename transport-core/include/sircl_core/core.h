/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 SparkRing contributors. See NOTICE for provenance. */
#ifndef SIRCL_CORE_H
#define SIRCL_CORE_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
#if defined(SIRCL_CORE_STATIC) && defined(__GNUC__)
#define SIRCL_CORE_API __attribute__((visibility("hidden")))
#else
#define SIRCL_CORE_API
#endif
#define SIRCL_CORE_ABI 1u
#define SIRCL_CORE_VERSION "0.1.0"
#define SIRCL_CORE_MAX_POSITIONS 64
#define SIRCL_CORE_MAX_LANES 2
SIRCL_CORE_API unsigned sircl_core_abi(void);
SIRCL_CORE_API const char *sircl_core_version(void);
SIRCL_CORE_API const char *sircl_core_source_id(void);

/* Zero cap is unbounded. The inline helpers add no lock or allocation. */
static inline uint64_t sircl_core_window_room(uint64_t cap, uint64_t outstanding) {
  return !cap ? UINT64_MAX : outstanding >= cap ? 0 : cap - outstanding;
}
static inline int sircl_core_window_claim(uint64_t *outstanding, uint64_t *high_water,
                                         uint64_t cap, uint64_t bytes, int legacy_oversize) {
  uint64_t cur = __atomic_load_n(outstanding, __ATOMIC_ACQUIRE);
  for (;;) {
    if (bytes > UINT64_MAX - cur) return 0;
    if (cap && cur + bytes > cap && !(legacy_oversize && !cur)) return 0;
    if (__atomic_compare_exchange_n(outstanding, &cur, cur + bytes, 0,
                                    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) break;
  }
  if (high_water) {
    uint64_t now = cur + bytes, max = __atomic_load_n(high_water, __ATOMIC_RELAXED);
    while (now > max && !__atomic_compare_exchange_n(high_water, &max, now, 0,
                                                    __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {}
  }
  return 1;
}
/* Caller retires a proven outstanding amount exactly once. */
static inline void sircl_core_window_release(uint64_t *outstanding, uint64_t bytes) {
  __atomic_fetch_sub(outstanding, bytes, __ATOMIC_ACQ_REL);
}
struct sircl_core_abort { int reason; };
SIRCL_CORE_API void sircl_core_request_abort(struct sircl_core_abort *state, int reason);
SIRCL_CORE_API int sircl_core_abort_reason(const struct sircl_core_abort *state);
/* A callback runs at most budget units; returns work count or a negative error.
 * The array and callback owners must outlive the poll; no implicit threads. */
struct sircl_core_progress_source {
  int (*poll)(void *context, unsigned budget);
  void *context;
};
SIRCL_CORE_API int sircl_core_progress_poll(struct sircl_core_progress_source *sources,
                             size_t count, unsigned budget);

struct sircl_core_topology {
  int nhosts;
  char host[SIRCL_CORE_MAX_POSITIONS][64];
  int ncables;
  struct { int a, pa, b, pb; } cable[2 * SIRCL_CORE_MAX_POSITIONS];
  char fn[2][SIRCL_CORE_MAX_LANES][64];
  uint32_t budget;
};
SIRCL_CORE_API int sircl_core_topology_parse(const char *path, struct sircl_core_topology *out,
                             char *error, size_t capacity);
SIRCL_CORE_API int sircl_core_topology_route(const struct sircl_core_topology *topology,
                             int local, int peer, const char **functions, int *relays);
SIRCL_CORE_API uint64_t sircl_core_sysfs_guid(const char *root, const char *device);
SIRCL_CORE_API void sircl_core_sysfs_netdev(const char *root, const char *device, char *out, size_t len);
SIRCL_CORE_API int sircl_core_gid_index(const char *root, const char *device, unsigned port);
#ifdef __cplusplus
}
#endif
#endif
