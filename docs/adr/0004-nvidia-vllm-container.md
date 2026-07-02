# ADR-0004: NVIDIA vLLM container runtime (not pip/venv)

**Date:** 2026-05-24
**Status:** Implemented

## Context

The GB10 (Blackwell, SM 12.1) requires a CUDA-compiled torch and vLLM that
understands SM 12.1 kernels. The standard pypi `torch` wheel for aarch64 is
CPU-only — a pip/venv install cannot serve models on this hardware.

## Options considered

**A. pip/venv from PyPI**
Attempted. The aarch64 torch wheel on PyPI has no CUDA support. Failed at
the first GPU operation. Ruled out.

**B. Build torch + vLLM from source**
Would produce a working binary, but requires maintaining a build environment,
tracking upstream CUDA kernel changes for SM 12.1, and rebuilding on every
vLLM update. Very high ongoing maintenance cost.

**C. NVIDIA's vLLM container (`nvcr.io/nvidia/vllm`)**
Ships matching torch + vLLM compiled for SM 12.1. NVIDIA maintains the build.
Updates are a `docker pull` + `make deploy`.

## Decision

NVIDIA container (option C), pinned to `26.04-py3`.

**Why 26.04 specifically:** SM 12.1 CUTLASS kernels were broken in the `26.03`
image (fixed in vLLM PR #38126). `26.03` caused ~40 CUDA traps during warmup
on GB10. `26.04` is the first stable image for this hardware.

## Consequences

- All CUDA-linked code runs inside the container. The host Python and any
  pip-installed packages are irrelevant to serving.
- Update path: bump `vllm_image` in `group_vars/all.yml`, pull the new image
  on both nodes (digests must match), then `make deploy`. The unit files
  change, so both services restart onto the new image.
- The container must be launched with `--cgroupns=host`; without it, NVML
  init fails ("Failed to initialize NVML: Unknown Error"). This is templated
  into the vllm role and must not be removed.
- Tied to NVIDIA's release cadence. New vLLM features (e.g., SM 12.1 FP4
  kernels landing in 26.05+) require a container bump to access.
