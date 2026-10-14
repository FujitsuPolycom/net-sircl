/* Optional local observations. No wire fields or admission rules depend on this
 * profiler. The registry lock permits another proxy to progress a QP and permits
 * communicator teardown to snapshot threads that are still serving other comms.
 * Profiling deliberately trades overhead for complete, unsampled accounting. */
#include <inttypes.h>
#include <sys/syscall.h>

struct profile_hist { uint64_t bins[65], count, sum, min, max; };
struct profile_row {
  struct profile_row *next;
  uint64_t comm_id, bytes, wrs, data_bytes, data_wrs, polls, empty, errors;
  uint64_t waits, abandoned_waits, unmatched, pending;
  unsigned qp;
  int dev, peer, relay;
  char function[64];
  struct profile_hist sizes, completion_ns, wait_ns, abandoned_wait_ns;
  struct profile_hist cqes, credit_usable_ns, post_credit_ns;
  uint64_t *wait_samples;
  size_t wait_count, wait_capacity;
};
struct profile_thread {
  struct profile_thread *next;
  struct profile_row *rows;
  long tid;
  uint64_t first_wall, last_wall, first_cpu, last_cpu;
};
struct profile_stamp { uint64_t id, ns; };
struct profile_comm {
  uint64_t id, head, tail, seen;
  struct profile_stamp stamps[SEND_DEPTH];
};
static struct {
  pthread_mutex_t lock;
  char *prefix;
  uint64_t epoch, next_comm, lost;
  struct profile_thread *threads;
} profiler = {.lock = PTHREAD_MUTEX_INITIALIZER};
static int profile_enabled;

static uint64_t profile_clock(clockid_t clock) {
  struct timespec ts;
  if (clock_gettime(clock, &ts)) return 0;
  return (uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec;
}
static void profile_hist_add(struct profile_hist *h, uint64_t value) {
  unsigned b = value ? 64u - (unsigned)__builtin_clzll(value) : 0;
  h->bins[b]++;
  if (!h->count || value < h->min) h->min = value;
  if (value > h->max) h->max = value;
  h->count++;
  h->sum += value;
}
/* Called under profiler.lock, after the transport's function lock if any. */
static struct profile_row *profile_row(struct comm *c) {
  long tid = syscall(SYS_gettid);
  struct profile_thread *t = profiler.threads;
  while (t && t->tid != tid) t = t->next;
  if (!t) {
    t = calloc(1, sizeof *t);
    if (!t) { profiler.lost++; return NULL; }
    t->tid = tid;
    t->next = profiler.threads;
    profiler.threads = t;
  }
  t->last_wall = profile_clock(CLOCK_MONOTONIC);
  t->last_cpu = profile_clock(CLOCK_THREAD_CPUTIME_ID);
  if (!t->first_wall) { t->first_wall = t->last_wall; t->first_cpu = t->last_cpu; }
  if (!c->profile) {
    c->profile = calloc(1, sizeof *c->profile);
    if (!c->profile) { profiler.lost++; return NULL; }
    c->profile->id = ++profiler.next_comm;
  }
  struct profile_row *r = t->rows;
  while (r && r->comm_id != c->profile->id) r = r->next;
  if (!r) {
    r = calloc(1, sizeof *r);
    if (!r) { profiler.lost++; return NULL; }
    r->comm_id = c->profile->id;
    r->qp = c->qp->qp_num;
    r->dev = c->dev;
    r->peer = c->peer_position;
    r->relay = c->win && c->win->relay;
    snprintf(r->function, sizeof r->function, "%s", g.devs[c->dev].name);
    r->next = t->rows;
    t->rows = r;
  }
  return r;
}
static int profile_post(struct comm *c, uint64_t addr, uint32_t key, uint32_t len,
                        uint64_t remote, uint32_t rkey, int flags, uint64_t id) {
  /* Timestamp immediately before provider submission, including its call time. */
  uint64_t start = profile_clock(CLOCK_MONOTONIC);
  int rc = sircl_core_post_write(c->qp, addr, key, len, remote, rkey, flags, id);
  pthread_mutex_lock(&profiler.lock);
  struct profile_row *r = profile_row(c);
  if (r && !rc) {
    r->bytes += len; r->wrs++;
    if ((id >> 56) == WR_DATA) { r->data_bytes += len; r->data_wrs++; }
    if (r->relay) profile_hist_add(&r->sizes, len);
    if (flags & IBV_SEND_SIGNALED) {
      struct profile_comm *p = c->profile;
      if (p->tail - p->head < SEND_DEPTH) {
        p->stamps[p->tail++ % SEND_DEPTH] = (struct profile_stamp){id, start};
      } else { profiler.lost++; }
    }
  } else if (r) r->errors++;
  pthread_mutex_unlock(&profiler.lock);
  return rc;
}
static int profile_poll(struct comm *c, struct ibv_wc *wc) {
  int n = sircl_core_poll_cq(c->cq, 32, wc);
  uint64_t seen = profile_clock(CLOCK_MONOTONIC);
  pthread_mutex_lock(&profiler.lock);
  struct profile_row *r = profile_row(c);
  if (r) {
    c->profile->seen = seen;
    r->polls++;
    if (!n) r->empty++;
    if (n < 0) r->errors++;
    if (n > 0) profile_hist_add(&r->cqes, (uint64_t)n);
  }
  pthread_mutex_unlock(&profiler.lock);
  return n;
}
static void profile_complete(struct comm *c, uint64_t id) {
  /* Called immediately after credit release, before advancing other requests. */
  uint64_t usable = profile_clock(CLOCK_MONOTONIC);
  pthread_mutex_lock(&profiler.lock);
  struct profile_row *r = profile_row(c);
  if (r) {
    struct profile_comm *p = c->profile;
    if (p->head < p->tail && p->stamps[p->head % SEND_DEPTH].id == id) {
      uint64_t start = p->stamps[p->head++ % SEND_DEPTH].ns;
      if (r->relay) {
        if ((id >> 56) == WR_DATA) profile_hist_add(&r->completion_ns, p->seen - start);
        profile_hist_add(&r->credit_usable_ns, usable - p->seen);
        profile_hist_add(&r->post_credit_ns, usable - start);
      }
    } else r->unmatched++;
  }
  pthread_mutex_unlock(&profiler.lock);
}
static void profile_wait(struct comm *c, struct req *q, int blocked) {
  if (!c->win || !c->win->relay) return;
  pthread_mutex_lock(&profiler.lock);
  struct profile_row *r = profile_row(c);
  uint64_t now = profile_clock(CLOCK_MONOTONIC);
  if (r && blocked && !q->profile_wait_start) {
    q->profile_wait_start = now;
    q->profile_wait_row = r;
    r->waits++;
  } else if (!blocked && q->profile_wait_start) {
    struct profile_row *owner = q->profile_wait_row;
    uint64_t duration = now - q->profile_wait_start;
    profile_hist_add(&owner->wait_ns, duration);
    if (owner->wait_count == owner->wait_capacity) {
      size_t capacity = owner->wait_capacity ? owner->wait_capacity * 2 : 64;
      uint64_t *samples = realloc(owner->wait_samples, capacity * sizeof *samples);
      if (samples) { owner->wait_samples = samples; owner->wait_capacity = capacity; }
      else profiler.lost++;
    }
    if (owner->wait_count < owner->wait_capacity) owner->wait_samples[owner->wait_count++] = duration;
    q->profile_wait_start = 0;
  }
  pthread_mutex_unlock(&profiler.lock);
}
static void profile_string(FILE *f, const char *s) {
  fputc('"', f);
  for (; *s; s++) {
    unsigned char ch = (unsigned char)*s;
    if (ch < 32 || ch == '"' || ch == '\\') fprintf(f, "\\u%04x", ch);
    else fputc(ch, f);
  }
  fputc('"', f);
}
static void profile_hist_json(FILE *f, const char *name, const struct profile_hist *h) {
  fprintf(f, ",\"%s\":{\"count\":%" PRIu64 ",\"sum\":%" PRIu64
          ",\"min\":%" PRIu64 ",\"max\":%" PRIu64 ",\"log2_bins\":[",
          name, h->count, h->sum, h->min, h->max);
  for (int i = 0; i < 65; i++) fprintf(f, "%s%" PRIu64, i ? "," : "", h->bins[i]);
  fputs("]}", f);
}
/* Snapshot at every network communicator close: v7-v10 have no finalize hook.
 * Each thread has one cumulative file per initialization epoch; atomic rename
 * replaces earlier snapshots. No file IO or formatting occurs during progress. */
static void profile_flush(void) {
  pthread_mutex_lock(&profiler.lock);
  char host[256] = "unknown";
  (void)gethostname(host, sizeof host - 1);
  for (char *s = host; *s; s++) if (*s == '/' || *s == '\\') *s = '_';
  for (struct profile_thread *t = profiler.threads; t; t = t->next) {
    char *path = NULL, *tmp = NULL;
    if (asprintf(&path, "%s.%s.%ld.%" PRIu64 ".%ld.json", profiler.prefix, host,
                 (long)getpid(), profiler.epoch, t->tid) < 0) continue;
    if (asprintf(&tmp, "%s.tmp", path) < 0) { free(path); continue; }
    FILE *f = fopen(tmp, "w");
    if (!f) { WARN("net-sircl: profile output %s: %s", tmp, strerror(errno)); goto next; }
    fprintf(f, "{\"schema\":1,\"rank\":%d,\"pid\":%ld,\"tid\":%ld,\"host\":",
            g.position, (long)getpid(), t->tid);
    profile_string(f, host);
    fprintf(f, ",\"epoch\":%" PRIu64 ",\"wall_ns\":%" PRIu64 ",\"thread_cpu_ns\":%" PRIu64
            ",\"lost_observations\":%" PRIu64 ",\"remote_completion_to_credit_ns\":null,\"rows\":[",
            profiler.epoch, t->last_wall - t->first_wall, t->last_cpu - t->first_cpu, profiler.lost);
    int comma = 0;
    for (struct profile_row *r = t->rows; r; r = r->next) {
      fprintf(f, "%s{\"function\":", comma++ ? "," : ""); profile_string(f, r->function);
      fprintf(f, ",\"comm_id\":%" PRIu64 ",\"qp\":%u,\"peer\":%d,\"relay\":%d",
              r->comm_id, r->qp, r->peer, r->relay);
#define PROFILE_FIELD(field) fprintf(f, ",\"" #field "\":%" PRIu64, r->field)
      PROFILE_FIELD(bytes); PROFILE_FIELD(wrs); PROFILE_FIELD(data_bytes); PROFILE_FIELD(data_wrs);
      PROFILE_FIELD(polls); PROFILE_FIELD(empty); PROFILE_FIELD(errors); PROFILE_FIELD(waits);
      PROFILE_FIELD(abandoned_waits); PROFILE_FIELD(unmatched); PROFILE_FIELD(pending);
#undef PROFILE_FIELD
#define PROFILE_HIST(field) profile_hist_json(f, #field, &r->field)
      PROFILE_HIST(sizes); PROFILE_HIST(completion_ns); PROFILE_HIST(wait_ns);
      PROFILE_HIST(abandoned_wait_ns); PROFILE_HIST(cqes); PROFILE_HIST(credit_usable_ns); PROFILE_HIST(post_credit_ns);
#undef PROFILE_HIST
      fputs(",\"wait_samples_ns\":[", f);
      for (size_t i = 0; i < r->wait_count; i++) fprintf(f, "%s%" PRIu64, i ? "," : "", r->wait_samples[i]);
      fputc(']', f);
      fputc('}', f);
    }
    fputs("]}\n", f);
    int failed = ferror(f);
    if (fclose(f)) failed = 1;
    if (failed || rename(tmp, path)) WARN("net-sircl: cannot finish profile %s", path);
next:
    free(tmp); free(path);
  }
  pthread_mutex_unlock(&profiler.lock);
}
static void profile_close(struct comm *c) {
  pthread_mutex_lock(&profiler.lock);
  uint64_t now = profile_clock(CLOCK_MONOTONIC);
  for (int i = 0; i < NET_SIRCL_SLOTS; i++) {
    struct req *q = &c->reqs[i];
    if (q->profile_wait_start) {
      q->profile_wait_row->abandoned_waits++;
      profile_hist_add(&q->profile_wait_row->abandoned_wait_ns, now - q->profile_wait_start);
    }
  }
  if (c->profile) {
    /* Attribute abandoned stamps to a row already present, without using a
     * destroyed QP or fabricating activity on the teardown thread. */
    for (struct profile_thread *t = profiler.threads; t; t = t->next)
      for (struct profile_row *r = t->rows; r; r = r->next)
        if (r->comm_id == c->profile->id) {
          r->pending += c->profile->tail - c->profile->head;
          c->profile->head = c->profile->tail;
        }
    free(c->profile);
    c->profile = NULL;
  }
  pthread_mutex_unlock(&profiler.lock);
  profile_flush();
}
static void profile_reset(void) {
  /* Initialization/finalization occurs while transport global lock is held. */
  while (profiler.threads) {
    struct profile_thread *t = profiler.threads;
    profiler.threads = t->next;
    while (t->rows) { struct profile_row *r = t->rows; t->rows = r->next; free(r->wait_samples); free(r); }
    free(t);
  }
  free(profiler.prefix); profiler.prefix = NULL;
  profile_enabled = 0;
  profiler.lost = profiler.next_comm = 0;
}
static void profile_init(void) {
  profile_reset();
  const char *prefix = getenv("NET_SIRCL_PROFILE");
  if (prefix && *prefix) profiler.prefix = strdup(prefix);
  profile_enabled = profiler.prefix != NULL;
  if (profile_enabled) profiler.epoch = profile_clock(CLOCK_REALTIME);
}
