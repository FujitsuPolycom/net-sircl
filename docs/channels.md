# Automatic collective channel policy

The library exports NVIDIA's tuner v4 interface alongside ext-net v7–v12.
When no external tuner is selected, NCCL discovers the tuner in its loaded
network-plugin library. No additional library or environment entry is required.
For a multi-node communicator using a topology of more than two hosts, the
tuner supplies an eight-channel limit per collective. Two-host topologies retain
NCCL's default tuning. Algorithm and protocol cost tables remain unchanged.

The tuner API does not expose peer positions. Its decision therefore uses the
complete topology on every rank, including relay-only hosts. A two-rank subset
of a larger topology still gets eight channels; a full cycle also gets eight
even when its Ring edges are direct. It cannot selectively tune individual
edges within one collective. Use identical configuration on every rank.

Any explicit `NCCL_MIN_NCHANNELS`, `NCCL_MAX_NCHANNELS`, `NCCL_MIN_NRINGS`,
`NCCL_MAX_NRINGS`, `NCCL_MIN_CTAS` or `NCCL_MAX_CTAS` disables this tuner limit;
NCCL owns those entries, including empty/invalid values. `NET_SIRCL_CHANNELS`
selects 1–64 channels or 0 to leave selection to NCCL; NCCL's environment entries
take precedence. `NCCL_TUNER_PLUGIN=none` disables the tuner, and an explicitly
selected external tuner takes precedence through NCCL's loader.

NCCL still clamps operation channel counts to available communicator channels.
The tuner does not see `ncclConfig_t` or per-call CTA policy: applications relying
on an explicit minimum CTA count should disable it with `NET_SIRCL_CHANNELS=0`
or `NCCL_TUNER_PLUGIN=none`. The limit applies to collective operations, not
Send/Recv, communicator allocation, or a host-wide sum of concurrent operations.
INIT may still print 64 allocated channels. With `NCCL_DEBUG=INFO` and
`NCCL_DEBUG_SUBSYS=INIT,NET,TUNING`, look for the tuner policy and NCCL 2.32.3's
operation `channel{Lo..Hi}` range; count both endpoints. Smaller operations may
use fewer than eight. `scripts/check_channels.py` checks these actual ranges.

## Why this interface

Source reviewed: NVIDIA NCCL tag **v2.32.3-1**, Apache-2.0/BSD-3 attribution in
[NOTICE](../NOTICE); no NCCL code is patched.

- [Tuner loader](https://github.com/NVIDIA/nccl/blob/v2.32.3-1/src/plugin/tuner.cc)
  falls back to the network library and negotiates v6 down through v2.
- [Tuner v4 contract](https://github.com/NVIDIA/nccl/blob/v2.32.3-1/src/include/plugin/tuner/tuner_v4.h)
  permits supplying channels without changing algorithm or protocol costs.
- [Tuning](https://github.com/NVIDIA/nccl/blob/v2.32.3-1/src/tuning/tuning.cc)
  uses that output as `maxChannels`;
  [general tuning](https://github.com/NVIDIA/nccl/blob/v2.32.3-1/src/tuning/tuning_general.cc)
  and [enqueue](https://github.com/NVIDIA/nccl/blob/v2.32.3-1/src/enqueue/enqueue.cc)
  bound operation scheduling with it.
- [Topology construction](https://github.com/NVIDIA/nccl/blob/v2.32.3-1/src/graph/topo.cc)
  reads `maxComms` as connection capacity. The
  [postset channel calculation](https://github.com/NVIDIA/nccl/blob/v2.32.3-1/src/graph/connect.cc)
  also duplicates channels and applies communicator/environment bounds. Reporting
  a fictitious connection capacity is not a supported per-operation channel cap.
- NCCL caches environment parameters. Mutating `NCCL_MAX_NCHANNELS` inside the
  plugin is not a documented initialization contract and would affect unrelated
  communicators, so the plugin never writes NCCL's environment.

This is an emulation-tested default, not a new hardware performance result.
The 0.1.1 direct-pair cap-16 cost is why a two-host topology keeps stock tuning.
Compare automatic, explicit cap-8 and stock arms before hardware qualification.
