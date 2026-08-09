---
name: model-bringup
description: Taking a staged model from the inbox to serving — batching profiles into one deploy, probing the container before activating, and the checkpoint traps that cost a bring-up to learn. Use after model-discovery has staged weights, or when asked to "get these models running".
---

# Model bring-up — from the inbox to serving

[[model-discovery]] finds and stages; [[model-evaluation]] decides whether one model
fits. This skill is the **operational** half: several staged models, minimum deploys,
nothing guessed.

## The cadence — batch the deploys, serialize the activations

`deploy` is the only step that needs a human and a password. Everything else — probing,
activating, smoking, benching — is unprivileged. So the shape is:

```
  deploy  →  probe + activate + learn (×N, no human)  →  ONE correction deploy
```

**Profiles batch; activations serialize.** Adding ten profiles costs exactly one deploy
and starts nothing — the profiles directory is the allowlist, and `deploy` is
selection-neutral. Activating is then one at a time, because each is a real experiment.
Writing one profile per deploy is the mistake; it turns a human into the rate limiter for
work they aren't needed for.

Corollary: a first deploy's job is to make things **probe-able and bring-up-able**, not
perfect. Ship minimal flags, learn from the probe and the first activation, then fix
everything in a single follow-up deploy.

> The exception is a **defect campaign**, where one variable per activation is the whole
> point (see `docs/upgrades/container-*.md`). That constraint is about *activations*,
> not profiles — it never argues for splitting a deploy.

## Order of bring-up

Cheapest and most informative first:

1. **Single-node before big-shared.** A 21 GiB TP=1 model proves the architecture, the
   quant path and the parser names for its whole family. Everything learned transfers to
   the 235B sibling that costs 10× the load time.
2. **Smallest footprint first.** A load that OOMs the *host* takes the node down and
   needs a physical power cycle (DEF-0004). Distance from the memory ceiling is
   distance from that outcome.
3. **Known-good architecture first.** If the container probe already confirmed one arch,
   start there — a failure is then attributable to your profile, not the container.

## Probe before you activate

`./sparky.sh probe` (ADR-0019) answers container questions in seconds, with no root:

```bash
./sparky.sh probe archs Mistral3ForConditionalGeneration Qwen3VLForConditionalGeneration
./sparky.sh probe quant          # is MIXED_PRECISION / NVFP4_AWQ / modelopt_mixed there?
./sparky.sh probe parsers        # the EXACT --tool-call-parser / --reasoning-parser names
./sparky.sh probe versions       # vllm, nccl, transformers, xgrammar, fastapi
```

An unsupported architecture fails minutes into a weight load. The probe costs twenty
seconds. There is no reason to learn it the expensive way.

**Batch the probes too** — one `probe archs` call takes every architecture you staged.

## The traps — each of these cost a real bring-up

**The repo name lies about the quantization.** Read `config.json`, never the directory
name. `Mistral-Medium-3.5-128B-NVFP4` is `quant_algo: MIXED_PRECISION` — 367 FP8 layers
and 249 NVFP4 ones. `Qwen3-VL-32B-Instruct-NVFP4` is `NVFP4_AWQ`. Both change which
kernels load and what the footprint is.

**Never pass `--quantization` for a self-declaring checkpoint.** Double-quantization
produces fluent garbage, and it looks like a model quality problem rather than a config
one. The oldest footgun on this cluster.

**A `tokenizer.json` in the directory does not mean vLLM will accept it.** Mistral
checkpoints ship both HF and native artifacts, and vLLM still validates the tokenizer
*type* for the architecture:

> `ValidationError: Value error, The tokenizer must be an instance of MistralTokenizer`

That refusal happens at config time, after quantization detection — so a bring-up that
gets far enough to log its quant path can still die on the tokenizer. `--tokenizer-mode
mistral` is required, and it belongs in **both** `head_extra_args` and
`worker_extra_args`, because every rank builds its own `VllmConfig`.

**A guessed parser name is a refusal to start, not a warning.** Probe `parsers` and use
the exact string. Same for `--tokenizer-mode`.

**Check `kv_cache_scheme` / `kv_cache_quant_algo`.** A checkpoint declaring FP8 KV puts
the profile in DEF-0007 territory (FP8 KV × prefix caching → multi-turn corruption). Omit
`--enable-prefix-caching` until that's re-tested; it is the half you can add back for
free.

**Size the context to the KV budget, not the model card.** DeepSeek-V4-Flash advertises
1M tokens; at ~78 GiB/node of weights there is ~18 GiB of KV per rank, so 131072 is the
honest number. `max_model_len` is a memory decision.

## Writing the profile

Copy the profile whose **shape** matches — `minimax-m2.7-nvfp4.yml` for big-shared TP=2,
`qwen3-coder-nvfp4-single.yml` for single-node on snoopy — then per
[[model-evaluation]]'s memory math set `gpu_memory_utilization` from your
*outside-headroom* target, not from "as high as it goes".

Start with the **minimal flag set**: `--enable-chunked-prefill`, plus only what the
checkpoint provably needs. Add parsers after probing. Every flag you cannot justify is a
way for the bring-up to fail for a reason unrelated to the model.

`./sparky.sh lint` validates the whole allowlist — fleet-wide-unique engine names, the
single front port, and flags that survive the env-file round trip. Run it before the
deploy, not after.

## After the first activation, read the log for

- which quantization path was chosen (`modelopt_mixed`? `Marlin`? — Marlin on a MoE is
  DEF-0004, a node-killer)
- whether the declared FP8 KV was honoured or silently fell back to bf16 — it changes
  concurrency by 2×
- `Available KV cache memory` against your predicted number; a large miss means the
  memory math was wrong and the profile needs re-tuning, not the model rejecting
- the smoke gate's tool-shape column, which is what caught DEF-0010

Then record it: a fact sheet in `docs/models/`, a `DEF-NNNN` row for anything carried,
and a row in `docs/models/tombstones.md` if the model is rejected outright
([[documentation]]).
