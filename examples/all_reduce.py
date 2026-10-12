"""One GPU per host; launch with torchrun and the README's plugin environment."""
import torch
import torch.distributed as dist

torch.cuda.set_device(0)
dist.init_process_group("nccl", device_id=torch.device("cuda", 0))
rank, world = dist.get_rank(), dist.get_world_size()
value = torch.full((4096,), rank + 1, device="cuda", dtype=torch.float32)
dist.all_reduce(value)
assert torch.all(value == world * (world + 1) / 2).item()
torch.cuda.synchronize()
dist.destroy_process_group()
print(f"rank {rank}: PASS", flush=True)
