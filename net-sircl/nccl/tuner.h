/* NCCL v2.32.3-1 src/include/nccl_common.h function enum and tuner v4 ABI.
 * Copyright (c) 2017-2026 NVIDIA CORPORATION & AFFILIATES.
 * SPDX-License-Identifier: Apache-2.0. See LICENSE.txt and ../NOTICE. */
#ifndef SIRCL_NCCL_TUNER_H
#define SIRCL_NCCL_TUNER_H
#include "net.h"
typedef enum {
  ncclFuncBroadcast = 0, ncclFuncReduce = 1, ncclFuncAllGather = 2,
  ncclFuncReduceScatter = 3, ncclFuncAllReduce = 4, ncclFuncSendRecv = 5,
  ncclFuncSend = 6, ncclFuncRecv = 7, ncclFuncAlltoAll = 8,
  ncclFuncScatter = 9, ncclFuncGather = 10, ncclFuncAllGatherV = 11,
  ncclFuncPutSignal = 12, ncclFuncSignal = 13, ncclFuncWaitSignal = 14,
  ncclNumFuncs = 15
} ncclFunc_t;
#include "tuner_v4.h"
#endif
