/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 SparkRing contributors. Shared RC primitives; protocol-neutral.
 * Include a provider's symbol mapping before this header for emulation. */
#ifndef SIRCL_CORE_VERBS_H
#define SIRCL_CORE_VERBS_H
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <infiniband/verbs.h>

struct sircl_core_qp_peer {
  uint8_t gid[16];
  uint32_t qpn, mtu, flow_label;
  uint16_t lid;
  uint8_t port, gid_index, traffic_class, hop_limit;
};

static inline struct ibv_context *sircl_core_open_named_device(const char *name) {
  int n = 0;
  struct ibv_device **list = ibv_get_device_list(&n);
  if (!list) return NULL;
  struct ibv_context *ctx = NULL;
  int found = 0;
  for (int i = 0; i < n; ++i) if (!strcmp(ibv_get_device_name(list[i]), name)) {
    found = 1;
    ctx = ibv_open_device(list[i]);
    break;
  }
  int saved = found ? errno : ENODEV;
  ibv_free_device_list(list);
  errno = saved;
  return ctx;
}
static inline int sircl_core_query_port(struct ibv_context *ctx, unsigned port,
                                        struct ibv_port_attr *attr) {
  return ibv_query_port(ctx, (uint8_t)port, attr);
}
static inline int sircl_core_query_gid(struct ibv_context *ctx, unsigned port,
                                       int index, union ibv_gid *gid) {
  return ibv_query_gid(ctx, (uint8_t)port, index, gid);
}
static inline struct ibv_pd *sircl_core_alloc_pd(struct ibv_context *ctx) {
  return ibv_alloc_pd(ctx);
}
static inline struct ibv_mr *sircl_core_reg_mr(struct ibv_pd *pd, void *p, size_t n, int access) {
  return ibv_reg_mr(pd, p, n, access);
}
static inline struct ibv_cq *sircl_core_create_cq(struct ibv_context *ctx, int depth) {
  return ibv_create_cq(ctx, depth, NULL, NULL, 0);
}
static inline struct ibv_qp *sircl_core_create_rc_qp(struct ibv_pd *pd, struct ibv_cq *cq,
                                                    unsigned depth, unsigned inline_bytes) {
  struct ibv_qp_init_attr attr;
  memset(&attr, 0, sizeof attr);
  attr.send_cq = attr.recv_cq = cq;
  attr.qp_type = IBV_QPT_RC;
  attr.cap.max_send_wr = depth;
  attr.cap.max_recv_wr = 1;
  attr.cap.max_send_sge = attr.cap.max_recv_sge = 1;
  attr.cap.max_inline_data = inline_bytes;
  return ibv_create_qp(pd, &attr);
}
static inline int sircl_core_qp_init(struct ibv_qp *qp, unsigned port, int access) {
  struct ibv_qp_attr a;
  memset(&a, 0, sizeof a);
  a.qp_state = IBV_QPS_INIT;
  a.port_num = (uint8_t)port;
  a.qp_access_flags = access;
  return ibv_modify_qp(qp, &a, IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS);
}
static inline int sircl_core_qp_rtr(struct ibv_qp *qp, const struct sircl_core_qp_peer *p) {
  struct ibv_qp_attr a;
  memset(&a, 0, sizeof a);
  a.qp_state = IBV_QPS_RTR;
  a.path_mtu = (enum ibv_mtu)p->mtu;
  a.dest_qp_num = p->qpn;
  a.max_dest_rd_atomic = 1;
  a.min_rnr_timer = 12;
  a.ah_attr.is_global = 1;
  a.ah_attr.dlid = p->lid;
  a.ah_attr.port_num = p->port;
  memcpy(a.ah_attr.grh.dgid.raw, p->gid, 16);
  a.ah_attr.grh.sgid_index = p->gid_index;
  a.ah_attr.grh.hop_limit = p->hop_limit;
  a.ah_attr.grh.traffic_class = p->traffic_class;
  a.ah_attr.grh.flow_label = p->flow_label;
  return ibv_modify_qp(qp, &a, IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN |
                              IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER);
}
static inline int sircl_core_qp_rts(struct ibv_qp *qp, unsigned timeout, unsigned retry) {
  struct ibv_qp_attr a;
  memset(&a, 0, sizeof a);
  a.qp_state = IBV_QPS_RTS;
  a.timeout = (uint8_t)timeout;
  a.retry_cnt = (uint8_t)retry;
  a.rnr_retry = 7; /* Receiver-not-ready retries are independent of the ACK retry budget. */
  a.max_rd_atomic = 1;
  return ibv_modify_qp(qp, &a, IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY |
                              IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC);
}
static inline int sircl_core_post_send(struct ibv_qp *qp, struct ibv_send_wr *wr, struct ibv_send_wr **bad) {
  return ibv_post_send(qp, wr, bad);
}
static inline int sircl_core_poll_cq(struct ibv_cq *cq, int count, struct ibv_wc *wc) {
  return ibv_poll_cq(cq, count, wc);
}
static inline int sircl_core_post_write(struct ibv_qp *qp, uint64_t local, uint32_t lkey, uint32_t bytes,
                                        uint64_t remote, uint32_t rkey, int flags, uint64_t id) {
  struct ibv_sge sge = {.addr = local, .length = bytes, .lkey = lkey};
  struct ibv_send_wr wr, *bad = NULL;
  memset(&wr, 0, sizeof wr);
  wr.wr_id = id;
  wr.opcode = IBV_WR_RDMA_WRITE;
  wr.sg_list = bytes ? &sge : NULL;
  wr.num_sge = bytes ? 1 : 0;
  wr.send_flags = flags;
  wr.wr.rdma.remote_addr = remote;
  wr.wr.rdma.rkey = rkey;
  return sircl_core_post_send(qp, &wr, &bad);
}
static inline int sircl_core_destroy_qp(struct ibv_qp *qp) { return ibv_destroy_qp(qp); }
static inline int sircl_core_destroy_cq(struct ibv_cq *cq) { return ibv_destroy_cq(cq); }
static inline int sircl_core_dereg_mr(struct ibv_mr *mr) { return ibv_dereg_mr(mr); }
static inline int sircl_core_dealloc_pd(struct ibv_pd *pd) { return ibv_dealloc_pd(pd); }
static inline int sircl_core_close_device(struct ibv_context *ctx) { return ibv_close_device(ctx); }
#endif
