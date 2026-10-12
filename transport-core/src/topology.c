#define _GNU_SOURCE
#include <sircl_core/core.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#define MAX_POSITIONS SIRCL_CORE_MAX_POSITIONS
#define NET_SIRCL_MAX_LANES SIRCL_CORE_MAX_LANES
/* Extracted from net-sircl 7b81b968; see NOTICE. */
/* Parse a topology file (net_sircl.h "Topology file"). 0 on success; `err` says why not. */
int sircl_core_topology_parse(const char *path, struct sircl_core_topology *t, char *err, size_t len) {
  memset(t, 0, sizeof *t);
  snprintf(t->fn[0][0], 64, "rocep1s0f0");
  snprintf(t->fn[0][1], 64, "roceP2p1s0f0");
  snprintf(t->fn[1][0], 64, "rocep1s0f1");
  snprintf(t->fn[1][1], 64, "roceP2p1s0f1");
  t->budget = 393216; /* 75 % of a ConnectX-7 hairpin queue of 512 KiB (SIRCL's relay budget) */
  FILE *f = fopen(path, "r");
  if (!f) {
    snprintf(err, len, "cannot open %s: %s", path, strerror(errno));
    return -1;
  }
  char line[1024], cabling[16] = "";
  int lineno = 0, rc = 0;
  while (fgets(line, sizeof line, f) && !rc) {
    ++lineno;
    char *hash = strchr(line, '#');
    if (hash) *hash = '\0';
    char *save = NULL, *key = strtok_r(line, " \t\r\n", &save);
    if (!key) continue;
    if (!strcmp(key, "hosts")) {
      for (char *h; (h = strtok_r(NULL, " \t\r\n", &save)) && t->nhosts < MAX_POSITIONS;)
        snprintf(t->host[t->nhosts++], 64, "%s", h);
    } else if (!strcmp(key, "cabling")) {
      char *v = strtok_r(NULL, " \t\r\n", &save);
      snprintf(cabling, sizeof cabling, "%s", v ? v : "");
    } else if (!strcmp(key, "cable")) {
      char *x = strtok_r(NULL, " \t\r\n", &save), *y = strtok_r(NULL, " \t\r\n", &save);
      int a, pa, b, pb;
      if (!x || !y || sscanf(x, "%d.port%d", &a, &pa) != 2 || sscanf(y, "%d.port%d", &b, &pb) != 2 || pa < 0 ||
          pa > 1 || pb < 0 || pb > 1 || t->ncables >= 2 * MAX_POSITIONS) {
        snprintf(err, len, "%s:%d: a cable is `cable <i>.port<0|1> <j>.port<0|1>`", path, lineno);
        rc = -1;
      } else {
        t->cable[t->ncables].a = a, t->cable[t->ncables].pa = pa;
        t->cable[t->ncables].b = b, t->cable[t->ncables].pb = pb;
        t->ncables++;
      }
    } else if (!strcmp(key, "port0") || !strcmp(key, "port1")) {
      int p = key[4] - '0';
      for (int l = 0; l < NET_SIRCL_MAX_LANES; ++l) {
        char *v = strtok_r(NULL, " \t\r\n", &save);
        if (v) snprintf(t->fn[p][l], 64, "%s", v);
      }
    } else if (!strcmp(key, "relay_budget")) {
      char *v = strtok_r(NULL, " \t\r\n", &save), *end;
      unsigned long b = v ? strtoul(v, &end, 10) : 0;
      if (!v || *end || b == 0 || b > UINT32_MAX) {
        snprintf(err, len, "%s:%d: relay_budget is a positive byte count", path, lineno);
        rc = -1;
      } else {
        t->budget = (uint32_t)b;
      }
    } else if (strcmp(key, "net-sircl-topology")) {
      snprintf(err, len, "%s:%d: unknown key %s", path, lineno, key);
      rc = -1;
    }
  }
  fclose(f);
  if (rc) return rc;
  if (t->nhosts < 2) {
    snprintf(err, len, "%s: `hosts` names fewer than two hosts", path);
    return -1;
  }
  /* ring and path: host i's port 0 is cabled to host i+1's port 1 (SIRCL's ring cabling). */
  if (!strcmp(cabling, "ring") || !strcmp(cabling, "path")) {
    int n = t->nhosts, last = !strcmp(cabling, "ring") ? n : n - 1;
    if (n == 2 && last == 2) last = 1; /* a pair has one cable between its two hosts */
    if (t->ncables + last > 2 * MAX_POSITIONS) {
      snprintf(err, len, "%s: too many cables", path);
      return -1;
    }
    for (int i = 0; i < last; ++i) {
      t->cable[t->ncables].a = i, t->cable[t->ncables].pa = 0;
      t->cable[t->ncables].b = (i + 1) % n, t->cable[t->ncables].pb = 1;
      t->ncables++;
    }
  } else if (strcmp(cabling, "explicit") && cabling[0]) {
    snprintf(err, len, "%s: cabling is ring, path or explicit", path);
    return -1;
  }
  for (int c = 0; c < t->ncables; ++c)
    if (t->cable[c].a < 0 || t->cable[c].a >= t->nhosts || t->cable[c].b < 0 || t->cable[c].b >= t->nhosts ||
        t->cable[c].a == t->cable[c].b) {
      snprintf(err, len, "%s: a cable joins a host index outside 0-%d", path, t->nhosts - 1);
      return -1;
    }
  return 0;
}

/* Hops from every host to `from` over the cables (-1: unreachable). */
static void topo_distances(const struct sircl_core_topology *t, int from, int *dist) {
  int queue[MAX_POSITIONS], head = 0, tail = 0;
  for (int i = 0; i < t->nhosts; ++i) dist[i] = -1;
  dist[from] = 0;
  queue[tail++] = from;
  while (head < tail) {
    int u = queue[head++];
    for (int c = 0; c < t->ncables; ++c) {
      int v = t->cable[c].a == u ? t->cable[c].b : t->cable[c].b == u ? t->cable[c].a : -1;
      if (v >= 0 && dist[v] < 0) dist[v] = dist[u] + 1, queue[tail++] = v;
    }
  }
}

/* The route from `me` to `peer` as lane functions (names) and relays. One shortest direction: both functions
 * of that port. Two (opposite ends of an even ring): lane 0 follows the path that leaves the lower position
 * through its port 0 and lane 1 the other, as SIRCL's planner splits them (sparkring_sircl/routes.py
 * derive_lanes), so both ends name the same function class per lane. -1 when the peer is unreachable. */
int sircl_core_topology_route(const struct sircl_core_topology *t, int me, int peer, const char **fn, int *relays) {
  if (!t || !fn || !relays || me < 0 || peer < 0 || me >= t->nhosts || peer >= t->nhosts) return -1;
  int from_me[MAX_POSITIONS], from_peer[MAX_POSITIONS];
  topo_distances(t, me, from_me);
  topo_distances(t, peer, from_peer);
  int d = from_me[peer];
  if (d <= 0) return -1;
  /* Local ports that start a shortest path. */
  int port_ok[2] = {0, 0};
  for (int c = 0; c < t->ncables; ++c) {
    int lp = -1, v = -1;
    if (t->cable[c].a == me) lp = t->cable[c].pa, v = t->cable[c].b;
    else if (t->cable[c].b == me) lp = t->cable[c].pb, v = t->cable[c].a;
    if (lp >= 0 && from_peer[v] == d - 1) port_ok[lp] = 1;
  }
  *relays = d - 1;
  if (port_ok[0] != port_ok[1]) {
    int p = port_ok[0] ? 0 : 1;
    fn[0] = t->fn[p][0];
    fn[1] = t->fn[p][1];
    return 2;
  }
  /* Both ports start a shortest path: path A leaves the lower of the two positions through port 0. At the
   * lower position that is its own port 0; at the higher position path A arrives, so it leaves by the port
   * that the path from the lower position's port 0 reaches it on: in ring cabling, port 1. */
  int a_port = me < peer ? 0 : 1;
  fn[0] = t->fn[a_port][0];
  fn[1] = t->fn[1 - a_port][1];
  return 2;
}
