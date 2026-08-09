# Upgrade: `nvcr.io/nvidia/vllm` → `26.07-py3` (the defect-clearing candidate)

**Status:** ✅ **Done — all six campaign steps complete (2026-08-08).** Every NVFP4
profile serves on `dgx-spark/vllm:26.07-xgrammar-fix`; `step-3.5-fp8` stays on 26.04 by
choice. **DEF-0001 and DEF-0005 closed and deleted from the register**, DEF-0002 and
DEF-0003 downgraded to watch, DEF-0010 WAR'd and verified, DEF-0011 filed and scoped —
and DEF-0004 escalated to 🔴, taking the AWQ profile with it.
**Current pins:** `26.04-py3` (vLLM 0.19, NCCL 2.29.7) for `step-3.5-fp8` alone · `dgx-spark/vllm:26.07-xgrammar-fix` (vLLM 0.24.0, NCCL 2.30.7) for everything else
**Target:** `26.07-py3` — **vLLM 0.24.0, NCCL 2.30.7, fastapi 0.136.3** (probed 2026-08-08)
**Last updated:** 2026-08-08

> NGC images are calendar-versioned `YY.MM`, so `26.07-py3` is the **July 2026** build.
> It is *not* a vLLM version — 26.04 ships vLLM 0.19, 26.06 ships 0.22.1, 26.07 ships
> whatever it ships, which is the first thing to find out.

This is a **living tracker**, not a decision record. Supersedes nothing: the
[26.06 tracker](container-nvidia-vllm-26.06-py3.md) stays the record of how we got the
NVFP4 profiles running, and its findings still apply until disproved here.

---

## Why this one matters more than the rest of the sweep

The 2026-08-08 version sweep found newer releases of almost everything (Open WebUI
0.11.0, Grafana 13.1.3, node-exporter 1.12.1, …). All of it is housekeeping. **This
single artefact is the one that could clear six defect rows**, because every one of
them is conditioned on what the container ships:

| Defect | Waits on | What clearing it buys |
|---|---|---|
| **DEF-0001** | NCCL ≥ 2.30.6 with the NVLS regression reverted | drop `NCCL_NVLS_ENABLE=0` |
| **DEF-0002** | vllm#41725 — TP=2 inference deadlock, 35–55 min in | **sustained TP=2 serving on 26.x** — the big one |
| **DEF-0003** | vllm#40969 — GB10 cudagraph hang | full cudagraphs, and an ADR-0014 throughput A/B |
| **DEF-0004** | Marlin WNA16 MoE load path on sm_121 | AWQ/compressed-tensors profiles off 26.04 |
| **DEF-0005** | a `fastapi<0.137` cap upstream | **delete the derived image entirely** — Dockerfile, build entry, and all |
| **DEF-0006** | `Step3VLProcessor._get_num_multimodal_tokens` | un-park `step-3.7-nvfp4` |

Nothing else in the sweep unblocks anything. That is the argument for probing this
before spending a bump cycle on Grafana.

## Probe results — 2026-08-08

| | 26.06 (current) | **26.07** | consequence |
|---|---|---|---|
| vLLM | 0.22.1 | **0.24.0** | two minors — behaviour changes possible, test per profile |
| NCCL | 2.30.5 | **2.30.7** | **≥ 2.30.6** — DEF-0001's stated bar is met |
| fastapi | 0.137.1 | **0.136.3** | **capped — DEF-0005 clears** |
| `Step3VLProcessor._get_num_multimodal_tokens` | absent | **still absent** | DEF-0006 stands |
| `DeepseekV4` / `Step3VL` / `Qwen3_5Moe` / `MiniMaxM2` archs | present | **all present** | no regression |

**The headline: the derived image can die.** `26.07-py3` ships fastapi **0.136.3**, below
the 0.137 that breaks `prometheus-fastapi-instrumentator`. That is DEF-0005's clears-when
verbatim — *"NVIDIA ships a 26.06+ image that caps `fastapi<0.137` … then repoint
`vllm_image` to stock, drop the derived entry."* Moving the NVFP4 profiles to 26.07
deletes `dgx-spark/vllm:26.06-fastapi-fix`, its Dockerfile, its build context and its
`container_images` entry outright.

**NCCL 2.30.7 meets DEF-0001's bar but does not clear it.** The row asks for ≥ 2.30.6
*with the regression reverted*, and the only proof is a dual-node bring-up with
`NCCL_NVLS_ENABLE=0` removed. Note the blast radius: that killswitch lives in
`roles/common/files/nccl-env.conf`, which is **cluster-wide and byte-identical on both
nodes** — unlike a per-profile image pin, there is no way to test it on one profile only.
Do it last, alone, behind the fail-safe net, and revert on any hang.

**vLLM 0.24.0 is a two-minor jump** from 0.22.1. Nothing in the probes says our serve
flags still parse — `--speculative-config`, the tool/reasoning parsers and the
quantization flags all deserve a real bring-up before this is called good.

## The probes (re-run these on the next bump)

None of these touch the cluster; they run the image and exit. Run them **before**
changing a single pin.

What vLLM and NCCL does it ship?

```bash
sudo docker run --rm nvcr.io/nvidia/vllm:26.07-py3 python3 -c "import vllm,torch;print('vllm',vllm.__version__);print('nccl',torch.cuda.nccl.version())"
```

DEF-0005 — is fastapi capped, or still the 0.137.1 that breaks the instrumentator?

```bash
sudo docker run --rm nvcr.io/nvidia/vllm:26.07-py3 pip show fastapi
```

DEF-0006 — does the Step-3.7 VL processor carry the missing method yet?

```bash
sudo docker run --rm nvcr.io/nvidia/vllm:26.07-py3 python3 -c "from vllm.model_executor.models.step3_vl import Step3VLProcessor as P;print(hasattr(P,'_get_num_multimodal_tokens'))"
```

Still carries the architectures we depend on (regression check, not a new capability)?

```bash
sudo docker run --rm nvcr.io/nvidia/vllm:26.07-py3 python3 -c "from vllm.model_executor.models.registry import ModelRegistry as R;a=R.get_supported_archs();print({k:(k in a) for k in ['DeepseekV4ForCausalLM','Step3VLForConditionalGeneration','Qwen3_5MoeForConditionalGeneration','MiniMaxM2ForCausalLM']})"
```

## The test campaign — one variable per activation

Staged in a single deploy (Phase A), then run as a sequence of `activate` calls, which
need no privilege. Each step isolates one thing; run them **in order**, because a
failure early makes every later result unattributable.

| # | activate | isolates | tells us | result (2026-08-08) |
|---|---|---|---|---|
| 1 | `qwen3.6-35b-nvfp4-mtp3-single` | the container alone — MTP-3 keeps cudagraphs downgraded, so DEF-0003 stays masked | do our serve flags survive vLLM 0.24.0 | ✅ flags survive — but found **DEF-0010** (all tool-calling 500s) and, after WAR'ing it, **DEF-0011** |
| 2 | `qwen3-coder-nvfp4-single` | **no spec-decode** → `FULL_AND_PIECEWISE` for the first time on GB10 | **DEF-0003** | ✅ **no hang** — first real exercise ever; DEF-0003 → 🔵 watch |
| 3 | `minimax-m2.7-nvfp4` + a **concurrency** soak | TP=2 on 26.07 | **DEF-0002** | ✅ loads and serves on 26.07 (KV 30.58 GiB / 449,664 tok); soak result below |
| 4 | ~~`minimax-m2.7-awq-2607`~~ | AWQ/Marlin MoE load path on 0.24.0 | **DEF-0004** | ❌ **froze sparky** during weight load — profile deleted, DEF-0004 escalated to 🔴 |
| 5 | — delete the derived image | | **DEF-0005** closed | ✅ done — `vllm-26.06-fastapi-fix` removed |
| 6 | drop `NCCL_NVLS_ENABLE=0`, TP=2 bring-up | cluster-wide, cannot be isolated per profile | **DEF-0001** | ✅ **cleared** — NVLS-enabled TP=2 bring-up clean on NCCL 2.30.7; WAR removed, row deleted |

`minimax-m2.7-awq` and `step-3.5-fp8` were held on 26.04 throughout as the fallback if
26.07 disappointed. That is why step 4 used a temporary `-2607` twin rather than
repointing the real AWQ profile — it was the only known-good AWQ configuration we had,
and DEF-0004 is precisely the defect that could take it away.

**That caution paid for itself**: step 4 froze the node, and the real AWQ profile was
never at risk. It was retired afterwards anyway, but by *choice* rather than by
accident — DEF-0004 means AWQ can never leave 26.04, while `minimax-m2.7-nvfp4` serves
the same model on the current container. Only `step-3.5-fp8` remains on 26.04.

Step 5 was deliberately last-but-one: keeping `dgx-spark/vllm:26.06-fastapi-fix`
buildable through steps 1–4 is what made rollback a one-line profile edit.

## Implications for the cluster

- **If fastapi is capped:** `dgx-spark/vllm:26.06-fastapi-fix` and
  `roles/images/files/vllm-26.06-fastapi-fix/` are deleted, the `build:` entry leaves
  `container_images`, and every NVFP4 profile repoints at stock `26.07-py3`. That is the
  cleanest possible outcome — a whole derived-image mechanism retired.
- **If DEF-0002 clears:** TP=2 on 26.x becomes viable for sustained serving, which is
  what a Tier-1 NVFP4 model (DeepSeek-V4-Flash) would need. Until then the 90-minute
  light-load soak of 2026-08-06 is the only positive evidence, and it explicitly did not
  test concurrency.
- **If DEF-0003 clears — or is finally testable:** note the trap recorded on that row.
  Both live 26.06 profiles mask it by accident via MTP spec-decode, so the honest test
  needs a **spec-decode-free** profile. A 26.07 bring-up is the natural moment.
- **26.04 may stay** regardless: DEF-0004 keeps AWQ/Marlin models there. A bump does not
  automatically collapse the two-image arrangement.

## Dependencies

None blocking. This tracker inherits the [26.06 tracker](container-nvidia-vllm-26.06-py3.md)'s
WAR register — each WAR's *remove when* is re-evaluated here rather than restated.

## Completion criteria

1. The four probes above are recorded in the re-assessment log.
2. Every defect row the bump touches is re-tested **one at a time** (pulling several
   workarounds at once hides which was still load-bearing) and its status updated.
3. A single-node NVFP4 profile activates and passes the smoke gate on 26.07.
4. A TP=2 profile activates, and is soaked **under concurrency** — the shape DEF-0002
   actually needs, which the 90-minute light soak did not provide.
5. `docs/updating.md`'s container pathway is walked: digest into `container_images`, the
   derived image's `FROM` if it survives, per-profile `vllm_image`, this tracker, the
   defect register.

## Retry / deploy plan

Behind the fail-safe net, which is now **verified** rather than assumed (both boot gates
demonstrated 2026-08-08, see the README). Single-node on snoopy first — sparky keeps the
frontends up and a bad bring-up costs one reboot. Only then TP=2, which is the shape that
has historically required hard resets.

Pin it as a **per-profile** `vllm_image` first, never the global default. That is the
mechanism ADR-0013 exists for, and it means a bad 26.07 affects exactly one profile.

## Re-assessment log

- **2026-08-08 (step 6 — DEF-0001 CLEARED; campaign complete)** — `NCCL_NVLS_ENABLE=0`
  removed from `nccl-env.conf` and deployed to both nodes, then a full stop/start cycle
  (`activate empty` → `activate minimax-m2.7-nvfp4`) to force a fresh TP=2 rendezvous.
  It came up clean:

  > `vLLM is using nccl==2.30.7` ·
  > `world_size=2 rank=0 distributed_init_method=tcp://10.0.200.12:29501 backend=nccl` ·
  > `Using ['PYNCCL'] all-reduce backends for group 'tp:0'` ·
  > weights loaded, smoke gate **pass**, both nodes reachable throughout

  This is the failure that cost a double hard-reset on 2026-07-02, so passing it matters.
  Worth noting NVLS was genuinely *available* to be chosen — `SymmMemCommunicator` bows
  out on capability 12.1 and NCCL still selected `PYNCCL` from the full candidate list —
  so the code path was exercised rather than quietly skipped.

  DEF-0001's row is **deleted** per the register's own rule (a defect that is truly gone
  leaves; git history keeps it) and the killswitch comment in `nccl-env.conf` now records
  the verification plus a one-line restore procedure if a future bring-up ever hangs.

  **The 26.07 campaign is done.** Six steps, one variable each, one node freeze, one
  power cycle, two defects closed outright, two downgraded, one escalated, one new one
  filed and scoped by experiment.

- **2026-08-08 (step 3 — TP=2 SERVES on 26.07)** — `minimax-m2.7-nvfp4` activated after
  the AWQ freeze, as the replacement TP=2 experiment. Loaded cleanly in ~9 min:

  > 15 shards at ~40 s each · `Available KV cache memory: 30.58 GiB` ·
  > `GPU KV cache size: 449,664 tokens` · CUDA graphs 51/51 · smoke gate **pass**

  KV came in *above* the profile's ~26 GiB estimate. Tool-calling passes all four
  `tool_choice` shapes, which is what pins DEF-0011's scope (below).

  **The soak then passed clean — DEF-0002 not reproduced on 26.07:**

  > 64 min · concurrency 8 · 164 rounds · **1312/1312 HTTP 200** ·
  > p50 21.8 s / max 23.4 s · no latency creep · straight through the 35–55 min window

  Every request finished on `length` (300-token cap), so each one ran the full decode
  path rather than stopping early — a heavier test than varied output lengths, though it
  does mean this measured *sustained decode under concurrency* specifically.

  The harness was rewritten first to record the real HTTP status, latency and byte count
  per request. The previous one inferred success from output shape, which is why its one
  anomalous round in 62 could never be explained afterwards — and an unexplained blip in
  a deadlock hunt is worth nothing. 1312 rows are now on disk for this run.

  DEF-0002 stays 🔵 **watch, not cleared**: we have never reproduced the deadlock
  ourselves, so a clean soak is absence of evidence rather than a fix.

  **This makes NVFP4, not AWQ, MiniMax's forward path.** Same model, same TP=2 shape, a
  `modelopt` loader that never touches the Marlin code DEF-0004 lives in.

- **2026-08-08 (step 4 — AWQ FROZE THE NODE; DEF-0004 escalated)** —
  `minimax-m2.7-awq-2607` activated. The engine reached `Loading safetensors checkpoint
  shards: 0/27`, host memory then collapsed (`systemd-journald: Under memory pressure,
  flushing caches` every ~2 s for eight minutes) and **sparky stopped responding
  entirely** — recovered only by a physical power cycle.

  vLLM 0.24.0 therefore does **not** fix DEF-0004, and the failure mode is worse than
  filed: a node-killer, not a load hang. Ruled out as a memory-budget problem —
  `minimax-m2.7-nvfp4` loads a **larger** checkpoint (130.28 GiB vs 121.53) with **less**
  free host RAM (42–45 GiB vs 46.09) and is fine. The variable is the Marlin path.

  **The fail-safe boot gate fired for real for the first time** (ADR-0009 — previously
  only verified synthetically). The `.running` marker survived the hard reset, proving
  the stop was unclean, so on boot systemd refused to re-attempt the load that had just
  killed the machine:

  > `vllm@minimax-awq-2607.service was skipped because of an unmet condition check`
  > `(ConditionPathExists=!/opt/vllm/state/vllm-minimax-awq-2607.running)`

  sparky came back **empty and reachable in four minutes** instead of freezing again
  unattended. Recovery was an unprivileged `activate`, exactly as designed.

  Consequences: the `-2607` twin profile is **deleted** (its own header planned for this
  outcome), AWQ stays pinned to 26.04 indefinitely, and DEF-0004 moves 🟡 → 🔴 with a
  "human present, fail-safe verified" precondition on any future re-test.

- **2026-08-08 (DEF-0010 WAR verified; DEF-0011 found and scoped)** — the derived image
  works: `none` and `auto` both 200, `auto` returning a real `tool_calls` entry. Two
  process lessons, each of which cost a deploy cycle:

  1. **`--no-deps` is load-bearing.** The first attempt used a plain
     `pip install 'xgrammar>=0.2.1'`; pip re-resolved xgrammar's own `transformers`
     dependency down to v4, which vLLM 0.24.0 removed, and every engine died at import.
     A vendor image is a solved dependency set — patch it surgically or not at all.
  2. **A tag is not a version.** The corrected Dockerfile was then *silently ignored*,
     because the `images` role gated `docker build` on the image already being present.
     A `build:` tag never changes, so presence says nothing about which Dockerfile
     produced it. The build now always runs (layer cache makes an unchanged context a
     no-op) and the image carries build-time assertions, so a broken WAR fails the
     **deploy** in seconds rather than every engine minutes later.

  `tool_choice: "required"` and named-function still 500 — but with a different
  signature, `grammar rejected tokens [198, …]`, an FSM rejection at generation time
  rather than an import error. Filed as **DEF-0011**. Scope was then settled by
  experiment rather than inference: `minimax-m2.7-nvfp4` (same container, also a
  reasoning model, **no** spec-decode) passes all four shapes, so MTP is the co-factor
  and the blast radius is one profile — matching [vllm#34650](https://github.com/vllm-project/vllm/issues/34650).

- **2026-08-08 (DEF-0010 WAR'd — taking the bump)** — built
  `dgx-spark/vllm:26.07-xgrammar-fix` (26.07 + `pip install 'xgrammar>=0.2.1'`) and
  repointed all five 26.07 profiles at it. **Deleted** `vllm-26.06-fastapi-fix` — its
  defect (DEF-0005) is fixed in 26.07, so that row is closed and the Dockerfile removed.

  The trade is deliberate and follows the standing priority: **staying current beats
  keeping a known-working older image.** Two consecutive NGC releases have each shipped
  an internally inconsistent dependency pair — 26.06 fastapi, 26.07 xgrammar — so the
  derived image is not a transient embarrassment to be escaped but the mechanism
  (ADR-0013) that lets us run the newest container at all. What it patches will keep
  changing; that it exists is the point.

  Rollback, if ever needed, is git: the 26.06 Dockerfile and pins are one revert away and
  the image itself is still resident on both nodes until pruned.

- **2026-08-08 (step 2 — DEF-0003 exercised, NO HANG)** — `qwen3-coder-nvfp4-single`
  activated on 26.07. It carries **no speculative decoding**, so nothing downgraded the
  cudagraph mode and `FULL_AND_PIECEWISE` ran for real — the first time on this hardware:

  > `speculative_config=None` · `cudagraph_mode: FULL_AND_PIECEWISE` ·
  > `Graph capturing finished in 9 secs, took 1.44 GiB`

  Crucially the step-1 warning (`FULL_AND_PIECEWISE is not supported with spec-decode …
  setting cudagraph_mode=PIECEWISE`) is **absent**. Five inference requests returned 200
  in 6.8–12.1 s. No hang.

  **Scope it honestly:** this is vLLM **0.24.0**, not the 0.22.1 the defect was filed
  against, and five requests are not a soak. It supports "full cudagraphs work on
  26.07", not "#40969 is fixed". DEF-0003 moves to 🔵 watch, not cleared — the ADR-0014
  throughput A/B (PIECEWISE vs FULL) is the follow-up that would make it worth acting on.

  Note also this profile's smoke gate was skipped (`--no-smoke`): DEF-0010 would have
  failed it on tool-shape for reasons unrelated to what was under test.

- **2026-08-08 (DEF-0010 cause pinned)** — the image ships **xgrammar 0.2.0** while vLLM
  0.24.0 requires `>= 0.2.1`. Not a loose-floor problem as first written: NVIDIA shipped
  **below vLLM's own declared minimum**, so `pip check` inside the image would have
  caught it. The WAR is exact: `pip install 'xgrammar>=0.2.1'` in a derived image.

- **2026-08-08 (step 1 — canary, FAILED)** — `qwen3.6-35b-nvfp4-mtp3-single` activated on
  26.07. The engine **loaded and served plain chat correctly**; the smoke gate then
  failed on the tool-shape probe with HTTP 500:

  > `ImportError: cannot import name 'normalize_tool_choice' from 'xgrammar'`
  > — `vllm/tool_parsers/structural_tag_registry.py:12`

  Blast radius, measured: `tool_choice: "none"` → 200; **`"auto"` → 500; `"required"` →
  500**. Open WebUI sends `auto` on ordinary chats, so this is not a niche tool-workflow
  problem — it breaks normal chat in the UI. Filed as **DEF-0010**.

  The cause is DEF-0005's shape one layer up: vLLM 0.24.0 requires `xgrammar >= 0.2.1`,
  a floor looser than the symbol its code imports, and NVIDIA shipped a satisfying-but-too-old
  version. So 26.07 *fixes* the fastapi pairing and *introduces* an xgrammar pairing.
  **The likely WAR is the same as the one it retires** — a derived image, this time
  `pip install -U xgrammar`.

  The canary did its job exactly: MTP-3 kept cudagraphs downgraded, so DEF-0003 stayed
  masked and this is attributable to the container alone. Without the gate's tool-shape
  probe it would have shipped and surfaced later as "Open WebUI chat randomly fails".

  **Consequence for the rest of the campaign:** every 26.07 smoke gate will now fail on
  tool-shape for this known reason. That failure must not be mistaken for the defect
  under test in steps 2–4, which are about cudagraphs, TP=2 deadlock and Marlin loading —
  all independent of tool-calling, so those results still carry to a fixed 26.07 image.

- **2026-08-08 (probed)** — vLLM **0.24.0**, NCCL **2.30.7**, fastapi **0.136.3**,
  Step-3.7 VL processor still missing its method, all four architectures present. See
  the results table above. Verdict: **worth taking**, primarily to retire the derived
  image (DEF-0005) — with DEF-0002/0003/0004 becoming *testable* rather than
  automatically fixed, and DEF-0001 gaining a credible but unproven NCCL.
- **2026-08-08 (opened)** — `26.07-py3` found on NGC by the first `version-discovery`
  sweep; the same sweep confirmed **stock 26.06 still ships fastapi 0.137.1** and its
  `Step3VLProcessor` still lacks `_get_num_multimodal_tokens`, so DEF-0005 and DEF-0006
  both stand against 26.06. Image pulling; nothing probed.

## References

- [26.06 tracker](container-nvidia-vllm-26.06-py3.md) — the predecessor, and the WAR register
- [`docs/defects.md`](../defects.md) — the six rows this bump is aimed at
- [`docs/updating.md`](../updating.md) — the container-bump pathway
- [ADR-0013](../adr/0013-container-image-sourcing.md) — images as sourced, pinned artefacts
- [ADR-0014](../adr/0014-optimization-register.md) — the throughput A/Bs a cudagraph fix would enable
