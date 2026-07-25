# WAR (2026-07-03) for DEF-0005 (docs/defects.md): NVIDIA's nvcr.io/nvidia/vllm:26.06-py3 ships fastapi 0.137.1
# together with prometheus-fastapi-instrumentator 8.0.0 — an incompatible combo.
# FastAPI 0.137 refactored include_router() to store lazy `_IncludedRouter` wrappers
# in app.routes (no `.path`); the instrumentator's route-name resolution does a
# naive `route.path`, so it raises AttributeError and returns HTTP 500 on every
# non-excluded endpoint (/v1/models, /v1/chat/completions, ...). /health and /metrics
# are in excluded_handlers, so the server *looks* up but can't serve inference.
#
# Upstream vLLM guards against exactly this by capping fastapi<0.137
# (vllm-project/vllm#45596, #45597; fastapi#15791). This image applies that cap on
# top of NVIDIA's build so the 26.06 (NVFP4) profiles can actually serve.
#
# REMOVE WHEN: NVIDIA ships a 26.06+ image that already caps fastapi<0.137 or bundles
# a `_IncludedRouter`-aware prometheus-fastapi-instrumentator. Then repoint the 26.06
# profiles' vllm_image back to the stock tag and drop this entry from container_images.
#
# BUILT BY: the `images` role (ADR-0013) on every node, from this Dockerfile — no
# longer a manual `docker build`. Declared as a `build:` entry in container_images
# (group_vars/all.yml); the role stages this context to each node and builds it.
#
# FROM is pinned to the 26.06-py3 digest — the SAME digest the `images` role pulls as
# the base (container_images), so the build resolves to the already-present image with
# no registry round-trip and is byte-reproducible. Bump both together when 26.06 moves.
FROM nvcr.io/nvidia/vllm@sha256:6e537fc8d84beb9b60e789236168141d2ef4420bc28cbb85ec7d4318ca918b6c
RUN pip install --no-cache-dir 'fastapi<0.137'
