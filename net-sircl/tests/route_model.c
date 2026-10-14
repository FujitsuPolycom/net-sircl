/* Expose the production allocator to the independent Python route/queue oracle. */
#include "../net_sircl.c"
int test_route_caps(const char *path, uint32_t budget, uint32_t *out) {
  struct sircl_core_topology t;
  char err[512];
  if (sircl_core_topology_parse(path, &t, err, sizeof err)) return -1;
  uint32_t caps[MAX_POSITIONS][ROUTE_FUNCTIONS];
  int derived = route_allowances(&t, budget, caps);
  memcpy(out, caps, t.nhosts * ROUTE_FUNCTIONS * sizeof(uint32_t));
  return derived;
}

int test_split_caps(const char *path, uint32_t budget, uint32_t *out, uint32_t *acks) {
  struct sircl_core_topology t;
  char err[512];
  if (sircl_core_topology_parse(path, &t, err, sizeof err)) return -1;
  uint32_t caps[MAX_POSITIONS][ROUTE_FUNCTIONS], ack[MAX_POSITIONS][ROUTE_FUNCTIONS];
  int split = split_allowances(&t, route_allowances(&t, budget, caps), caps, ack);
  memcpy(out, caps, t.nhosts * ROUTE_FUNCTIONS * sizeof(uint32_t));
  memcpy(acks, ack, t.nhosts * ROUTE_FUNCTIONS * sizeof(uint32_t));
  return split;
}
