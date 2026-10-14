/* Static relay admission for the complete topology route set. See ROUTE-ALLOWANCE.md.
 * This is plugin policy, not a change to transport-core's route selection. */
#ifndef NET_SIRCL_ROUTES_H
#define NET_SIRCL_ROUTES_H

#define ROUTE_MODEL_HOSTS 8
#define ROUTE_FUNCTIONS 4

static int route_function(const struct sircl_core_topology *t, const char *name) {
  for (int f = 0; f < ROUTE_FUNCTIONS; ++f)
    if (!strcmp(t->fn[f / 2][f % 2], name)) return f;
  return -1;
}

/* Return 1 only when every forward/reverse lane can be reconstructed exactly.
 * Otherwise retain the aggregate bound, including for larger topologies. */
static int route_allowances(const struct sircl_core_topology *t, uint32_t budget,
                            uint32_t caps[MAX_POSITIONS][ROUTE_FUNCTIONS]) {
  unsigned sources = t->nhosts > 2 ? (unsigned)t->nhosts - 2 : 1;
  uint32_t fallback = budget / sources / 64u * 64u;
  for (int s = 0; s < t->nhosts; ++s)
    for (int f = 0; f < ROUTE_FUNCTIONS; ++f) caps[s][f] = fallback;
  if (t->nhosts < 2 || t->nhosts > ROUTE_MODEL_HOSTS) return 0;
  for (int f = 0; f < ROUTE_FUNCTIONS; ++f)
    for (int h = 0; h < f; ++h)
      if (!strcmp(t->fn[f / 2][f % 2], t->fn[h / 2][h % 2])) return 0;

  int neighbor[ROUTE_MODEL_HOSTS][2], arrival[ROUTE_MODEL_HOSTS][2];
  memset(neighbor, -1, sizeof neighbor);
  memset(arrival, -1, sizeof arrival);
  for (int c = 0; c < t->ncables; ++c) {
    int a = t->cable[c].a, b = t->cable[c].b, pa = t->cable[c].pa, pb = t->cable[c].pb;
    if (a < 0 || b < 0 || a >= t->nhosts || b >= t->nhosts || a == b ||
        pa < 0 || pa > 1 || pb < 0 || pb > 1 || neighbor[a][pa] >= 0 || neighbor[b][pb] >= 0) return 0;
    neighbor[a][pa] = b; arrival[a][pa] = pb;
    neighbor[b][pb] = a; arrival[b][pb] = pa;
  }
  /* A queue is (relay, outgoing port, function class). A source allowance is
   * (origin, first port, function class); multiple destinations/QPs share it. */
  uint32_t users[ROUTE_MODEL_HOSTS * ROUTE_FUNCTIONS] = {0};
  unsigned weight[ROUTE_MODEL_HOSTS * ROUTE_FUNCTIONS] = {0};
  for (int s = 0; s < t->nhosts; ++s)
    for (int d = 0; d < t->nhosts; ++d) if (d != s) {
      const char *fn[2], *back[2];
      int relays, reverse_relays;
      if (sircl_core_topology_route(t, s, d, fn, &relays) < 0) continue;
      if (sircl_core_topology_route(t, d, s, back, &reverse_relays) != 2 || reverse_relays != relays) return 0;
      for (int lane = 0; lane < 2; ++lane) {
        int f = route_function(t, fn[lane]);
        if (f < 0) return 0;
        int origin = s * ROUTE_FUNCTIONS + f, u = s, port = f / 2;
        weight[origin] += (unsigned)relays;
        for (int hop = 0; ; ++hop) {
          if (hop > relays || neighbor[u][port] < 0) return 0;
          int v = neighbor[u][port], in = arrival[u][port];
          if (v == d) {
            if (hop != relays || strcmp(back[lane], t->fn[in][f % 2])) return 0;
            break;
          }
          port = 1 - in; u = v;
          users[u * ROUTE_FUNCTIONS + port * 2 + f % 2] |= UINT32_C(1) << origin;
        }
      }
    }
  uint32_t result[ROUTE_MODEL_HOSTS * ROUTE_FUNCTIONS];
  for (int a = 0; a < t->nhosts * ROUTE_FUNCTIONS; ++a) result[a] = budget / 64u * 64u;
  for (int q = 0; q < t->nhosts * ROUTE_FUNCTIONS; ++q) {
    unsigned total = 0;
    for (int a = 0; a < t->nhosts * ROUTE_FUNCTIONS; ++a)
      if (users[q] & (UINT32_C(1) << a)) total += weight[a];
    for (int a = 0; a < t->nhosts * ROUTE_FUNCTIONS; ++a) if (users[q] & (UINT32_C(1) << a)) {
      uint32_t share = (uint32_t)((uint64_t)budget * weight[a] / total / 64u * 64u);
      if (share < result[a]) result[a] = share;
    }
  }
  /* Preserve enough room for a 640-byte control pair and independent progress. */
  for (int a = 0; a < t->nhosts * ROUTE_FUNCTIONS; ++a)
    if (weight[a] && result[a] < 1024) return 0;
  for (int s = 0; s < t->nhosts; ++s)
    for (int f = 0; f < ROUTE_FUNCTIONS; ++f) caps[s][f] = result[s * ROUTE_FUNCTIONS + f];
  return 1;
}
/* Split the existing physical envelope 7B/5 into forward charge 6B/5 and
 * reverse ACK debt B/5. Both use the SAME source weights. Rounding only lowers
 * allowances. Tiny shares retain the aggregate policy for the whole topology:
 * every admitted source must be able to reserve a two-packet control pair. */
static int split_allowances(const struct sircl_core_topology *t, int derived,
                            uint32_t caps[MAX_POSITIONS][ROUTE_FUNCTIONS],
                            uint32_t ack[MAX_POSITIONS][ROUTE_FUNCTIONS]) {
  memset(ack, 0, MAX_POSITIONS * ROUTE_FUNCTIONS * sizeof(uint32_t));
  if (!derived) return 0;
  for (int s = 0; s < t->nhosts; ++s)
    for (int f = 0; f < ROUTE_FUNCTIONS; ++f)
      if (caps[s][f] < 1280) return 0;
  for (int s = 0; s < t->nhosts; ++s)
    for (int f = 0; f < ROUTE_FUNCTIONS; ++f) {
      ack[s][f] = caps[s][f] / 5u / 128u * 128u;
      caps[s][f] = caps[s][f] * 6u / 5u / 64u * 64u;
    }
  return 1;
}
#endif
