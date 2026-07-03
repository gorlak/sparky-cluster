# WAR (2026-07-03): NVIDIA's nvcr.io/nvidia/vllm:26.06-py3 ships fastapi 0.137.1
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
# profiles' vllm_image back to the stock tag and delete this image.
#
# Build on BOTH nodes (digests need not match — it's a thin local layer over the
# shared base, which IS pinned):
#   sudo docker build -t dgx-spark/vllm:26.06-fastapi-fix docker/vllm-26.06-fastapi-fix
FROM nvcr.io/nvidia/vllm:26.06-py3
RUN pip install --no-cache-dir 'fastapi<0.137'
