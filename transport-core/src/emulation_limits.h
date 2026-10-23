/* Finite registry envelope for eight local ranks with NCCL connection pools. */
#ifndef SIRCL_EMULATION_LIMITS_H
#define SIRCL_EMULATION_LIMITS_H
#define SIRCL_EMU_MAX_RANKS 8
#define SIRCL_EMU_LOCAL_CONNECTIONS 1024
#define SV_LOCAL_QPS SIRCL_EMU_LOCAL_CONNECTIONS
#define SV_LOCAL_CQS SIRCL_EMU_LOCAL_CONNECTIONS
#define SV_MAX_QPS (SIRCL_EMU_MAX_RANKS * SIRCL_EMU_LOCAL_CONNECTIONS)
/* Control, send, receive and staging registrations can coexist per connection. */
#define SV_MAX_MRS (4 * SV_MAX_QPS)
/* CPU lifetime tests override the cache bound to exercise eviction cheaply. */
#ifndef SV_SEGMENTS
#define SV_SEGMENTS (4 * SIRCL_EMU_LOCAL_CONNECTIONS)
#endif
#endif
