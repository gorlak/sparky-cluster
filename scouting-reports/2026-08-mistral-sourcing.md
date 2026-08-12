# Scouting report — 2026-08-11 · sourcing a Mistral that actually serves

**Trigger.** `mistral-medium-3.5-128b-nvfp4` (repo `nvidia/Mistral-Medium-3.5-128B-NVFP4`)
failed its DEF-0012 trial activation on 26.07 / vLLM 0.24.0, TP=2. This sweep asks the
sourcing question the profile header pre-registered: *is there a coherently-packaged
Mistral that serves on GB10?*

**Answer: yes — three candidates, all of which fit with room, and two with published
GB10 evidence.** The fleet's European slot is a sourcing problem, not a model problem.

---

## What the failed trial actually proved

Worth stating precisely, because it narrows the search and it corrects a stronger claim
made earlier the same day.

`--config-format mistral` **fixed** the original symptom — the
`Mismatch in 'image' token count between text and input_ids` from HF's `PixtralProcessor`
is gone. Config and tokenizer now agree. It then failed further in, at weight load:

```
quantization=fp8, quantization_config=None          # the MIXED_PRECISION map was lost
...
File ".../vllm/model_executor/models/llama.py", line 479, in load_weights
    param = params_dict[name]
KeyError: 'language_model.embed_tokens.weight'
```

Both halves of that are the same root cause: **`load_format` stayed `auto` while
`config_format` went native.** vLLM built a Mistral-native config and then asked an
HF-named weight loader to satisfy it. The quant config went with the config switch
(`params.json` in this repo carries no `quantization_config`); the weight names did not.

**The mixed state was never resolved — it moved down one layer.** First the tokenizer
disagreed with the config; then the config disagreed with the loader.

### The correction: this checkpoint is probably not "mis-assembled"

An earlier verdict today called `nvidia/Mistral-Medium-3.5-128B-NVFP4` mis-assembled and
recommended rejecting it. **The forum evidence contradicts that**, and the file listings
settle it:

| repo | `config.json` | `model.safetensors.index.json` | `params.json` | `tekken.json` | `hf_quant_config.json` |
|---|---|---|---|---|---|
| `nvidia/Mistral-Medium-3.5-128B-NVFP4` (ours) | ✓ | ✓ | ✓ | ✓ | ✓ |
| `zdy1995love/Mistral-Medium-3.5-128B-NVFP4` | ✓ | ✓ | ✓ | ✓ | — |
| `RedHatAI/Mistral-Medium-3.5-128B` | ✓ | ✓ | ✓ | ✓ | — |

**Dual HF-plus-native artifacts is the Mistral publishing convention, not a packaging
error** — every Mistral-3 repo checked ships both. And `zdy1995love/…-NVFP4`, structurally
the same as ours, **loads and serves on a DGX Spark**. So the fault is in our flag
combination and/or vLLM 0.24.0, not in the bytes on disk.

---

## The field

All three fit TP=2 with generous headroom — the constraint here is packaging and kernel
support, never capacity.

| # | Repo | Total | /node @TP=2 | Packaging | GB10 evidence |
|---|---|---|---|---|---|
| **A** | `nvidia/Mistral-Medium-3.5-128B-NVFP4` | 89 GiB | 44.5 | dual; quant in **HF** config only | ❌ fails for us (above) |
| **B** | `zdy1995love/Mistral-Medium-3.5-128B-NVFP4` | 74.8 GiB | 37.4 | dual; no `hf_quant_config.json` | ✅ **served on DGX Spark** |
| **C** | `mistralai/Mistral-Small-4-119B-2603-NVFP4` | 65.9 GiB | 33.0 | **pure native**; quant **in `params.json`** | ✅ **served on DGX Spark** |

### ⚠️ SIZING CORRECTION 2026-08-11 — Mistral repos ship the weights TWICE

An earlier version of this table ruled out three candidates as "too big". **That was wrong,
and the error was a sizing method, not a judgement.** Summing every `*.safetensors` in a
Mistral repo **double-counts**: these repos carry a complete **native** `consolidated-*`
set *and* a complete **HF** `model-*` set of the same weights. You download and serve
**one**.

| Repo | consolidated-* | model-* | naive total | **real, one set** |
|---|---|---|---|---|
| `mistralai/Mistral-Medium-3.5-128B` | 124.4 GiB | 124.4 GiB | 248.9 | **62.2 GiB/node** |
| `RedHatAI/Mistral-Medium-3.5-128B` | 124.4 GiB | 124.4 GiB | 248.9 | **62.2 GiB/node** |
| `mistralai/Devstral-2-123B-Instruct-2512` | 119.4 GiB | 119.4 GiB | 238.9 | **59.7 GiB/node** |
| `mistralai/Mistral-Small-4-119B-2603-NVFP4` | 65.9 GiB | — | 65.9 | 33.0 GiB/node |

All three "rejects" **fit comfortably at TP=2.** And their `fp8` tag was accurate, not
aspirational — the dtype histogram from the safetensors metadata proves it:
`Mistral-Medium-3.5-128B` is `{F8_E4M3: 121.8B, BF16: 5.9B}`.

**Always size from `.safetensors.parameters` (the dtype histogram) and split the file list
by layout prefix.** Summing bytes alone is wrong for any repo that ships both layouts, and
every `mistralai/` repo checked does.

### D — `mistralai/Mistral-Medium-3.5-128B` (official FP8) — the strongest Tier-1 option

Recovered by the correction above, and it outranks A–C on the cluster's *own* priorities:

- **Official Mistral**, canonical packaging — not a third-party quant of anything.
- **Genuinely FP8**, dtype-verified. 62.2 GiB/node — fits at `gmu 0.75` with ~27.5 GiB
  KV/rank, and more if `gmu` is raised.
- **Dense 128B** — the Tier-1 shape. [[model-discovery]] is explicit that a candidate which
  only fits by leaving the hardware idle is *under-using* it; C at 33 GiB/node leaves most
  of the 121 GiB pool unused, and this does not.
- **It ships BOTH weight layouts**, which is the decisive difference from what we have
  staged. `nvidia/Mistral-Medium-3.5-128B-NVFP4` carries only `model.safetensors.index.json`
  — **no `consolidated.safetensors.index.json`** — so `--load-format mistral` has no native
  index to read and the trio may be unrunnable there *no matter what we set*. This repo has
  one, so the proven native recipe is actually available.

**Download caveat:** a naive `snapshot_download` pulls **249 GiB to get 124 GiB of model.**
Restrict to one layout (`consolidated*` for the native path, `model-*` for HF) — worth
teaching `scripts/download.py` an allow-pattern for exactly this.

### C is the structurally interesting one

`mistralai/Mistral-Small-4-119B-2603-NVFP4` is the only candidate with **no HF artifacts at
all** — no `config.json`, no `model.safetensors.index.json`. Just `params.json`,
`tekken.json`, `consolidated.safetensors.index.json`. There is nothing for the native path
to disagree with, and critically its `params.json` **carries the quant config inline**:

```json
"quantization_config": {"format": "nvfp4-pack-quantized",
                        "quant_method": "compressed-tensors", ...}
```

That is exactly the property Candidate A lacks. `--config-format mistral` cannot lose a
quant config that lives in the native config.

Other facts: Apache-2.0 (A is `license: other`), newer generation, **MoE** (128 experts, 4
active + 1 shared), **MLA** attention (`kv_lora_rank`/`q_lora_rank`/`qk_rope_head_dim`),
vision encoder, YaRN context extension.

---

## GB10 corroboration (NVIDIA Developer Forums)

Two threads, both primary-source, both fetched 2026-08-11.

**[Running Mistral Small 4 119B NVFP4 on DGX Spark (GB10)](https://forums.developer.nvidia.com/t/running-mistral-small-4-119b-nvfp4-on-nvidia-dgx-spark-gb10/363863)** — candidate C, working:

```
--tokenizer-mode mistral --config-format mistral --load-format mistral
--max-model-len 40000 --gpu-memory-utilization 0.75
--tool-call-parser mistral --enable-auto-tool-choice
VLLM_MLA_DISABLE=1  VLLM_NVFP4_GEMM_BACKEND=marlin
```
~27 tok/s, ~99 GB, single node, vLLM 0.17.2rc1 **built from source** (not NGC).

**The flags travel as a TRIO.** `--load-format mistral` is the one our profile lacked, and
its absence is exactly what the `KeyError` reports. This is the single most actionable
thing the sweep found.

**[Performance report: Mistral Medium 3.5 128B NVFP4 + EAGLE](https://forums.developer.nvidia.com/t/performance-report-mistral-medium-3-5-128b-nvfp4-eagle/368917)** — candidate B, working: repo
`zdy1995love/Mistral-Medium-3.5-128B-NVFP4`, vLLM 0.20.2rc1, `--load-format auto` (their
`fastsafetensors` attempt OOM'd at 16k), ~559 s to load. **No tokenizer, config-format,
image-token or quantization errors reported.**

### Two caveats that are ours to carry, not theirs

1. **`VLLM_MLA_DISABLE=1` is a real cost on C.** They disabled MLA because `head_size=320`
   is unsupported, which capped context at 40k against the model's 65k. Whether 0.24.0
   still needs this is unknown and worth a probe before committing.
2. **`VLLM_NVFP4_GEMM_BACKEND=marlin` on an MoE intersects [DEF-0004](../docs/defects.md)** —
   the compressed-tensors Marlin-MoE weight-load path froze sparky hard enough to need a
   power cycle. That defect is WNA16, not NVFP4, so this is adjacent rather than identical;
   but C is an MoE being pushed onto a Marlin backend, and DEF-0004's rule is *do not
   re-test a Marlin MoE path without the fail-safe verified and a human present*.
3. Both reports are **single-node**, on **source-built vLLM well behind 0.24.0**. Neither
   tells us this works at TP=2 on NGC 26.07. That is the gap our own bake-off would close.

---

## Recommendation — a three-way bake-off, same shape as the Nemotron one

The cheapest experiment first, because it needs no download at all:

**Round 0 — one more activation of what is already staged (free).** Add
`--load-format mistral` to candidate A's flags, completing the native trio. It costs one
deploy and one activation and directly tests the sweep's main finding. Two outcomes, both
useful: it serves (DEF-0012 closes, no download), or it fails on the missing
`consolidated.safetensors.index.json` — which would then be *real* evidence that this repo
cannot take the native path, rather than the inference we drew today.

**Round 1 — stage B and C and run them against A.** 140 GiB of downloads total, both
unprivileged. Then a `runbooks/mistral-bakeoff.yml` over `(profile × regiment)`, exactly
like the Nemotron Puzzle-vs-Super comparison — same regiments, same day, same container, so
the numbers are comparable rather than a pile.

Pre-register the decision rule before the numbers exist:

- **D (official FP8) is the one to beat**, and the sizing correction is why. It is the only
  candidate that is simultaneously vendor-published, dense, Tier-1-shaped, and packaged with
  the native layout the proven recipe needs. If it serves, take it.
- **If C serves at TP=2 without `VLLM_MLA_DISABLE`** it is the *fast* European option rather
  than the Tier-1 one — smallest footprint, widest KV headroom, Apache-2.0, newest
  generation, and packaging that cannot re-create this class of failure. Worth carrying
  alongside D, not instead of it.
- **If C needs MLA disabled**, its 40k context ceiling makes it a peer of B rather than a
  winner; prefer whichever measures better on quality.
- **If only B serves**, take B and record that the fleet's Mistral is a third-party quant —
  worth knowing, since it has no vendor support story.
- **If none serves on 26.07 TP=2**, the European slot waits for a container, and that is a
  defensible answer rather than a gap.

---

## Method notes folded back into [[model-discovery]]

- Check **packaging coherence from the file listing before downloading**. One
  `curl -s https://huggingface.co/api/models/<repo> | jq -r '.siblings[].rfilename'` shows
  whether a repo is HF, native, or both — and *which file carries the quant config* decides
  which flag trio works. This sweep's whole answer came from that one call.
- The NVIDIA forum indexes **per-model threads titled "Running \<model\> on DGX Spark"**.
  Searching the model name plus "DGX Spark" returns working serve commands verbatim, which
  is far better than searching the error text.
- Size with the API, not the CLI: `?blobs=true` and sum `.siblings[].size` in `jq`. It is
  exact and avoids the SI-vs-GiB conversion trap.
- `hf models ls --author mistralai --sort last_modified` produced the field. The
  `--num-parameters` fit-window search returned mostly GGUF and merge noise.
