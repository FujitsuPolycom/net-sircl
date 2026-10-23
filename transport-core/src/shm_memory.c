/* Shared-memory allocation and bounded mapping leases for the verbs emulator. */
#define _GNU_SOURCE
#include "shm_memory.h"
#include <sircl_core/shm_verbs.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
static pthread_mutex_t segments_lock = PTHREAD_MUTEX_INITIALIZER;
static sv_segment segments[SV_SEGMENTS];
static uint64_t segment_clock;
static pthread_cond_t segment_retired = PTHREAD_COND_INITIALIZER;

static void segment_tag(char *out, size_t len) {
  const char *tag = getenv("SIRCL_EMU_SEGMENT_TAG");
  if (tag && *tag) {
    snprintf(out, len, "%s", tag);
    return;
  }
  unsigned long long start = 0;
  char stat[1024] = {0};
  FILE *f = fopen("/proc/self/stat", "r");
  if (f) {
    size_t n = fread(stat, 1, sizeof stat - 1, f);
    stat[n] = 0;
    fclose(f);
    /* Field 2 (the command, in parentheses) may hold spaces: count fields after its closing parenthesis.
     * Field 3 follows it; field 22 is the 20th after. */
    char *p = strrchr(stat, ')');
    for (int field = 3; p && *p && field <= 22; ++field) {
      p = strchr(p + 1, ' ');
      if (p && field == 22) start = strtoull(p + 1, NULL, 10);
    }
  }
  snprintf(out, len, "%d-%llx", (int)getpid(), start);
}

void *sccl_emu_segment_alloc(size_t bytes, char *err, size_t err_len) {
  size_t page = (size_t)sysconf(_SC_PAGESIZE);
  bytes = (bytes + page - 1) / page * page;
  char tag[32], name[SV_NAME];
  segment_tag(tag, sizeof tag);
  /* A name that exists already belongs to a segment another process left (or holds): take the fabric's
   * next serial instead. It is never unlinked here; its owner may live in another PID namespace. */
  int fd = -1;
  for (int attempt = 0; attempt < 64; ++attempt) {
    uint32_t serial = sircl_emu_next_segment();
    if (!serial) { snprintf(err, err_len, "emulation fabric is not attached"); return NULL; }
    snprintf(name, sizeof name, "/sircl-emu-seg-%s-%u", tag, serial);
    fd = shm_open(name, O_RDWR | O_CREAT | O_EXCL, 0600);
    if (fd >= 0 || errno != EEXIST) break;
  }
  if (fd < 0) {
    snprintf(err, err_len, "emulation segment %s: shm_open: %s", name, strerror(errno));
    return NULL;
  }
  if (ftruncate(fd, (off_t)bytes) != 0) {
    snprintf(err, err_len, "emulation segment %s of %zu bytes: ftruncate: %s", name, bytes, strerror(errno));
    close(fd);
    shm_unlink(name);
    return NULL;
  }
  void *base = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  close(fd);
  if (base == MAP_FAILED) {
    snprintf(err, err_len, "emulation segment %s: mmap: %s", name, strerror(errno));
    shm_unlink(name);
    return NULL;
  }
  pthread_mutex_lock(&segments_lock);
  int slot = -1;
  for (int i = 0; i < SV_SEGMENTS; ++i)
    if (!segments[i].used) {
      slot = i;
      break;
    }
  if (slot < 0) {
    for (int i = 0; i < SV_SEGMENTS; ++i)
      if (!segments[i].owned && !segments[i].leases &&
          (slot < 0 || segments[i].touched < segments[slot].touched)) slot = i;
    if (slot >= 0) {
      munmap(segments[slot].base, segments[slot].bytes);
      memset(&segments[slot], 0, sizeof segments[slot]);
    }
  }
  if (slot < 0) {
    pthread_mutex_unlock(&segments_lock);
    munmap(base, bytes);
    shm_unlink(name);
    snprintf(err, err_len, "more than %d emulation segments in one process", SV_SEGMENTS);
    return NULL;
  }
  snprintf(segments[slot].name, SV_NAME, "%s", name);
  segments[slot].base = base;
  segments[slot].bytes = bytes;
  segments[slot].owned = 1;
  segments[slot].used = 1;
  pthread_mutex_unlock(&segments_lock);
  return base;
}

void sccl_emu_segment_free(void *base) {
  pthread_mutex_lock(&segments_lock);
  for (int i = 0; i < SV_SEGMENTS; ++i) {
    if (segments[i].used && segments[i].owned && segments[i].base == base) {
      while (segments[i].leases) pthread_cond_wait(&segment_retired, &segments_lock);
      munmap(segments[i].base, segments[i].bytes);
      shm_unlink(segments[i].name);
      segments[i].used = 0;
      break;
    }
  }
  pthread_mutex_unlock(&segments_lock);
}


int sircl_emu_owned_segment(uint64_t addr, uint64_t length, char *name, size_t len, uint64_t *offset) {
  int found = 0;
  pthread_mutex_lock(&segments_lock);
  for (int i = 0; i < SV_SEGMENTS; ++i) {
    sv_segment *s = &segments[i];
    uint64_t base = (uint64_t)(uintptr_t)s->base;
    if (s->used && s->owned && addr >= base && addr - base <= s->bytes && length <= s->bytes - (addr - base)) {
      snprintf(name, len, "%s", s->name);
      *offset = addr - base;
      found = 1;
      break;
    }
  }
  pthread_mutex_unlock(&segments_lock);
  return found;
}

/* This process's mapping of the named segment, mapped on first use. */
sv_segment *sircl_emu_mapping_acquire(const char *name) {
  pthread_mutex_lock(&segments_lock);
  for (int i = 0; i < SV_SEGMENTS; ++i) {
    if (segments[i].used && !strcmp(segments[i].name, name)) {
      segments[i].leases++;
      segments[i].touched = ++segment_clock;
      pthread_mutex_unlock(&segments_lock);
      return &segments[i];
    }
  }
  sv_segment *slot = NULL;
  for (int i = 0; i < SV_SEGMENTS; ++i) {
    sv_segment *candidate = &segments[i];
    if (!candidate->used) { slot = candidate; break; }
    if (!candidate->owned && !candidate->leases && (!slot || candidate->touched < slot->touched))
      slot = candidate;
  }
  if (!slot) { pthread_mutex_unlock(&segments_lock); return NULL; }
  int fd = shm_open(name, O_RDWR, 0600);
  struct stat st;
  void *base = MAP_FAILED;
  if (fd >= 0 && fstat(fd, &st) == 0 && st.st_size > 0)
    base = mmap(NULL, (size_t)st.st_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  if (fd >= 0) close(fd);
  if (base == MAP_FAILED) { pthread_mutex_unlock(&segments_lock); return NULL; }
  if (slot->used) munmap(slot->base, slot->bytes);
  memset(slot, 0, sizeof *slot);
  snprintf(slot->name, SV_NAME, "%s", name);
  slot->base = base;
  slot->bytes = (size_t)st.st_size;
  slot->used = 1;
  slot->leases = 1;
  slot->touched = ++segment_clock;
  pthread_mutex_unlock(&segments_lock);
  return slot;
}

void sircl_emu_mapping_release(sv_segment *mapping) {
  pthread_mutex_lock(&segments_lock);
  mapping->leases--;
  pthread_cond_broadcast(&segment_retired);
  pthread_mutex_unlock(&segments_lock);
}
