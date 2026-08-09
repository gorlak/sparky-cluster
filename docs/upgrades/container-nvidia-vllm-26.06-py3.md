# Upgrade: `nvcr.io/nvidia/vllm` → `26.06-py3` (NVFP4 enablement)

**Status:** 🟡 In progress — blocked on upstream issue closure
**Current pin:** `nvcr.io/nvidia/vllm:26.04-py3` (vLLM 0.19.0, NCCL 2.29.7) — stable
**Target:** `26.06-py3`+ (vLLM 0.22.1, NCCL 2.30.5) — required for NVFP4
**Last updated:** 2026-07-02

> NVIDIA NGC images are calendar-versioned `YY.MM`, so `26.06-py3` = the **June 2026**
> build of `nvcr.io/nvidia/vllm` (Python 3). It is *not* a vLLM version — 26.04 ships
> vLLM 0.19; 26.06 ships 0.22.1.

> **2026-08-08: `26.07-py3` exists**, and is the live candidate — see
> [container-nvidia-vllm-26.07-py3.md](container-nvidia-vllm-26.07-py3.md). This tracker
> remains the record of how the NVFP4 profiles got running on 26.06, and its WAR register
> is still the authority; the 26.07 tracker re-evaluates each WAR rather than restating it.

This is a **living tracker**, not a decision record. The upgrade is underway and
paused on specific upstream bugs; when the completion criteria below are met we
re-attempt it. (It is deliberately *not* an ADR — nothing is settled to make
immutable; we are waiting, not deciding against.)

---

## Why migrate at all

NVFP4 quantizations (Step-3.7-Flash-NVFP4, MiniMax NVFP4, …) need the b12x SM12.1
FP4 kernels merged to vLLM main on 2026-05-20 (PR #40082), first shipped in a
container ≥ 26.05/26.06. **26.04 predates them.** NVFP4 roughly halves the per-node
weight footprint vs FP8, which unlocks big-shared-with-headroom profiles on this
2× GB10 cluster (see `docs/models/step-3.7-flash.md`). So the target container is a
prerequisite for the whole NVFP4 line of work.

## What happened (2026-07-02)

1. Bumped the pin to `26.06-py3`, pulled on both nodes (digests matched), deployed
   `minimax-m2.7-awq` at TP=2.
2. **Both nodes hard-hung during multinode bring-up** — SSH-unresponsive, required
   a hard reset of both machines. (Same failure *class* as the earlier
   "Qwen3.5-122B froze sparky" lockup.)
3. Root-caused to a **NCCL 2.30.4+ NVLS regression on GB10** (see blockers). The
   version boundary matches our own logs exactly: 26.04's NCCL 2.29.7 works;
   26.06's NCCL 2.30.5 hangs.
4. Rolled back to 26.04 (stable; `minimax-m2.7-awq` running).
5. Implemented **fail-safe boot (ADR-0009)** so a *future* failed retry lands both
   nodes empty-and-reachable instead of re-hanging on every boot. This is what
   makes re-attempting 26.06 safe rather than reckless.
6. Staged the **NVLS killswitch** (`NCCL_NVLS_ENABLE=0`, see
   `roles/common/files/nccl-env.conf`) — harmless on 26.04, likely necessary on 26.06.

## Blocking upstream issues

Status as of **2026-07-02** — all open and un-triaged unless noted. Re-check these
when re-assessing.

| Issue | Scope | State | Triaged? | Workaround we control |
|---|---|---|---|---|
| [NVIDIA/nccl#2167](https://github.com/NVIDIA/nccl/issues/2167) | **Our 26.06 hang** — NCCL 2.30.4+ NVLS load-time hang on GB10 (no NVLink). 2.29.7/2.30.3 work; 2.30.4+ hang | Open | No label, no NVIDIA reply | **`NCCL_NVLS_ENABLE=0`** (staged) |
| [vllm#41725](https://github.com/vllm-project/vllm/issues/41725) | Inference-time TP=2 CUDA deadlock, 2× Spark + MiniMax, 35–55 min in | Open | No label, no maintainer reply | none confirmed — PP=2 sidesteps (latency cost) |
| [vllm#40969](https://github.com/vllm-project/vllm/issues/40969) | GB10 cudagraph inference hang (DeepSeek-V4, `FULL_AND_PIECEWISE`) | Open | Label `DSv4` only | `cudagraph_mode: PIECEWISE` / `--enforce-eager` |
| [vllm#33041](https://github.com/vllm-project/vllm/issues/33041) | Older TP=2-on-Blackwell **init** hang (NCCL 2.27.7) | Closed | Labeled `bug` | resolution unclear |

These are a *family* of distinct-but-related GB10-multinode hangs (load-time,
init-time, inference-time, cudagraph-time), each with its own trigger. Do not
assume fixing one fixes the others.

## Workarounds (WARs) — the removal register

Each row is a temporary patch tied to an upstream bug — **debt to remove when the bug is
fixed, not permanent config.** On any future container bump, walk this table: for every
WAR whose *Remove when* condition now holds, drop it and **re-test one at a time** so you
know which was still load-bearing (pulling several at once hides that).

| WAR | Fixes (symptom) | Upstream | Cost | Applied in | Remove when | Status |
|---|---|---|---|---|---|---|
| **DEF-0001** · `NCCL_NVLS_ENABLE=0` | NCCL-init hard hang at TP=2 bring-up | [nccl#2167](https://github.com/NVIDIA/nccl/issues/2167) | none — NVLS is unusable without NVLink | `roles/common/files/nccl-env.conf` | #2167 fixed **or** a container ships NCCL ≥ 2.30.6 with the NVLS regression reverted | ✅ applied — confirmed (cleared NCCL init 2026-07-02) |
| `--enforce-eager` | *(hypothesis: compile-phase hang)* | — | throughput | — | — | ❌ **ruled out** (2026-07-02) — hung *identically* at Marlin-MoE weight load with compile+cudagraphs disabled, so the hang is below the compile layer |
| **DEF-0003** · `cudagraph_mode: PIECEWISE` | cudagraph inference hang | [vllm#40969](https://github.com/vllm-project/vllm/issues/40969) | some throughput | per-profile args | #40969 fixed | ⚪ candidate (only relevant once a model gets *past* load and serves) |
| **DEF-0002** · PP=2 instead of TP=2 | inference-time compute deadlock | [vllm#41725](https://github.com/vllm-project/vllm/issues/41725) | single-stream latency (pipeline bubbles) | profile `pipeline_parallel_size` | #41725 fixed | ⚪ candidate — architectural fallback |
| **DEF-0005** · **Patched image**: pin `fastapi<0.137` (built `FROM` 26.06) | HTTP **500 on every `/v1/*` request** — `prometheus_fastapi_instrumentator` 8.0.0 crashes on FastAPI 0.137's `_IncludedRouter` (no `.path`); `/health`+`/metrics` excluded so it *looks* up | [vllm#45596](https://github.com/vllm-project/vllm/issues/45596), [#45597](https://github.com/vllm-project/vllm/issues/45597), [fastapi#15791](https://github.com/fastapi/fastapi/discussions/15791) | none (thin layer; upstream vLLM does the same cap) | `ansible/roles/images/files/vllm-26.06-fastapi-fix/Dockerfile` (built by the `images` role, ADR-0013) + 26.06 profiles' `vllm_image` | NVIDIA ships a 26.06+ image that caps `fastapi<0.137` or bundles a `_IncludedRouter`-aware instrumentator | ✅ applied — image built both nodes 2026-07-03 (fastapi 0.137.1→0.136.3); NVFP4 **load validated**, serving pending re-deploy onto the patched image |

## Completion criteria (re-attempt when ANY holds)

- **nccl#2167** is fixed/closed, **or** we validate `NCCL_NVLS_ENABLE=0` fully
  resolves our 2-node TP=2 bring-up on 26.06 (solo-first test plan below).
- A container ≥ 26.x ships **NCCL ≥ 2.30.6** (or otherwise reverts the NVLS
  regression).
- **vllm#41725** (inference-time TP=2 deadlock) closes with a fix or a confirmed
  stable workaround — this one is the real gate for *sustained* serving, since it
  strikes after 35–55 min even when bring-up succeeds.

## Retry plan (behind the fail-safe net)

**The model matters.** The blocker we hit is the **Marlin (AWQ / compressed-tensors) MoE
load path**, which NVFP4 (`modelopt`) does not use — so the retry targets an **NVFP4 model**
(e.g. `Step-3.7-Flash-NVFP4`), *not* minimax.

**Pin 26.06 per-profile, never globally.** A global `vllm_image` bump is what broke minimax
three times. The container is a **per-profile choice**: `vllm_image` in `group_vars/all.yml`
stays the stable **26.04** default, and only the NVFP4 profile overrides it —
`vllm_image: nvcr.io/nvidia/vllm:26.06-py3` in the profile YAML (an `-e @profiles/…` extra-var,
which outranks group_vars). Both images coexist on the nodes; `minimax-m2.7-awq` and everything else
keep loading on 26.04. See "Per-profile container pinning" note below.

When a completion criterion is met:

1. Add the 26.06 pin to the **NVFP4 profile only**; the `images` role (ADR-0013)
   pulls/builds the required images on both nodes at deploy — no manual `docker pull`.
   Pin the `container_images` entry by digest to make both nodes byte-identical.
2. Confirm `NCCL_NVLS_ENABLE=0` is present in the deployed NCCL config.
3. **Test solo first** if a small-enough NVFP4 model fits one node (no cross-node NCCL) — it
   isolates the FP4 kernels from the multinode path. (Step-3.7-NVFP4 is ~129 GiB and needs
   TP=2, so a smaller NVFP4 MoE would be the smoke-test candidate; else go to step 4 directly.)
4. TP=2 bring-up behind the fail-safe net (ADR-0009): a hang → hard reset → empty+reachable.
5. **Soak test**: multi-hour load to catch the #41725 inference-time deadlock (35–55 min).
   A clean bring-up is necessary but not sufficient.
6. Only promote to a serving profile after a clean soak.

### Per-profile container pinning

`vllm_image` is an ordinary var: a profile that sets it overrides the `group_vars/all.yml`
default (extra-vars precedence). No code change needed — the engine env file already
renders `{{ vllm_image }}` per engine. This is the mechanism that lets `minimax-m2.7-awq` stay on 26.04
while the `step-3.7-nvfp4` profile runs 26.06 on the same cluster.

## Re-assessment log

- **2026-08-08** — **Defect triage against 26.06 before an upgrade round. All three
  suspect rows are still real; none was stale.** Two were confirmed by inspecting the
  image directly: `Step3VLProcessor` still lacks `_get_num_multimodal_tokens`
  (DEF-0006 stands), and stock 26.06 still ships **fastapi 0.137.1**, so the derived
  `26.06-fastapi-fix` image is still load-bearing (DEF-0005 stands).

  The third is the interesting one. **DEF-0003 was mis-described, not stale.** Reading a
  live engine's startup config showed the profile requesting
  `cudagraph_mode: FULL_AND_PIECEWISE` — the exact mode the defect names — and vLLM
  *downgrading it itself*:

  > `CUDAGraphMode.FULL_AND_PIECEWISE is not supported with spec-decode for attention
  > backend FlashInferBackend …; setting cudagraph_mode=PIECEWISE`

  So the workaround has been in force by accident, via MTP speculative decoding, not by
  any profile setting. `enforce_eager=False`; graph capture succeeds in ~5 s for 1.2 GiB.
  **We have therefore never actually run full cudagraphs on GB10**, and nine days of
  uptime is not evidence about them — absence of a symptom is not a test when the
  condition was never active.

  The forward risk is concrete: the next 26.06 profile *without* spec-decode —
  DeepSeek-V4-Flash, Mistral-Medium, Nemotron-Puzzle are all candidates — unmasks it on
  first bring-up. The register row now says so, so the "check defects.md" step of the
  add-a-model pathway will actually catch it.
- **2026-08-06** — **First sustained 26.06 TP=2 serving run — no DEF-0002 deadlock in 90
  minutes.** `minimax-m2.7-nvfp4` (modelopt NVFP4, TP=2 across both nodes, 26.06) was
  activated and soaked at 1 completion/min for 90 min: **87 probes, zero non-200,
  latency 0.9–6.3 s with no upward drift.** Serving began ~01:22 and the soak ran
  01:31→03:00, so it covered **9→99 min in** — the 35–55 min deadlock window fully
  bracketed with margin either side. NCCL 2.30.5 initialised cleanly with the NVLS
  killswitch (DEF-0001 holding); bring-up was clean on the first attempt, no hard reset.
  **Does not clear DEF-0002.** The load was light (one small request per minute, no
  concurrency), 90 min is short of the "hours" the clears-when asks for, and it is a
  single run. What it does establish is that the deadlock is **not** reliably reproduced
  by light sustained traffic across the stated window — so the next test should raise
  *concurrency* rather than only duration. Worth noting the earlier Marlin reframe held:
  this is the NVFP4/`modelopt` kernel path, and it neither hung at load (DEF-0004 is
  indeed orthogonal) nor deadlocked in serving.
- **2026-07-02** — Upgrade opened. 26.06 bump hard-hung both nodes at TP=2
  bring-up; root-caused to NCCL 2.30.4+ NVLS regression (nccl#2167). Rolled back to
  26.04. Fail-safe boot (ADR-0009) and NVLS killswitch staged. All upstream issues
  open/un-triaged.
- **2026-07-02 (later)** — Re-attempted 26.06 with the NVLS killswitch + fail-safe boot
  deployed. **Killswitch worked** — got past the NCCL-init hang (nccl#2167 cleared for our
  config). But 26.06 then hung in the **model-load phase** (never reached ready, never
  served). Hard reset; **fail-safe boot validated** (both nodes came up empty + reachable).
- **2026-07-02 (later still)** — Tested `--enforce-eager` (compile + cudagraphs disabled,
  confirmed in args). **No change** — hung at the *identical* point on both nodes: right
  after "Starting to load model" → **Marlin backend for WNA16 MoE**, ~15 s into weight
  loading. So the blocker is **not** compile/cudagraph/NCCL — it's the **compressed-tensors
  WNA16 Marlin MoE weight-load path on sm_121**. `--enforce-eager` ruled out. Hard reset
  (3rd); fail-safe boot validated again. Rolled back to 26.04.
  **Key reframe:** minimax is an AWQ/**Marlin** model — but 26.06 is wanted for **NVFP4**,
  a *different* kernel path (`modelopt`/FP4) we have **not** tested. The Marlin-MoE load
  hang is likely orthogonal to NVFP4. The next 26.06 test should use an actual **NVFP4
  model** (single-node first per the retry plan), not minimax. We have
  `stepfun-ai/Step-3.7-Flash-NVFP4` already downloaded (HF cache).
- **2026-07-02 (step-3.7 / NVFP4 test — the key result)** — Deployed the new `step-3.7-nvfp4`
  profile (Step-3.7-Flash-NVFP4, per-profile 26.06 pin; **per-profile pinning validated** —
  minimax et al. stayed on 26.04). **NVFP4 loaded on 26.06 with NO hang** — it got past NCCL
  init *and* weight load cleanly, so the Marlin/AWQ load hang is confirmed **model-specific:
  the model mattered.** It then **crash-looped** (box stayed reachable — *not* a hang, no hard
  reset) on a vLLM vision-language bug: `AttributeError: 'Step3VLProcessor' object has no
  attribute '_get_num_multimodal_tokens'` (`vllm/model_executor/models/transformers/multimodal.py`,
  during max-image-token profiling) — orthogonal to NVFP4 and the container. Stopped the loop.
  **Takeaways:** (1) **26.06 runs NVFP4 kernels on GB10** — the container upgrade is viable for
  NVFP4; the hang was never about NVFP4. (2) Step-3.7 *specifically* is blocked on the VL-processor
  bug; a **text-only NVFP4 MoE** would validate 26.06/NVFP4 end-to-end (and single-node first, per
  the retry plan). (3) Also hit a first-deploy playbook abort from the worker-reconnect task, now
  fixed (regression noted in ADR-0011).

## Appendix — Marlin-MoE load-hang repro (DEF-0004; errata; follow-up deferred)

Full detail for the 26.06 model-load hang, kept as the record + the seed for a future repro
run / upstream report. Investigation is **paused** — minimax stays on 26.04. Summary is in the
WAR register + re-assessment log above.

### Exact hang location

Both nodes progress identically to the start of weight loading on 26.06, then freeze:

```
[gpu_model_runner] Starting to load model /models/MiniMax-M2.7-AWQ-4bit...
[compressed_tensors_wNa16]  Using MarlinLinearKernel for CompressedTensorsWNA16
[compressed_tensors_moe]    Using CompressedTensorsWNA16MarlinMoEMethod
[compressed_tensors_moe_wna16_marlin] Using Marlin backend for WNA16 MoE (group_size=32, num_bits=4)
[weight_utils] Checkpoint size: 121.53 GiB. Available RAM: 46.38 GiB.
<weight-load progress advances ~15 s, then STOPS; both nodes SSH-unresponsive; hard reset required>
```

The head unit's `TimeoutStartSec=1200` never fires — the *node itself* wedges, so systemd
can't act. Not a clean crash: a hard hang. `Available RAM ~46 GiB` during load is expected
(vLLM has reserved ~0.75×121 GiB of unified memory); **26.04 loads fine under the same
condition**, so it is not simple host-OOM.

### Versions & model

| | Hangs | Works |
|---|---|---|
| Container | `26.06-py3` | `26.04-py3` |
| vLLM | `0.22.1+7b9cb5b7.dev` | `0.19.0` |
| NCCL | `2.30.5` | `2.29.7` |
| CUDA (fwd-compat) | 13.3 / drv 610.43.02 | 13.2 / drv 595.58.03 |

Model `MiniMax-M2.7-AWQ-4bit` — compressed-tensors WNA16, `group_size=32`, `num_bits=4`,
Marlin MoE. TP=2 over 2× GB10 sm_121, gmu 0.75, host kernel driver 580.159.03.

### Ruled out

- **NCCL / NVLS** — `NCCL_NVLS_ENABLE=0` clears NCCL init; hang is later, at weight load.
- **torch.compile / CUDA graphs** — `--enforce-eager` (`mode=NONE`, confirmed) hangs identically.

⇒ It's the compressed-tensors WNA16 **Marlin MoE weight-load path on sm_121** — a clean
**regression** (0.19 loads fine, 0.22.1 hangs).

### Minimal reproducer

Model staged at `/opt/vllm/models/MiniMax-M2.7-AWQ-4bit` on both nodes; NCCL env from
`nccl-env.conf` (includes `NCCL_NVLS_ENABLE=0`). **Head (10.0.200.12):**

```bash
docker run --rm --name vllm-repro --network=host --gpus all --cgroupns=host --ipc=host \
  --device=/dev/infiniband --ulimit memlock=-1 --ulimit stack=67108864 --shm-size=32g \
  -v /opt/vllm/models:/models:ro --env-file /opt/vllm/nccl-env.conf -e VLLM_HOST_IP=10.0.200.12 \
  nvcr.io/nvidia/vllm:26.06-py3 \
  vllm serve /models/MiniMax-M2.7-AWQ-4bit --host 0.0.0.0 --port 8000 \
    --served-model-name minimax-m2 --tensor-parallel-size 2 --distributed-executor-backend mp \
    --nnodes 2 --node-rank 0 --master-addr 10.0.200.12 --trust-remote-code \
    --max-model-len 131072 --gpu-memory-utilization 0.75 --enable-chunked-prefill
```

**Worker (10.0.200.13):** same but `-e VLLM_HOST_IP=10.0.200.13`, `--node-rank 1`, `--headless`,
drop `--host/--port/--served-model-name`. Swap the tag to `26.04-py3` for the known-good control.
For a diagnostic run: uncomment `NCCL_DEBUG=INFO` in `nccl-env.conf`, add `-e VLLM_LOGGING_LEVEL=DEBUG`,
and try `docker exec <ctr> py-spy dump --pid 1` in the window before the node wedges.

### Related upstream issues (searched 2026-07-02)

Our exact symptom (load-phase *hang*) isn't filed, but it's inside a known cluster —
compressed-tensors WNA16 Marlin MoE is broken on GB10/sm_121 in several ways:

- [vllm#40357](https://github.com/vllm-project/vllm/issues/40357) — **closest**: GB10/sm_121,
  compressed-tensors WNA16 INT4 `group_size=32` (same as ours), Marlin MoE — symptom is
  *repeated tokens at inference*, not a load hang; `--moe-backend triton` is **ignored** (still
  binds Marlin).
- [vllm#43906](https://github.com/vllm-project/vllm/issues/43906) — sm_121 is **gated out** of the
  optimized MoE kernels (`family(100)` excludes SM_12x) → **falls back to Marlin**.
- [vllm#35303](https://github.com/vllm-project/vllm/issues/35303) —
  `CompressedTensorsWNA16MarlinMoEMethod` crashes on actorder=null AWQ MoE (Blackwell).
- [vllm#41511](https://github.com/vllm-project/vllm/issues/41511) — W4A16 MoE `weight_scale` not
  K-sharded under TP (TP>2 crash; TP=2 works — not our hang, same subsystem).

**Sharpest evidence: the 0.19 → 0.22.1 load-path regression.** No config escape exists
(`--moe-backend triton` ignored; sm_121 gated into Marlin by design), so **26.04 is minimax's
correct home**, not a temporary state. To report: comment on #40357 (same HW + quant) with the
load-hang symptom + the regression boundary.

### Open questions for NVIDIA

- Is the WNA16 **Marlin MoE** weight-load path known-broken on sm_121 in the 26.06 vLLM build?
  (26.04 / 0.19 is fine — clean regression boundary.)
- **Does this affect the NVFP4 / `modelopt` path at all?** That is what we actually want 26.06
  for, and it does not use Marlin — likely orthogonal, but untested.

## References

- ADR-0009 — fail-safe boot (the net that makes retrying safe)
- `docs/models/step-3.7-flash.md` — NVFP4 target model + memory analysis
- `ansible/group_vars/all.yml` — the `vllm_image` pin (with the hang note)
- `ansible/roles/common/files/nccl-env.conf` — the NVLS killswitch
