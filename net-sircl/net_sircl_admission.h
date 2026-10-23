/* Private admission implementation. The including translation unit supplies protocol posting
 * and CQ-drain callbacks; accounting, membership and FIFO ownership live here. Callers hold the
 * shared window lock across reservation, completion and retirement. */
static ncclResult_t send_progress(struct comm *, struct req *);
static ncclResult_t recv_progress(struct comm *, struct req *);
static ncclResult_t drain(struct comm *);

static struct window *window_for(int d, int position, uint32_t cap) {
  if (!cap) return NULL;
  pthread_mutex_lock(&g.lock);
  struct window *w = NULL;
  for (int i = 0; i < g.nwindows && !w; ++i)
    if (g.windows[i].dev == d && g.windows[i].position == position) w = &g.windows[i];
  if (!w && g.nwindows < MAX_WINDOWS) {
    w = &g.windows[g.nwindows++];
    memset(w, 0, sizeof *w);
    w->dev = d, w->position = position, w->cap = cap;
    pthread_mutex_init(&w->lock, NULL);
  }
  pthread_mutex_unlock(&g.lock);
  return w;
}

static void window_join(struct window *w, struct comm *c) {
  pthread_mutex_lock(&w->lock);
  c->next = w->members;
  w->members = c;
  pthread_mutex_unlock(&w->lock);
}

static void admission_leave_locked(struct comm *c) {
    /* Queued requests belong to the comm; never retain them after free. */
    struct req **pending = &c->win->pending_head;
    c->win->pending_tail = NULL;
    while (*pending) {
      if ((*pending)->c == c) *pending = (*pending)->pending_next;
      else {
        c->win->pending_tail = *pending;
        pending = &(*pending)->pending_next;
      }
    }
    struct comm **link = &c->win->members;
    while (*link && *link != c) link = &(*link)->next;
    if (*link) *link = c->next;
    if (c->inflight) {
      if (c->win->relay) c->win->error = ncclRemoteError;
      else sircl_core_window_release(&c->win->inflight, c->inflight);
    }
}

/* Charge 64-byte buffer strides plus 128 bytes of frame overhead and one 128-byte ACK
 * per packet. RC send completion proves both the write and its ACK have left the relay path.
 * The reserve counts both directions, even if the hardware gives them independent queues. */
static uint64_t write_charge(struct comm *c, uint32_t bytes) {
  if (!c->win || !c->win->relay) return bytes;
  uint64_t packets = bytes ? ((uint64_t)bytes + c->mtu_bytes - 1) / c->mtu_bytes : 1;
  return (((uint64_t)bytes + 63u) & ~UINT64_C(63)) + packets * 256u;
}

static uint64_t ack_charge(struct comm *c, uint32_t bytes) {
  if (!c->win || !c->win->ack_cap) return 0;
  return 128u * (bytes ? ((uint64_t)bytes + c->mtu_bytes - 1) / c->mtu_bytes : 1);
}

static int window_take(struct window *w, uint64_t bytes) {
  return !w->error && sircl_core_window_claim(&w->inflight, &w->max_inflight, w->cap, bytes, 0);
}

/* Reserve both resources atomically under the shared window lock. No WR is
 * posted while holding only one resource, so there is no resource wait cycle. */
static int window_take_ack(struct window *w, uint64_t bytes, uint64_t ack) {
  if (w->ack_cap && ack > w->ack_cap - w->ack_inflight) return 0;
  if (!window_take(w, bytes)) return 0;
  w->ack_inflight += ack;
  return 1;
}

/* Caller holds the window lock. Fit the available charge, retaining at least two
 * independently completed pieces per full allowance when two 4 KiB pieces fit.
 * Avoid tiny residual writes unless they finish a request or the entire allowance
 * is smaller: otherwise packet/CQE overhead defeats the larger completion chunks. */
static uint32_t chunk_bytes(struct comm *c, uint32_t left) {
  uint32_t hi = left < g.chunk ? left : g.chunk, lo = 0;
  if (c->win->ack_cap) {
    uint64_t packet_bytes = (c->win->ack_cap - c->win->ack_inflight) / 128u * c->mtu_bytes;
    if (packet_bytes < hi) hi = (uint32_t)packet_bytes;
  }
  uint64_t cap = c->win->cap;
  uint64_t available = cap - c->win->inflight;
  uint64_t target = cap >= 2 * write_charge(c, 4096) ? cap / 2 : cap;
  if (available < target) target = available;
  /* Saturated channels are the common retry case: avoid a binary search when
   * even the smallest useful piece must wait for a completion. */
  uint32_t minimum = cap >= write_charge(c, 4096) ? (left < 4096 ? left : 4096) : 1;
  if (target < write_charge(c, minimum)) return 0;
  if (hi < minimum) return 0;
  if (write_charge(c, hi) <= target) return hi;
  while (lo < hi) {
    uint32_t mid = lo + (hi - lo + 1) / 2;
    if (write_charge(c, mid) <= target) lo = mid;
    else hi = mid - 1;
  }
  if (lo < left) lo &= ~63u;
  if (lo < 4096 && lo < left && cap >= write_charge(c, 4096)) return 0;
  return lo;
}

/* Reservation ownership includes the comm share before any provider call. */
static int admission_reserve(struct comm *c, uint64_t bytes, uint64_t ack) {
  if (!c->win) return 1;
  if (!window_take_ack(c->win, bytes, ack)) return 0;
  c->inflight += bytes;
  return 1;
}

/* Cancel only work the provider did not accept. Partial control pairs retain debt. */
static void admission_cancel(struct comm *c, uint64_t bytes, uint64_t ack) {
  if (!c->win) return;
  sircl_core_window_release(&c->win->inflight, bytes);
  c->win->ack_inflight -= ack;
  c->inflight -= bytes;
  c->win->error = ncclSystemError;
}

static void admission_complete(struct comm *c, uint64_t id) {
  if (!c->win) return;
  uint64_t bytes = WR_CHARGE(id);
  c->win->ack_inflight -= WR_ACK(id);
  c->inflight -= bytes;
  sircl_core_window_release(&c->win->inflight, bytes);
}

static void pending_append(struct window *w, struct req *q) {
  q->pending_next = NULL;
  if (w->pending_tail) w->pending_tail->pending_next = q;
  else w->pending_head = q;
  w->pending_tail = q;
}

/* FIFO requests, independent of CQ ownership. A blocked head retains its turn
 * until all its writes are posted. Both resources are acquired together by
 * the ordinary posting functions. Enqueueing reserves no credit; posted WRs
 * keep their charge until RC completion. A visit stops when credit is short. */
static ncclResult_t advance_pending(struct window *w) {
  while (w->pending_head) {
    struct req *q = w->pending_head;
    ncclResult_t r = q->is_send ? send_progress(q->c, q) : recv_progress(q->c, q);
    if (r != ncclSuccess) return r;
    int finished = q->is_send ? q->final_posted : q->cts_posted;
    if (!finished) return ncclSuccess;
    w->pending_head = q->pending_next;
    if (!w->pending_head) w->pending_tail = NULL;
    q->pending_next = NULL;
    if (profile_enabled) profile_wait(q->c, q, 0);
  }
  return ncclSuccess;
}

static ncclResult_t submit_progress(struct comm *c, struct req *q) {
  if (!c->win || !c->win->relay)
    return q->is_send ? send_progress(c, q) : recv_progress(c, q);
  pending_append(c->win, q);
  if (profile_enabled && q->is_send && c->win->pending_head != q) profile_wait(c, q, 1);
  return advance_pending(c->win);
}

/* Post a windowed comm's waiting payload or CTS writes, oldest request first. */
static ncclResult_t advance(struct comm *c) {
  for (uint32_t s = c->head; s != c->seq; ++s) {
    struct req *q = &c->reqs[s % NET_SIRCL_SLOTS];
    if (!q->used) continue;
    ncclResult_t r = q->is_send ? send_progress(c, q) : recv_progress(c, q);
    if (r != ncclSuccess) return r;
    if (q->is_send ? !q->final_posted : !q->cts_posted) break;
  }
  return ncclSuccess;
}

/* Caller holds the shared window lock across progress and request retirement. */
static ncclResult_t poll_cq(struct comm *c) {
  if (c->error) return c->error;
  if (!c->win) return drain(c);
  struct window *w = c->win;
  if (w->error) return w->error;
  for (struct comm *m = w->members; m; m = m->next) {
    /* Every relay CQE carries positive charge, including CTS and final pairs.
     * A zero balance proves there is nothing to reap; receive done words are
     * still checked by test_locked. Explicit direct windows have zero-charge
     * final CQEs and must retain unconditional polling. */
    ncclResult_t r = ncclSuccess;
    if (!w->relay || m->inflight) r = drain(m);
    if (r != ncclSuccess) return w->error = r;
    /* Reuse completed credit before polling unrelated CQs, granting it to
     * the oldest waiting request rather than preferentially to the CQ owner. */
    r = w->relay ? advance_pending(w) : advance(m);
    if (r != ncclSuccess) return w->error = r;
  }
  /* Rotate CQ visits; relay admission has its own FIFO order. */
  if (w->members && w->members->next) {
    struct comm *first = w->members, *last = first;
    while (last->next) last = last->next;
    w->members = first->next;
    first->next = NULL;
    last->next = first;
  }
  return ncclSuccess;
}

