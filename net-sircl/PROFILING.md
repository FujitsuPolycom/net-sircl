# Local proxy profiling

Set `NET_SIRCL_PROFILE=/existing/writable/directory/job-prefix` before plugin
initialization. An unset or empty value disables profiling. The production and
stand-in libraries use the same instrumentation; no build flag is needed.

Each posting/polling/credit-wait thread gets one JSON file, named with the prefix,
hostname, PID, initialization epoch and Linux TID. Network communicator close
atomically replaces cumulative snapshots; plugin finalize writes them again.
This supports ABIs 7-10, which have no finalize callback. An abnormal exit can
leave earlier snapshots or no file. The directory must exist. Output failures
warn through NCCL's logger and do not alter transport results.

```sh
# From the repository root:
python3 net-sircl/tools/summarize_profile.py '/logs/job-prefix.*.json' --json merged.json
```

Pass files from exactly one job. The script rejects duplicate snapshots and
lost observations, and merges by topology position (`rank`) and RDMA function.
The input retains QP and unique communicator instance IDs (QP numbers can be
reused). Thread CPU/wall time is separate, never duplicated per function.
Histogram bin 0 holds zero; bin b>0 holds `[2**(b-1), 2**b-1]`. Durations are
nanoseconds. Counts, sums, minima and maxima are exact; histogram quantiles
are approximate.

| Field | Meaning |
|---|---|
| `bytes`, `wrs`, `data_bytes`, `data_wrs` | Successful posts, including control traffic in the first pair; attributed to posting thread/function/QP |
| `sizes` | Size of every successful relayed WR, including unsignaled control writes |
| `completion_ns` | Each relayed payload chunk: immediately before provider submission to return of the poll observing its successful CQE |
| `waits`, `wait_ns`, `wait_samples_ns` | Accepted send cannot admit payload or final control pair until admission succeeds. Consecutive retries extend one episode. Histogram and individual durations belong to the thread starting the wait; episodes may overlap across requests |
| `abandoned_waits`, `abandoned_wait_ns` | Waits still open at close, censored at close, separate from successful admissions |
| `polls`, `empty`, `cqes` | Provider calls, zero-result calls, histogram of CQEs per nonempty call |
| `credit_usable_ns` | Poll return observing a CQE to credit release; includes earlier CQE processing in the batch, excludes scheduling until another request consumes credit |
| `post_credit_ns` | Submission to credit release for each relayed signaled write/control pair |
| `remote_completion_to_credit_ns` | Null: ordinary verbs CQEs have no remote timestamp. The CQ-observed interval is a lower bound, not a measurement of the physical interval |
| `thread_cpu_ns`, `wall_ns` | CLOCK_THREAD_CPUTIME_ID and CLOCK_MONOTONIC deltas between first and last observed operation on the thread; include intervening NCCL work/descheduling. Close creates no activity on its caller |
| `pending`, `unmatched`, `errors`, `lost_observations` | Signaled posts abandoned at close; unmatched successful CQEs; failed provider post/poll calls; allocation/timestamp-queue losses. Inspect nonzero values before interpreting a job |

Each chunk has a FIFO timestamp even when WRs reuse a slot ID. RC send-CQE
ordering permits a different proxy thread to reap the queue. Polls and completion
samples belong to the observing thread. The profiler changes no wire field,
verbs flag, request ID, admission charge or CUDA operation. CTS handshake waits
are not relay-credit waits and are excluded.

Disabled observations cost one predictable flag branch per post, poll, credit
release or send-admission observation: no clocks, allocations, registry locks,
file creation or environment reads on progress. Enabled observations use clocks,
a registry mutex, first-use allocation and a growing array of wait samples. The
mutex serializes observations across threads; CPU time includes profiler work.
Measure matching enabled/disabled jobs before interpreting performance. Teardown
file output can increase whole-job wall time. Memory scales with threads, QPs
and waits; timestamp storage is bounded by SEND_DEPTH per QP. This is an
unsampled diagnostic, not a low-overhead hardware tracer.

Remote completion cannot be timed precisely without another timestamp source
or a protocol change. Do not subtract clocks on separate hosts or label
post-to-CQE as wire latency.
