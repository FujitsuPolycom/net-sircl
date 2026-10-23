/* Private configuration boundary: validated settings and topology-to-device binding. */
/* -- settings -- */
static long env_long(const char *name, long fallback) {
  const char *v = getenv(name);
  if (!v || !*v) return fallback;
  char *end;
  long x = strtol(v, &end, 10);
  return *end ? fallback : x;
}

/* Reject malformed or overflowing QP fields before opening devices; never truncate to uint8_t. */
static ncclResult_t env_range(const char *name, int fallback, int min, int max, int *out) {
  const char *v = getenv(name);
  *out = fallback;
  if (!v) return ncclSuccess;
  for (const char *p = v; *p; ++p)
    if (*p < '0' || *p > '9') goto invalid;
  errno = 0;
  char *end;
  long x = strtol(v, &end, 10);
  if (!*v || errno == ERANGE || *end || x < min || x > max) goto invalid;
  *out = (int)x;
  return ncclSuccess;
invalid:
  WARN("net-sircl: %s must be a decimal integer in %d..%d (got '%s')", name, min, max, v);
  return ncclInvalidArgument;
}

static uint32_t oob_address(void) {
  const char *a = getenv("NET_SIRCL_OOB_ADDR");
  struct in_addr in;
  if (a && *a && inet_pton(AF_INET, a, &in) == 1) return in.s_addr;
  const char *ifname = getenv("NCCL_SOCKET_IFNAME");
  if (ifname && *ifname && *ifname != '^' && *ifname != '=') {
    struct ifaddrs *list, *i;
    if (getifaddrs(&list) == 0) {
      uint32_t found = 0;
      for (i = list; i && !found; i = i->ifa_next)
        if (i->ifa_addr && i->ifa_addr->sa_family == AF_INET && !strncmp(i->ifa_name, ifname, strlen(ifname)))
          found = ((struct sockaddr_in *)i->ifa_addr)->sin_addr.s_addr;
      freeifaddrs(list);
      if (found) return found;
    }
  }
  return htonl(INADDR_LOOPBACK);
}

static int in_list(const char *list, const char *name) {
  char copy[1024];
  snprintf(copy, sizeof copy, "%s", list);
  for (char *save = NULL, *e = strtok_r(copy, ", ", &save); e; e = strtok_r(NULL, ", ", &save))
    if (!strcmp(e, name)) return 1;
  return 0;
}

static int dev_by_name(const char *name) {
  for (int d = 0; d < g.ndevs; ++d)
    if (!strcmp(g.devs[d].name, name)) return d;
  return -1;
}

/* -- routes: NET_SIRCL_ROUTES, NET_SIRCL_WINDOWS, NET_SIRCL_TOPOLOGY -- */

/* Overrides may reduce a direct window or the function allowances, never invent a route.
 * Topology is the admission contract: unknown peers and changed routes fail closed. */
static ncclResult_t route_overrides(void) {
  const char *names[] = {"NET_SIRCL_ROUTES", "NET_SIRCL_WINDOWS"};
  for (int kind = 0; kind < 2; ++kind) {
    const char *text = getenv(names[kind]);
    if (!text) continue;
    if (!*text || strlen(text) >= 2048 || text[0] == ',' || text[strlen(text)-1] == ',' || strstr(text, ",,"))
      goto invalid;
    char copy[2048];
    strcpy(copy, text);
    uint64_t seen = 0;
    for (char *save = NULL, *e = strtok_r(copy, ",", &save); e; e = strtok_r(NULL, ",", &save)) {
      char *eq = strchr(e, '=');
      if (!eq || eq == e) goto invalid;
      *eq = 0;
      for (char *q = e; *q; ++q) if (*q < '0' || *q > '9') goto invalid;
      errno = 0;
      char *end;
      long peer = strtol(e, &end, 10);
      if (errno || *end || peer < 0 || peer >= g.nhosts || peer == g.position ||
          !g.routes[peer].ndev || (seen & (UINT64_C(1) << peer))) goto invalid;
      seen |= UINT64_C(1) << peer;
      struct route *r = &g.routes[peer];
      if (!kind) {
        char expected[160];
        snprintf(expected, sizeof expected, "%s/%s", g.devs[r->dev[0]].name, g.devs[r->dev[1]].name);
        if (strcmp(expected, eq + 1)) goto invalid;
      } else {
        if (!eq[1]) goto invalid;
        for (char *q = eq + 1; *q; ++q) if (*q < '0' || *q > '9') goto invalid;
        errno = 0;
        unsigned long cap = strtoul(eq + 1, &end, 10);
        if (errno || *end || cap > MAX_NET_BYTES || (cap && cap < 1024)) goto invalid;
        if (r->relays) {
          if (!cap) goto invalid;
          for (int d = 0; d < g.ndevs; ++d)
            if (cap < g.relay_windows[d].cap) g.relay_windows[d].cap = (uint32_t)cap;
        } else r->window = (uint32_t)cap;
      }
    }
    continue;
invalid:
    WARN("net-sircl: invalid %s; routes must match topology; relay windows cannot disable the function cap", names[kind]);
    return ncclInvalidArgument;
  }
  return ncclSuccess;
}

static int my_position_in(const struct sircl_core_topology *t) {
  long p = g.position;
  if (p >= 0) return (int)p;
  char name[256] = {0};
  if (gethostname(name, sizeof name - 1)) return -1;
  char *dot = strchr(name, '.');
  for (int i = 0; i < t->nhosts; ++i) {
    if (!strcmp(t->host[i], name)) return i;
    if (dot && (size_t)(dot - name) == strlen(t->host[i]) && !strncmp(t->host[i], name, (size_t)(dot - name)))
      return i;
  }
  return -1;
}

/* Routes and windows of every reachable peer from a topology file. */
static int routes_from_topology(const char *path) {
  struct sircl_route_plan plan;
  char err[512];
  if (sircl_route_plan_load(path, (uint32_t)g.relay_budget, NET_SIRCL_VERSION, &plan, err, sizeof err)) {
    WARN("net-sircl: NET_SIRCL_TOPOLOGY: %s", err);
    return -1;
  }
  const struct sircl_core_topology t = plan.topology;
  int me = my_position_in(&t);
  if (me < 0 || me >= t.nhosts) {
    WARN("net-sircl: NET_SIRCL_TOPOLOGY %s does not list this host; set NET_SIRCL_POSITION", path);
    return -1;
  }
  g.position = me;
  g.nhosts = t.nhosts;
  g.relay_budget = (int)plan.budget;
  g.policy = plan.policy;
  g.route_derived = plan.derived;
  unsigned sources = t.nhosts > 2 ? (unsigned)t.nhosts - 2 : 1;
  for (int d = 0; d < g.ndevs; ++d) {
    int f = sircl_route_function(&t, g.devs[d].name);
    g.relay_windows[d].cap = f >= 0 ? plan.caps[me][f] : (uint32_t)g.relay_budget / sources / 64u * 64u;
    g.relay_windows[d].ack_cap = f >= 0 ? plan.acks[me][f] : 0;
    g.relay_windows[d].relay = 1;
  }
  for (int peer = 0; peer < t.nhosts; ++peer) {
    if (peer == me) continue;
    const char *fn[2];
    int relays = 0;
    if (sircl_core_topology_route(&t, me, peer, fn, &relays) < 0) continue;
    struct route *r = &g.routes[peer];
    r->ndev = 0;
    for (int l = 0; l < NET_SIRCL_MAX_LANES; ++l) {
      int d = dev_by_name(fn[l]);
      if (d < 0) {
        WARN("net-sircl: topology lane %s is not exposed; retain both route lanes in NET_SIRCL_HCA", fn[l]);
        return -1;
      }
      r->dev[r->ndev++] = d;
    }
    r->relays = relays;
    uint32_t a = g.relay_windows[r->dev[0]].cap, b = g.relay_windows[r->dev[1]].cap;
    r->window = relays > 0 ? (a < b ? a : b) : 0;
  }
  return 0;
}

/* Test hook: the routes a topology file gives `position`, as "<peer>=<fn>/<fn>:<relays>:<window>;...". */
int net_sircl_topology_routes(const char *path, int position, char *out, size_t len) {
  if (!out || !len) return -1;
  struct sircl_route_plan plan;
  char err[512];
  out[0] = '\0';
  if (sircl_route_plan_load(path, RELAY_BUDGET_DEFAULT, NET_SIRCL_VERSION, &plan, err, sizeof err)) {
    snprintf(out, len, "error: %s", err);
    return -1;
  }
  const struct sircl_core_topology t = plan.topology;
  if (position < 0 || position >= t.nhosts) {
    snprintf(out, len, "error: position is outside the topology");
    return -1;
  }
  size_t used = 0;
  for (int peer = 0; peer < t.nhosts; ++peer) {
    const char *fn[2];
    int relays = 0;
    if (peer == position || sircl_core_topology_route(&t, position, peer, fn, &relays) < 0) continue;
    uint32_t a = plan.caps[position][sircl_route_function(&t, fn[0])], b = plan.caps[position][sircl_route_function(&t, fn[1])];
    int n = snprintf(out + used, len - used, "%d=%s/%s:%d:%u;", peer, fn[0], fn[1], relays,
                     relays > 0 ? (a < b ? a : b) : 0u);
    if (n < 0 || (size_t)n >= len - used) return -1;
    used += (size_t)n;
  }
  return 0;
}
