# ADR-0005: Native multinode via torch.distributed (no Ray)

**Date:** 2026-05-24
**Status:** Accepted

## Context

Tensor-parallel serving across two nodes requires a coordination mechanism.
The prior approach used Ray as vLLM's distributed backend. vLLM 0.19 dropped
Ray entirely in favour of native `torch.distributed` multinode.

## Options considered

**A. Pin to an older vLLM version that still supports Ray**
Keeps the existing unit files but forks from upstream. SM 12.1 CUTLASS fixes
are in recent images; pinning old means staying on broken kernels. Not viable.

**B. Ray on the new vLLM version**
Not possible — vLLM 0.19 has no Ray backend. The API is gone.

**C. Native torch.distributed multinode**
vLLM 0.19's built-in approach: `--nnodes / --node-rank / --master-addr /
--headless`. Both nodes rendezvous at a known TCP address. No Ray cluster
management, no Ray head/worker complexity.

## Decision

Native torch.distributed (option C).

## Consequences

- Simpler operational model: two systemd units (head on sparky, worker on
  snoopy), no Ray head/worker daemons, no Ray dashboard, no Ray tmp-dir
  mount footgun.
- Rendezvous at `10.0.200.12:29500` over ConnectX-7 (the 200 Gbit
  InfiniBand/RoCE link). `VLLM_HOST_IP` must be set to the ConnectX-7 IP on
  each node or vLLM advertises the wrong IP and rendezvous fails.
- Boot-order independence: snoopy's worker has `Restart=on-failure` /
  `RestartSec=10` and retries until sparky's rendezvous port is reachable.
  sparky's head has `TimeoutStartSec=1200` — 20 minutes for snoopy to join.
- `NCCL_SOCKET_IFNAME` and `NCCL_IB_HCA` pin NCCL to ConnectX-7 / RoCE;
  the 10GbE management NIC is never used for tensor-parallel traffic.
- Ray unit files and helper scripts are preserved in git history for reference
  but not in the working tree.
