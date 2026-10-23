/* Private topology planning contract shared by the plugin and CPU oracle. */
#ifndef NET_SIRCL_PLAN_H
#define NET_SIRCL_PLAN_H
#include <sircl_core/core.h>
#define MAX_POSITIONS SIRCL_CORE_MAX_POSITIONS
#define ROUTE_FUNCTIONS 4
struct sircl_route_plan {
  struct sircl_core_topology topology;
  uint32_t caps[MAX_POSITIONS][ROUTE_FUNCTIONS];
  uint32_t acks[MAX_POSITIONS][ROUTE_FUNCTIONS];
  uint64_t policy;
  uint32_t budget;
  int derived;
};
#define SIRCL_PLAN_PRIVATE __attribute__((visibility("hidden")))
SIRCL_PLAN_PRIVATE int sircl_route_function(const struct sircl_core_topology *, const char *);
SIRCL_PLAN_PRIVATE int sircl_route_allowances(const struct sircl_core_topology *, uint32_t,
                          uint32_t [MAX_POSITIONS][ROUTE_FUNCTIONS]);
SIRCL_PLAN_PRIVATE int sircl_split_allowances(const struct sircl_core_topology *, int,
                          uint32_t [MAX_POSITIONS][ROUTE_FUNCTIONS],
                          uint32_t [MAX_POSITIONS][ROUTE_FUNCTIONS]);
/* Deterministic, with no device discovery or process-global configuration. */
SIRCL_PLAN_PRIVATE int sircl_route_plan_load(const char *, uint32_t, unsigned, struct sircl_route_plan *, char *, size_t);
#endif
