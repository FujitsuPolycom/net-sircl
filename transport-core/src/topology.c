#define _GNU_SOURCE
#include <sircl_core/core.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#define MAX_POSITIONS SIRCL_CORE_MAX_POSITIONS
#define NET_SIRCL_MAX_LANES SIRCL_CORE_MAX_LANES
#define FIELD 64 /* bytes of a host or function name, terminator included */
#define BLANK " \t\r\n"
/* Extracted from net-sircl 7b81b968; see NOTICE. */

/* The grammar is shared with fabric-tool's parser (fabric_tool/topology.py): a file one of them accepts
 * means the same cables to the other. tests/test_topology_conformance.py holds both to it. The parser
 * refuses what it does not understand instead of guessing: an unknown key, a missing or misspelt
 * `cabling`, extra words, a truncated name, a host without a cable. */

static int reject(char *err, size_t len, const char *fmt, ...) {
  va_list ap;
  va_start(ap, fmt);
  if (err && len) vsnprintf(err, len, fmt, ap);
  va_end(ap);
  return -1;
}

static void topo_distances(const struct sircl_core_topology *t, int from, int *dist);

/* `<position>.port<0|1>` and nothing else: decimal digits only, no sign, no trailing text. */
static int cable_end(const char *text, int *host, int *port) {
  const char *p = text;
  long value = 0;
  if (*p < '0' || *p > '9') return -1;
  for (; *p >= '0' && *p <= '9'; ++p)
    if (value < 1000000) value = value * 10 + (*p - '0'); /* any larger index is out of range anyway */
  if (strncmp(p, ".port", 5) || (p[5] != '0' && p[5] != '1') || p[6]) return -1;
  *host = (int)value;
  *port = p[5] - '0';
  return 0;
}

/* A function name as fabric-tool accepts it: letters, digits and `_.:-`, starting with a letter,
 * digit or `_`, at most 63 bytes. */
static int function_name(const char *text) {
  size_t n = strlen(text);
  if (!n || n >= FIELD) return 0;
  for (size_t i = 0; i < n; ++i) {
    char ch = text[i];
    int word = (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '_';
    if (!word && (i == 0 || (ch != '.' && ch != ':' && ch != '-'))) return 0;
  }
  return 1;
}

/* Parse a topology file (net_sircl.h "Topology file"). 0 on success; `err` says why not. */
int sircl_core_topology_parse(const char *path, struct sircl_core_topology *t, char *err, size_t len) {
  memset(t, 0, sizeof *t);
  snprintf(t->fn[0][0], FIELD, "rocep1s0f0");
  snprintf(t->fn[0][1], FIELD, "roceP2p1s0f0");
  snprintf(t->fn[1][0], FIELD, "rocep1s0f1");
  snprintf(t->fn[1][1], FIELD, "roceP2p1s0f1");
  t->budget = 393216; /* 75 % of a ConnectX-7 hairpin queue of 512 KiB (SIRCL's relay budget) */
  FILE *f = fopen(path, "r");
  if (!f) return reject(err, len, "cannot open %s: %s", path, strerror(errno));
  char line[1024], cabling[16] = "";
  int lineno = 0, rc = 0;
  while (!rc && fgets(line, sizeof line, f)) {
    ++lineno;
    size_t got = strlen(line);
    if (got == sizeof line - 1 && line[got - 1] != '\n') {
      /* The buffer is full without a line end: more of this line follows unless the file ends here. */
      int next = fgetc(f);
      if (next != EOF) {
        rc = reject(err, len, "%s:%d: the line is longer than %zu characters", path, lineno, sizeof line - 2);
        break;
      }
    }
    char *hash = strchr(line, '#');
    if (hash) *hash = '\0';
    char *save = NULL, *key = strtok_r(line, BLANK, &save);
    if (!key) continue;
    if (!strcmp(key, "hosts")) {
      for (char *h; !rc && (h = strtok_r(NULL, BLANK, &save));) {
        if (t->nhosts == MAX_POSITIONS)
          rc = reject(err, len, "%s:%d: more than %d hosts", path, lineno, MAX_POSITIONS);
        else if (strlen(h) >= FIELD)
          rc = reject(err, len, "%s:%d: a host name is longer than %d characters", path, lineno, FIELD - 1);
        else
          snprintf(t->host[t->nhosts++], FIELD, "%s", h);
      }
    } else if (!strcmp(key, "cabling")) {
      char *v = strtok_r(NULL, BLANK, &save);
      if (!v || strtok_r(NULL, BLANK, &save))
        rc = reject(err, len, "%s:%d: cabling takes one of ring, path, explicit", path, lineno);
      else
        snprintf(cabling, sizeof cabling, "%s", v);
    } else if (!strcmp(key, "cable")) {
      char *x = strtok_r(NULL, BLANK, &save), *y = strtok_r(NULL, BLANK, &save);
      int a, pa, b, pb;
      if (!x || !y || strtok_r(NULL, BLANK, &save) || cable_end(x, &a, &pa) || cable_end(y, &b, &pb)) {
        rc = reject(err, len, "%s:%d: a cable is `cable <i>.port<0|1> <j>.port<0|1>`", path, lineno);
      } else if (t->ncables >= 2 * MAX_POSITIONS) {
        rc = reject(err, len, "%s: too many cables", path);
      } else {
        t->cable[t->ncables].a = a, t->cable[t->ncables].pa = pa;
        t->cable[t->ncables].b = b, t->cable[t->ncables].pb = pb;
        t->ncables++;
      }
    } else if (!strcmp(key, "port0") || !strcmp(key, "port1")) {
      int p = key[4] - '0';
      char *v[NET_SIRCL_MAX_LANES + 1];
      for (int l = 0; l <= NET_SIRCL_MAX_LANES; ++l) v[l] = strtok_r(NULL, BLANK, &save);
      if (!v[0] || !v[1] || v[2])
        rc = reject(err, len, "%s:%d: %s requires exactly two device identifiers", path, lineno, key);
      for (int l = 0; !rc && l < NET_SIRCL_MAX_LANES; ++l) {
        if (!function_name(v[l]))
          rc = reject(err, len, "%s:%d: invalid device identifier %s", path, lineno, v[l]);
        else
          snprintf(t->fn[p][l], FIELD, "%s", v[l]);
      }
    } else if (!strcmp(key, "relay_budget")) {
      char *v = strtok_r(NULL, BLANK, &save);
      unsigned long long b = 0;
      int ok = v && *v && !strtok_r(NULL, BLANK, &save);
      for (const char *p = v; ok && *p; ++p) {
        if (*p < '0' || *p > '9') ok = 0;
        else if ((b = b * 10 + (unsigned)(*p - '0')) > UINT32_MAX) ok = 0;
      }
      if (!ok || b == 0) rc = reject(err, len, "%s:%d: relay_budget is a positive byte count", path, lineno);
      else t->budget = (uint32_t)b;
    } else if (!strcmp(key, "net-sircl-topology")) {
      if (strtok_r(NULL, BLANK, &save))
        rc = reject(err, len, "%s:%d: unknown words after net-sircl-topology", path, lineno);
    } else {
      rc = reject(err, len, "%s:%d: unknown key %s", path, lineno, key);
    }
  }
  if (!rc && ferror(f)) rc = reject(err, len, "cannot read %s", path);
  fclose(f);
  if (rc) return rc;
  if (t->nhosts < 2) return reject(err, len, "%s: `hosts` names fewer than two hosts", path);
  for (int a = 0; a < t->nhosts; ++a)
    for (int b = a + 1; b < t->nhosts; ++b)
      if (!strcmp(t->host[a], t->host[b])) return reject(err, len, "%s: hosts names one host twice", path);
  /* ring and path: host i's port 0 is cabled to host i+1's port 1 (SIRCL's ring cabling). */
  if (!strcmp(cabling, "ring") || !strcmp(cabling, "path")) {
    int n = t->nhosts, last = !strcmp(cabling, "ring") ? n : n - 1;
    if (n == 2 && last == 2) last = 1; /* a pair has one cable between its two hosts */
    if (t->ncables + last > 2 * MAX_POSITIONS) return reject(err, len, "%s: too many cables", path);
    for (int i = 0; i < last; ++i) {
      t->cable[t->ncables].a = i, t->cable[t->ncables].pa = 0;
      t->cable[t->ncables].b = (i + 1) % n, t->cable[t->ncables].pb = 1;
      t->ncables++;
    }
  } else if (strcmp(cabling, "explicit")) {
    /* A missing `cabling` line used to yield a topology without cables, and so without routes. */
    return reject(err, len, "%s: cabling is ring, path or explicit", path);
  }
  for (int c = 0; c < t->ncables; ++c)
    if (t->cable[c].a < 0 || t->cable[c].a >= t->nhosts || t->cable[c].b < 0 || t->cable[c].b >= t->nhosts ||
        t->cable[c].a == t->cable[c].b)
      return reject(err, len, "%s: a cable joins a host index outside 0-%d", path, t->nhosts - 1);
  unsigned occupied[MAX_POSITIONS] = {0};
  for (int c = 0; c < t->ncables; ++c) {
    int a = t->cable[c].a, b = t->cable[c].b;
    unsigned pa = 1u << t->cable[c].pa, pb = 1u << t->cable[c].pb;
    if ((occupied[a] & pa) || (occupied[b] & pb))
      return reject(err, len, "%s: topology connects a port more than once", path);
    occupied[a] |= pa;
    occupied[b] |= pb;
  }
  /* One fabric: every host is cabled, and the cables join them all. With two ports per host that is
   * one path or one cycle. Relay admission is planned over the whole file, so a host the cables do
   * not reach, or a second island, is a mistake in the file rather than something to route around. */
  for (int h = 0; h < t->nhosts; ++h)
    if (!occupied[h])
      return reject(err, len, "%s: the cables leave host %d (%s) without a cable", path, h, t->host[h]);
  int reach[MAX_POSITIONS];
  topo_distances(t, 0, reach);
  for (int h = 0; h < t->nhosts; ++h)
    if (reach[h] < 0) return reject(err, len, "%s: the cables do not join the hosts into one path or cycle", path);
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

/* The far end of the cable in `port` of `host`: 1 with its host and arrival port, 0 for an empty port. */
static int topo_cross(const struct sircl_core_topology *t, int host, int port, int *far, int *far_port) {
  for (int c = 0; c < t->ncables; ++c) {
    if (t->cable[c].a == host && t->cable[c].pa == port) {
      *far = t->cable[c].b, *far_port = t->cable[c].pb;
      return 1;
    }
    if (t->cable[c].b == host && t->cable[c].pb == port) {
      *far = t->cable[c].a, *far_port = t->cable[c].pa;
      return 1;
    }
  }
  return 0;
}

/* The route from `me` to `peer` as lane functions (names) and relays. One shortest direction: both functions
 * of that port. Two (opposite ends of an even ring, or a pair joined by two cables): lane 0 follows the path
 * that leaves the lower position through its port 0 and lane 1 the other, as SIRCL's planner splits them
 * (sparkring_sircl/routes.py derive_lanes), so both ends name the same function class per lane on the same
 * path. -1 when the peer is unreachable. */
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
  /* Both ports start a shortest path. Path A leaves the lower of the two positions through its port 0. The
   * higher position sends lane 0 out of the port on which path A arrives there: port 1 in ring cabling, and
   * whichever port the cables give when explicit cabling turns a host around. Assuming port 1 for every
   * cabling put the two ends of a lane on different paths. */
  int a_port = 0;
  if (me > peer) {
    int at = peer, out = 0;
    for (int hop = 0; hop < d; ++hop) {
      int next, in;
      if (!topo_cross(t, at, out, &next, &in)) return -1;
      at = next;
      a_port = in;
      out = 1 - in;
    }
    if (at != me) return -1;
  }
  fn[0] = t->fn[a_port][0];
  fn[1] = t->fn[1 - a_port][1];
  return 2;
}
