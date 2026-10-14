/* Native backend for nccl_e2e.py, using only the NCCL 2.23 public API. This avoids requiring an older
 * PyTorch build when its libtorch_cuda has hard references to newer NCCL symbols.
 * Compile with a CUDA toolkit's include/lib directories and the target NCCL build's include/lib. */
#include <cuda_runtime.h>
#include <nccl.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <chrono>
#include <string>
#include <vector>
#include <unistd.h>

#define CUDA(C) do { cudaError_t r = (C); if (r != cudaSuccess) { \
  fprintf(stderr, "%s: %s\n", #C, cudaGetErrorString(r)); exit(1); } } while (0)
#define NCCL(C) do { ncclResult_t r = (C); if (r != ncclSuccess) { \
  fprintf(stderr, "%s: %s\n", #C, ncclGetErrorString(r)); exit(1); } } while (0)

static unsigned pattern(size_t i, int rank, int replay = 0) { return (i % 7 + 1 + replay) * (rank + 1); }

static void check(const unsigned *data, size_t n, int rank, int replay, bool sum) {
  for (size_t i = 0; i < n; ++i) {
    unsigned want = sum ? pattern(i, 0, replay) * 3 : pattern(i, rank, replay);
    if (data[i] != want) {
      fprintf(stderr, "WRONG element %zu: %u != %u\n", i, data[i], want);
      exit(1);
    }
  }
}

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  setvbuf(stdout, NULL, _IOLBF, 0);
  alarm(850);
  int rank = atoi(argv[1]);
  ncclUniqueId id;
  if (rank == 0) {
    NCCL(ncclGetUniqueId(&id));
    std::string tmp = std::string(argv[2]) + ".tmp";
    FILE *f = fopen(tmp.c_str(), "wb");
    if (!f || fwrite(&id, sizeof id, 1, f) != 1 || fclose(f) || rename(tmp.c_str(), argv[2])) return 2;
  } else {
    FILE *f = NULL;
    while (!(f = fopen(argv[2], "rb"))) usleep(1000);
    if (fread(&id, sizeof id, 1, f) != 1 || fclose(f)) return 2;
  }
  int version;
  NCCL(ncclGetVersion(&version));
  printf("rank %d: NCCL runtime %d\n", rank, version);
  CUDA(cudaSetDevice(0));
  ncclComm_t comm;
  NCCL(ncclCommInitRank(&comm, 2, id, rank));
  cudaStream_t stream;
  CUDA(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking));
  const size_t sizes[] = {8u << 10, 64u << 10, 512u << 10, 4u << 20, 32u << 20, 64u << 20};
  for (size_t bytes : sizes) {
    size_t n = bytes / sizeof(unsigned);
    std::vector<unsigned> input(n), output(n * 2);
    for (size_t i = 0; i < n; ++i) input[i] = pattern(i, rank);
    unsigned *send, *recv;
    CUDA(cudaMalloc(&send, bytes)); CUDA(cudaMalloc(&recv, bytes * 2));
    CUDA(cudaMemcpyAsync(send, input.data(), bytes, cudaMemcpyHostToDevice, stream));
    NCCL(ncclAllReduce(send, recv, n, ncclUint32, ncclSum, comm, stream));
    CUDA(cudaMemcpyAsync(output.data(), recv, bytes, cudaMemcpyDeviceToHost, stream));
    CUDA(cudaStreamSynchronize(stream));
    check(output.data(), n, 0, 0, true);
    NCCL(ncclAllGather(send, recv, n, ncclUint32, comm, stream));
    CUDA(cudaMemcpyAsync(output.data(), recv, bytes * 2, cudaMemcpyDeviceToHost, stream));
    CUDA(cudaStreamSynchronize(stream));
    check(output.data(), n, 0, 0, false); check(output.data() + n, n, 1, 0, false);
    NCCL(ncclBroadcast(send, recv, n, ncclUint32, 1, comm, stream));
    CUDA(cudaMemcpyAsync(output.data(), recv, bytes, cudaMemcpyDeviceToHost, stream));
    CUDA(cudaStreamSynchronize(stream));
    check(output.data(), n, 1, 0, false);
    CUDA(cudaFree(send)); CUDA(cudaFree(recv));
    printf("rank %d: %zu B all-reduce/all-gather/broadcast exact\n", rank, bytes);
  }
  const size_t bytes = 64u << 10, n = bytes / sizeof(unsigned);
  unsigned *send, *reduce, *gather, *broadcast;
  CUDA(cudaMalloc(&send, bytes)); CUDA(cudaMalloc(&reduce, bytes));
  CUDA(cudaMalloc(&gather, bytes * 2)); CUDA(cudaMalloc(&broadcast, bytes));
  cudaGraph_t graph;
  cudaGraphExec_t exec;
  CUDA(cudaStreamBeginCapture(stream, cudaStreamCaptureModeGlobal));
  /* Multiple captured operations exercise proxy submission retries as well as
   * test() progress; each replay consumes far more than the relay allowance. */
  for (int batch = 0; batch < 20; ++batch) {
    NCCL(ncclAllReduce(send, reduce, n, ncclUint32, ncclSum, comm, stream));
    NCCL(ncclAllGather(send, gather, n, ncclUint32, comm, stream));
    NCCL(ncclBroadcast(send, broadcast, n, ncclUint32, 1, comm, stream));
  }
  CUDA(cudaStreamEndCapture(stream, &graph));
  CUDA(cudaGraphInstantiate(&exec, graph, 0));
  std::vector<unsigned> input(n), output(n * 4);
  for (int replay = 0; replay < 20; ++replay) {
    for (size_t i = 0; i < n; ++i) input[i] = pattern(i, rank, replay);
    CUDA(cudaMemcpyAsync(send, input.data(), bytes, cudaMemcpyHostToDevice, stream));
    CUDA(cudaGraphLaunch(exec, stream));
    CUDA(cudaMemcpyAsync(output.data(), reduce, bytes, cudaMemcpyDeviceToHost, stream));
    CUDA(cudaMemcpyAsync(output.data() + n, gather, bytes * 2, cudaMemcpyDeviceToHost, stream));
    CUDA(cudaMemcpyAsync(output.data() + n * 3, broadcast, bytes, cudaMemcpyDeviceToHost, stream));
    CUDA(cudaStreamSynchronize(stream));
    check(output.data(), n, 0, replay, true);
    check(output.data() + n, n, 0, replay, false);
    check(output.data() + n * 2, n, 1, replay, false);
    check(output.data() + n * 3, n, 1, replay, false);
  }
  printf("rank %d: graph 64 KiB, 20 captured batches x20 replays all-reduce/all-gather/broadcast exact\n", rank);
  CUDA(cudaGraphExecDestroy(exec)); CUDA(cudaGraphDestroy(graph));
  CUDA(cudaStreamSynchronize(stream));
  CUDA(cudaFree(send)); CUDA(cudaFree(reduce)); CUDA(cudaFree(gather)); CUDA(cudaFree(broadcast));
  /* Large captured all-reduces require credit recycling many times per replay.
   * Check each replay with changing input, including both in/out-of-place. */
  if (getenv("NET_SIRCL_TEST_LARGE_GRAPHS")) {
    for (size_t graph_bytes = 8u << 20; graph_bytes <= (256u << 20); graph_bytes *= 2) {
      size_t count = graph_bytes / sizeof(unsigned);
      std::vector<unsigned> graph_input(count), graph_output(count);
      CUDA(cudaMalloc(&send, graph_bytes)); CUDA(cudaMalloc(&reduce, graph_bytes));
      for (int inplace = 0; inplace < 2; ++inplace) {
        CUDA(cudaStreamBeginCapture(stream, cudaStreamCaptureModeGlobal));
        NCCL(ncclAllReduce(send, inplace ? send : reduce, count, ncclUint32, ncclSum, comm, stream));
        CUDA(cudaStreamEndCapture(stream, &graph));
        CUDA(cudaGraphInstantiate(&exec, graph, 0));
        for (int replay = 0; replay < 20; ++replay) {
          for (size_t i = 0; i < count; ++i) graph_input[i] = pattern(i, rank, replay);
          CUDA(cudaMemcpyAsync(send, graph_input.data(), graph_bytes, cudaMemcpyHostToDevice, stream));
          CUDA(cudaGraphLaunch(exec, stream));
          CUDA(cudaMemcpyAsync(graph_output.data(), inplace ? send : reduce, graph_bytes,
                               cudaMemcpyDeviceToHost, stream));
          CUDA(cudaStreamSynchronize(stream));
          check(graph_output.data(), count, 0, replay, true);
        }
        CUDA(cudaGraphExecDestroy(exec)); CUDA(cudaGraphDestroy(graph));
        printf("rank %d: graph %zu B x20 all-reduce %s exact\n", rank, graph_bytes,
               inplace ? "in-place" : "out-of-place");
      }
      CUDA(cudaFree(send)); CUDA(cudaFree(reduce));
    }
  }
  auto start = std::chrono::steady_clock::now();
  NCCL(ncclCommDestroy(comm));
  printf("rank %d: teardown %.2f s\n", rank,
         std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count());
  CUDA(cudaStreamDestroy(stream));
  printf("rank %d: PASS\n", rank);
  return 0;
}
