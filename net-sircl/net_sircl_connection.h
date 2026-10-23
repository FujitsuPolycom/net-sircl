/* Private connection lifecycle: nonblocking metadata exchange, QP ownership and retryable teardown. */
static int set_nonblocking(int fd) {
  int flags = fcntl(fd, F_GETFL, 0);
  return flags < 0 ? -1 : fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static int send_all(int fd, const void *buf, size_t len) {
  const char *p = buf;
  int64_t deadline = now_ms() + 10000;
  while (len) {
    ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
    if (n < 0 && (errno == EAGAIN || errno == EINTR)) {
      if (now_ms() > deadline) return -1;
      struct pollfd pf = {fd, POLLOUT, 0};
      poll(&pf, 1, 100);
      continue;
    }
    if (n <= 0) return -1;
    p += n;
    len -= (size_t)n;
  }
  return 0;
}

/* Progress a receive of `len` bytes; 1 when complete, 0 when more is due, -1 on failure or end of stream. */
static int recv_some(int fd, void *buf, size_t len, size_t *got) {
  while (*got < len) {
    ssize_t n = recv(fd, (char *)buf + *got, len - *got, 0);
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) return -1;
    *got += (size_t)n;
  }
  return 1;
}

/* The peer's end of the comm's socket closed: its process ended or it closed the comm. */
static int peer_gone(struct comm *c) {
  if (c->fd < 0) return 0;
  struct pollfd pf = {c->fd, POLLIN | POLLRDHUP, 0};
  if (poll(&pf, 1, 0) <= 0) return 0;
  if (pf.revents & (POLLHUP | POLLERR | POLLRDHUP)) return 1;
  char b;
  return (pf.revents & POLLIN) && recv(c->fd, &b, 1, MSG_PEEK) == 0;
}

/* -- queue pairs -- */
static ncclResult_t comm_destroy(struct comm *c) {
  if (!c) return ncclSuccess;
  if (c->win) pthread_mutex_lock(&c->win->lock);
  /* Stop the QP before retiring its local accounting. An abandoned relay write may still be
   * in the fabric: poison the allowance instead of lending its credit to another comm. */
  if (c->qp) {
    if (sircl_core_destroy_qp(c->qp)) {
      if (c->win) pthread_mutex_unlock(&c->win->lock);
      WARN("net-sircl: queue pair destruction failed; communicator ownership retained");
      return c->error = ncclSystemError;
    }
    c->qp = NULL;
  }
  if (c->win) {
    admission_leave_locked(c);
    pthread_mutex_unlock(&c->win->lock);
    c->win = NULL;
  }
  if (c->ctrl_mr) {
    if (sircl_core_dereg_mr(c->ctrl_mr)) {
      WARN("net-sircl: control registration release failed; communicator ownership retained");
      return c->error = ncclSystemError;
    }
    c->ctrl_mr = NULL;
  }
  if (c->cq) {
    if (sircl_core_destroy_cq(c->cq)) {
      WARN("net-sircl: completion queue destruction failed; communicator ownership retained");
      return c->error = ncclSystemError;
    }
    c->cq = NULL;
  }
  if (profile_enabled) profile_close(c);
  shared_free(c->ctrl);
  if (c->fd >= 0) close(c->fd);
  free(c);
  return ncclSuccess;
}

static struct comm *comm_create(int d, int is_send) {
  struct comm *c = calloc(1, sizeof *c);
  if (!c) return NULL;
  c->dev = d;
  c->is_send = is_send;
  c->fd = -1;
  c->lane = -1;
  struct dev *dv = &g.devs[d];
  c->cq = sircl_core_create_cq(dv->ctx, CQ_DEPTH);
  c->qp = c->cq ? sircl_core_create_rc_qp(dv->pd, c->cq, SEND_DEPTH, 16) : NULL;
  c->ctrl = shared_alloc(sizeof *c->ctrl);
  c->ctrl_mr = c->ctrl ? sircl_core_reg_mr(dv->pd, c->ctrl, sizeof *c->ctrl,
                                    IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE) : NULL;
  if (!c->cq || !c->qp || !c->ctrl || !c->ctrl_mr) {
    WARN("net-sircl: queue pair setup on %s failed", dv->name);
    comm_destroy(c);
    return NULL;
  }
  if (sircl_core_qp_init(c->qp, dv->port, IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE)) {
    WARN("net-sircl: queue pair to INIT on %s failed", dv->name);
    comm_destroy(c);
    return NULL;
  }
  return c;
}

static void meta_fill(struct comm *c, struct net_sircl_meta *m) {
  struct dev *dv = &g.devs[c->dev];
  memset(m, 0, sizeof *m);
  m->magic = NET_SIRCL_MAGIC;
  m->qpn = c->qp->qp_num;
  memcpy(m->gid, dv->gid.raw, 16);
  m->mtu = (uint32_t)dv->mtu;
  m->ctrl_rkey = c->ctrl_mr->rkey;
  m->ctrl_addr = (uint64_t)(uintptr_t)c->ctrl;
  m->position = g.position;
  m->lane = c->lane;
  m->policy = g.policy;
}

/* Fail fast when this function's interface has no route to the peer's address: a peer beyond a cable needs
 * a relay table on the hosts between, and without one RTR would only time out after about a minute. */
static ncclResult_t route_check(struct comm *c, const struct net_sircl_meta *r) {
#ifdef NET_SIRCL_STANDIN
  (void)c; (void)r;
  return ncclSuccess;
#else
  struct dev *dv = &g.devs[c->dev];
  static const uint8_t mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xff, 0xff};
  if (!dv->netdev[0] || memcmp(r->gid, mapped, 12) || !env_long("NET_SIRCL_ROUTE_CHECK", 1)) return ncclSuccess;
  int fd = socket(AF_INET, SOCK_DGRAM, 0);
  if (fd < 0) return ncclSuccess;
  ncclResult_t result = ncclSuccess;
  if (setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, dv->netdev, (socklen_t)strlen(dv->netdev)) == 0) {
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = htons(4791);
    memcpy(&sa.sin_addr.s_addr, r->gid + 12, 4);
    if (connect(fd, (struct sockaddr *)&sa, sizeof sa) && (errno == ENETUNREACH || errno == EHOSTUNREACH)) {
      char ip[INET_ADDRSTRLEN];
      inet_ntop(AF_INET, &sa.sin_addr, ip, sizeof ip);
      WARN("net-sircl: no route from %s (%s) to %s, the peer at position %d: a peer beyond a cable needs a relay "
           "table on the hosts between (README, Peers beyond a cable)", dv->name, dv->netdev, ip, r->position);
      result = ncclSystemError;
    }
  }
  close(fd);
  return result;
#endif
}

static ncclResult_t comm_ready(struct comm *c, const struct net_sircl_meta *r) {
  struct dev *dv = &g.devs[c->dev];
  if (r->magic != NET_SIRCL_MAGIC) {
    WARN("net-sircl: peer metadata has magic %08x", r->magic);
    return ncclRemoteError;
  }
  if (r->policy != g.policy || r->position < 0 || r->position >= g.nhosts ||
      !g.routes[r->position].ndev) {
    WARN("net-sircl: peer topology/budget disagreement or unknown position %d", r->position);
    return ncclRemoteError;
  }
  ncclResult_t rc = route_check(c, r);
  if (rc != ncclSuccess) return rc;
  struct sircl_core_qp_peer peer = {
    .qpn = r->qpn, .mtu = r->mtu < (uint32_t)dv->mtu ? r->mtu : (uint32_t)dv->mtu,
    .port = dv->port, .gid_index = (uint8_t)dv->gid_index, .hop_limit = 64,
    .traffic_class = (uint8_t)g.traffic_class
  };
  if (peer.mtu < IBV_MTU_256 || peer.mtu > IBV_MTU_4096) return ncclRemoteError;
  c->mtu_bytes = 128u << peer.mtu;
  memcpy(peer.gid, r->gid, 16);
  if (sircl_core_qp_rtr(c->qp, &peer)) {
    WARN("net-sircl: queue pair to RTR on %s failed: %s (peer position %d unreachable from this function?)",
         dv->name, strerror(errno), r->position);
    return ncclSystemError;
  }
  if (sircl_core_qp_rts(c->qp, (unsigned)g.ib_timeout, (unsigned)g.ib_retry_cnt)) {
    WARN("net-sircl: queue pair to RTS on %s failed: %s", dv->name, strerror(errno));
    return ncclSystemError;
  }
  c->rctrl_addr = r->ctrl_addr;
  c->rctrl_rkey = r->ctrl_rkey;
  c->peer_position = r->position;
  const struct route *route = &g.routes[r->position];
  c->win = route->relays ? &g.relay_windows[c->dev] :
           c->is_send ? window_for(c->dev, r->position, route->window) : NULL;
  if (c->is_send && route->window && !c->win) return ncclSystemError;
  if (c->win) window_join(c->win, c);
  return ncclSuccess;
}

static const struct route *route_of(int position) {
  if (position < 0 || position >= MAX_POSITIONS || g.routes[position].ndev == 0) return NULL;
  return &g.routes[position];
}

/* The connector's lane and function toward `position`: NCCL's device when it is one of the route's lanes
 * (NCCL spreads its channels over the devices, which keeps both PCI functions of a cable busy), else the
 * route's lanes in turn. Without a route, NCCL's device and no lane. */
static int connect_dev(int position, int nccl_dev, int *lane) {
  struct route *r = (struct route *)route_of(position);
  *lane = -1;
  if (!r) return nccl_dev;
  for (int l = 0; l < r->ndev; ++l)
    if (r->dev[l] == nccl_dev) {
      *lane = l;
      return nccl_dev;
    }
  pthread_mutex_lock(&g.lock);
  *lane = (int)(r->next++ % (unsigned)r->ndev);
  pthread_mutex_unlock(&g.lock);
  return r->dev[*lane];
}

/* The acceptor's function: the connector's lane of this side's route toward the connector, so both ends of
 * the queue pair are the same function class on the same path. */
static int accept_dev(int position, int lane, int listen_dev) {
  const struct route *r = route_of(position);
  if (!r) return listen_dev;
  if (lane >= 0 && lane < r->ndev) return r->dev[lane];
  for (int l = 0; l < r->ndev; ++l)
    if (r->dev[l] == listen_dev) return listen_dev;
  return r->dev[0];
}

/* -- listen / connect / accept -- */
static ncclResult_t sircl_listen(void *ctx, int d, void *handle, void **listenComm) {
  (void)ctx;
  if (d < 0 || d >= g.ndevs) return ncclInvalidArgument;
  struct net_sircl_handle *h = handle;
  memset(handle, 0, NCCL_NET_HANDLE_MAXSIZE);
  int fd = socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return ncclSystemError;
  struct sockaddr_in sa;
  memset(&sa, 0, sizeof sa);
  sa.sin_family = AF_INET;
  sa.sin_addr.s_addr = htonl(INADDR_ANY);
  socklen_t len = sizeof sa;
  if (bind(fd, (struct sockaddr *)&sa, sizeof sa) || listen(fd, 128) ||
      getsockname(fd, (struct sockaddr *)&sa, &len) || set_nonblocking(fd)) {
    close(fd);
    return ncclSystemError;
  }
  h->magic = NET_SIRCL_MAGIC;
  h->version = NET_SIRCL_VERSION;
  h->port = sa.sin_port;
  h->addr = g.oob_addr;
  h->position = g.position;
  h->pending = 0;
  struct listen_comm *l = calloc(1, sizeof *l);
  if (!l) {
    close(fd);
    return ncclSystemError;
  }
  l->fd = fd;
  l->dev = d;
  *listenComm = l;
  return ncclSuccess;
}

static void pending_drop(struct pending *p) {
  if (!p) return;
  if (p->c) comm_destroy(p->c);
  if (p->fd >= 0) close(p->fd);
  free(p);
}

static ncclResult_t connect_fail(struct net_sircl_handle *h, struct pending *p, ncclResult_t r) {
  h->pending = 0;
  pending_drop(p);
  return r;
}

static ncclResult_t sircl_connect(void *ctx, int d, void *handle, void **sendComm,
                                  ncclNetDeviceHandle_v12_t **sendDevComm) {
  (void)ctx;
  (void)sendDevComm;
  *sendComm = NULL;
  struct net_sircl_handle *h = handle;
  if (h->magic != NET_SIRCL_MAGIC || h->version != NET_SIRCL_VERSION) {
    WARN("net-sircl: handle magic %08x version %u", h->magic, h->version);
    return ncclInvalidArgument;
  }
  if (!route_of(h->position)) return ncclInvalidArgument;
  struct pending *p = (struct pending *)(uintptr_t)h->pending;
  if (!p) {
    p = calloc(1, sizeof *p);
    if (!p) return ncclSystemError;
    p->deadline_ms = now_ms() + g.connect_timeout_ms;
    int lane = -1;
    p->dev = connect_dev(h->position, d, &lane);
    p->fd = socket(AF_INET, SOCK_STREAM, 0);
    if (p->fd < 0 || set_nonblocking(p->fd)) return connect_fail(h, p, ncclSystemError);
    int one = 1;
    setsockopt(p->fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof sa);
    sa.sin_family = AF_INET;
    sa.sin_port = h->port;
    sa.sin_addr.s_addr = h->addr;
    if (connect(p->fd, (struct sockaddr *)&sa, sizeof sa) && errno != EINPROGRESS) {
      WARN("net-sircl: connecting to the listener: %s", strerror(errno));
      return connect_fail(h, p, ncclSystemError);
    }
    p->c = comm_create(p->dev, 1);
    if (!p->c) return connect_fail(h, p, ncclSystemError);
    p->c->lane = lane;
    p->state = P_CONNECTING;
    h->pending = (uint64_t)(uintptr_t)p;
  }
  if (now_ms() > p->deadline_ms) {
    WARN("net-sircl: connecting to position %d timed out after %d ms (NET_SIRCL_CONNECT_TIMEOUT_MS)", h->position,
         g.connect_timeout_ms);
    return connect_fail(h, p, ncclSystemError);
  }
  if (p->state == P_CONNECTING) {
    struct pollfd pf = {p->fd, POLLOUT, 0};
    if (poll(&pf, 1, 0) <= 0) return ncclSuccess;
    int err = 0;
    socklen_t len = sizeof err;
    getsockopt(p->fd, SOL_SOCKET, SO_ERROR, &err, &len);
    if (err) {
      WARN("net-sircl: connecting to the listener: %s", strerror(err));
      return connect_fail(h, p, ncclSystemError);
    }
    meta_fill(p->c, &p->mine);
    if (send_all(p->fd, &p->mine, sizeof p->mine)) return connect_fail(h, p, ncclSystemError);
    p->state = P_WAIT_META;
  }
  if (p->state == P_WAIT_META) {
    int r = recv_some(p->fd, &p->theirs, sizeof p->theirs, &p->got);
    if (r < 0) return connect_fail(h, p, ncclRemoteError);
    if (r == 0) return ncclSuccess;
    ncclResult_t rr = comm_ready(p->c, &p->theirs);
    if (rr != ncclSuccess) return connect_fail(h, p, rr);
    /* Ready: this queue pair is at RTS, so the acceptor may let its side write (as NCCL's IB transport
     * finishes its connect). */
    uint8_t ready = 1;
    if (send_all(p->fd, &ready, 1)) return connect_fail(h, p, ncclSystemError);
    p->c->fd = p->fd;
    p->fd = -1;
    *sendComm = p->c;
    INFO("net-sircl: send comm on %s (lane %d) to position %d, window %u", g.devs[p->c->dev].name, p->c->lane,
         p->c->peer_position, p->c->win ? p->c->win->cap : 0u);
    p->c = NULL;
    h->pending = 0;
    free(p);
  }
  return ncclSuccess;
}

static ncclResult_t accept_fail(struct listen_comm *l, ncclResult_t r) {
  pending_drop(l->accepting);
  l->accepting = NULL;
  return r;
}

static ncclResult_t sircl_accept(void *listenComm, void **recvComm, ncclNetDeviceHandle_v12_t **recvDevComm) {
  (void)recvDevComm;
  *recvComm = NULL;
  struct listen_comm *l = listenComm;
  struct pending *p = l->accepting;
  if (!p) {
    int fd = accept(l->fd, NULL, NULL);
    if (fd < 0) return (errno == EAGAIN || errno == EWOULDBLOCK) ? ncclSuccess : ncclSystemError;
    p = calloc(1, sizeof *p);
    if (!p) {
      close(fd);
      return ncclSystemError;
    }
    p->fd = fd;
    set_nonblocking(fd);
    p->state = P_WAIT_META;
    p->deadline_ms = now_ms() + g.connect_timeout_ms;
    l->accepting = p;
  }
  if (now_ms() > p->deadline_ms) {
    WARN("net-sircl: accepting a connection timed out after %d ms (NET_SIRCL_CONNECT_TIMEOUT_MS)",
         g.connect_timeout_ms);
    return accept_fail(l, ncclSystemError);
  }
  if (p->state == P_WAIT_META) {
    int r = recv_some(p->fd, &p->theirs, sizeof p->theirs, &p->got);
    if (r < 0) return accept_fail(l, ncclRemoteError);
    if (r == 0) return ncclSuccess;
    p->c = comm_create(accept_dev(p->theirs.position, p->theirs.lane, l->dev), 0);
    if (!p->c) return accept_fail(l, ncclSystemError);
    p->c->lane = p->theirs.lane;
    meta_fill(p->c, &p->mine);
    if (send_all(p->fd, &p->mine, sizeof p->mine)) return accept_fail(l, ncclSystemError);
    ncclResult_t rr = comm_ready(p->c, &p->theirs);
    if (rr != ncclSuccess) return accept_fail(l, rr);
    p->state = P_WAIT_READY;
  }
  int r = recv_some(p->fd, &p->ready, 1, &p->ready_got);
  if (r < 0) return accept_fail(l, ncclRemoteError);
  if (r == 0) return ncclSuccess;
  p->c->fd = p->fd;
  p->fd = -1;
  *recvComm = p->c;
  p->c = NULL;
  l->accepting = NULL;
  free(p);
  return ncclSuccess;
}

/* -- memory -- */
