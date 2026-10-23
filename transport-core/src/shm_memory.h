/* Private ownership contract for emulator shared-memory mappings. */
#ifndef SIRCL_EMU_MEMORY_H
#define SIRCL_EMU_MEMORY_H
#include <stddef.h>
#include <stdint.h>
#define SV_NAME 64
#define SV_SEGMENTS 512
typedef struct {
  char name[SV_NAME];
  uint8_t *base;
  size_t bytes;
  int owned;
  int used;
  unsigned leases;
  uint64_t touched;
} sv_segment;
/* The registry supplies globally unique segment serials; zero means unattached. */
uint32_t sircl_emu_next_segment(void);
int sircl_emu_owned_segment(uint64_t, uint64_t, char *, size_t, uint64_t *);
sv_segment *sircl_emu_mapping_acquire(const char *);
void sircl_emu_mapping_release(sv_segment *);
#endif
