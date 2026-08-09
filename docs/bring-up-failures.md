# Bring-up failure catalogue

**Paste the error here first.** Every entry is keyed on the literal text vLLM prints, so
this file is meant to be grepped when a bring-up fails, before anything is theorised.

**The rule for adding one: each entry must name a check that would have caught it
*before* the deploy.** An entry without a pre-flight check is a diary; an entry with one
is a process improvement. That is the same discipline as a defect's *clears-when* and a
tombstone's *reconsider-when* — the field that makes the record do work later.

**This is not the defect register.** [`defects.md`](defects.md) tracks defects the cluster
*carries*, with workarounds in place. This tracks **failure modes we have hit**, most of
which are our own misconfiguration or a mis-packaged checkpoint, and each of which has a
cheap check. A failure here graduates to a `DEF-NNNN` only if we end up carrying it.

Read with [[model-bringup]], which sequences the bring-up itself.

---

## The pre-flight, in cost order

Run these before writing a profile. Together they cost under a minute and would have
caught **five of the eight** failures below.

```bash
du -sh /opt/cluster/model-cache/<model>/          # 1. real size, for the memory math
```
```bash
python3 -c "import json;c=json.load(open('/opt/cluster/model-cache/<model>/config.json'));q=c.get('quantization_config',{});print(c['architectures'],q.get('quant_algo'),q.get('kv_cache_scheme') or q.get('kv_cache_quant_algo'))"
```
```bash
./sparky.sh probe archs <Architecture>            # 3. does this build know it?
```
```bash
./sparky.sh probe quant                           # 4. is the quant_algo in modelopt_algos?
```
```bash
./sparky.sh probe parsers                         # 5. candidate parser names
```
```bash
grep -oE '<tool_call>|\[TOOL_CALLS\]|<function|<parameter' /opt/cluster/model-cache/<model>/chat_template.jinja | sort | uniq -c   # 6. the format the model ACTUALLY emits
```

---

## Catalogue

### `Value error, The tokenizer must be an instance of MistralTokenizer`
**Cause.** The checkpoint requires `--tokenizer-mode mistral`. Ours shipped *both* HF
(`tokenizer.json`) and Mistral-native (`tekken.json`, `params.json`) artifacts, and the
presence of the HF one was read — wrongly — as permission to use the HF path.
**Check.** `ls` the model directory for `tekken.json` / `params.json`. If present, the
checkpoint is Mistral-native regardless of what else is there. The fact sheet said so and
was overridden; **believe the fact sheet over file listings**.
**Note.** The flag belongs in `head_extra_args` **and** `worker_extra_args` — every rank
builds its own `VllmConfig`.
*2026-08-08 · Mistral-Medium-3.5*

### `ModelOpt currently only supports: ['FP8', …, 'MIXED_PRECISION'] quantizations`
**Cause.** The checkpoint's `quant_algo` (here `NVFP4_AWQ`) is not in ModelOpt's
allowlist. Fails at config validation, before any weight load.
**Check.** `./sparky.sh probe quant` → compare the checkpoint's `quant_algo` against
**`modelopt_algos`**, *not* against `methods`. Those are different, and the shorter one
is the binding constraint: `modelopt_fp4` appears among the methods, which is exactly
what made `NVFP4_AWQ` look supported when it was not.
*2026-08-08 · Qwen3-VL-32B → DEF-0013*

### `ValueError: Mismatch in 'image' token count between text and input_ids. Got ids=[0] and text=[1]`
**Cause.** Tokenizer and processor disagree. `--tokenizer-mode mistral` (tekken) does not
emit the `[IMG]` token id HF's `PixtralProcessor` expects from the prompt text, so the
processor sees one image in the text and none in the ids. A **hybrid** of the two paths,
which neither expects.
**Check.** If a checkpoint needs `--tokenizer-mode mistral`, it likely needs
`--config-format mistral` too — vLLM's recipes prescribe them **as a set**. Adopting half
a recipe is its own failure mode.
**Also.** `--limit-mm-per-prompt {"image":0}` does **not** avoid this — vLLM runs
multimodal profiling regardless of the limit.
*2026-08-08 · Mistral-Medium-3.5 → DEF-0012*

### `AssertionError: DeepseekV4 fp8_ds_mla layout only supports fp8 kv-cache, got auto`
**Cause.** The model's MLA layout **requires** `--kv-cache-dtype fp8`. The checkpoint does
not declare `kv_cache_quant_algo`, so vLLM defaulted to `auto` (bf16) and the assert fired.
**Check.** If `config.json` shows MLA-shaped attention (`num_key_value_heads: 1` with a
large `head_dim`, e.g. 512) and declares **no** KV scheme, expect to set the KV dtype
explicitly. Required, not tuning.
*2026-08-08 · DeepSeek-V4-Flash*

### `ImportError: cannot import name 'normalize_tool_choice' from 'xgrammar'`
**Cause.** The image shipped a dependency **below vLLM's own declared minimum**. Every
tool-calling request 500s — including ordinary chat, because Open WebUI sends
`tool_choice: "auto"`.
**Check.** `./sparky.sh probe pip <package>` after any container bump, and `probe
versions` for the usual suspects. A `pip check` inside the image would have caught it.
*2026-08-08 · container 26.07 → DEF-0010*

### `ImportError: Support for Transformers v4 is deprecated and was removed in vLLM v0.24.0`
**Cause.** A derived image patched one package **without `--no-deps`**; pip re-resolved
the vendor's carefully-pinned set and downgraded `transformers`.
**Check.** Every `pip install` in a derived Dockerfile carries `--no-deps`, plus
build-time asserts so a broken WAR fails the *deploy* rather than every engine. Enforced
by `tests/test_roles.py`.
**Second-order.** The corrected Dockerfile was then silently ignored, because the images
role gated `docker build` on the tag already existing — **a tag says nothing about which
Dockerfile produced it.** Also now enforced by test.
*2026-08-08 · the xgrammar WAR*

### Host OOM during weight load → **the node freezes**
**Cause.** MiniMax-AWQ on 26.07 exhausted host RAM at `Loading safetensors … 0/27` and
took sparky down; recovered only by power cycle. Not a headroom problem — a larger
checkpoint loads fine with less free RAM. The Marlin MoE load path (DEF-0004) is the
variable.
**Check.** Bring up the **smallest** footprint of a family first, and treat anything near
~78 GiB/node as needing a human present. The ADR-0009 boot gate then keeps the node empty
and reachable rather than re-attempting the load that killed it.
*2026-08-08 · MiniMax-M2.7-AWQ → DEF-0004*

### `RuntimeError: Assertion error (…layout.hpp): Unknown SF transformation`
**Cause.** DeepGEMM — the block-scaled FP8 GEMM library — has no scale-factor layout
transformation for this checkpoint's format on **sm_121**. A kernel gap, not a
misconfiguration: weights load completely (48/48 shards, memory flat) and it fails during
worker init.
**Check.** There is no cheap pre-flight for this one, and that is worth stating plainly —
`probe archs` and `probe quant` both pass, because the architecture *is* supported and the
quantization *is* accepted. Kernel coverage for a given (format × architecture) pair is
not introspectable from outside a real load. **Bring these up expecting a surprise**, and
prefer a family we have already run on GB10.
**WAR tried, and instructive.** `VLLM_USE_DEEP_GEMM=0` (through the new `engine_env:`)
removed this error entirely — and exposed the next one, `RuntimeError: dispatch_scaled_mm`
in CUTLASS c3x. **Both** FP8 block-scaled GEMM paths lack sm_121 coverage, so no flag
reaches past it. A workaround that reveals a second wall has still earned its keep: it
converted "maybe misconfigured" into "the hardware is not supported by this build".
*2026-08-08 · DeepSeek-V4-Flash → DEF-0014*

### Tool calls return **HTTP 200** with empty or garbage arguments
**Cause.** The `--tool-call-parser` name is *valid* but *wrong for this model*. vLLM
validates the name at argparse time and nothing else — a parser that does not understand
the model's output format then extracts nothing, or leaks template fragments:

    auto      -> 200, no tool_call at all
    required  -> 200, get_weather({})  or  {"city": "<value=Paris>"}
    named fn  -> 200, correct  ← misleading: vLLM constrains this shape itself

**This is worse than a crash**, because everything looks healthy: the smoke gate's
tool-shape column shows 200, and a status-code-only probe reports PASS. The damage
surfaces later as an agent that silently calls tools with missing arguments.
**Check.** Probe all four shapes and **validate the arguments against the request** —
status codes prove nothing. Note the named-function shape is the least informative: it
passes even with a mismatched parser. `auto` is the one that matters, since it is what
Open WebUI sends.
**Also.** Same-vendor is not same-format: `qwen3_xml` serves `qwen3.6-35b` correctly and
is wrong for `Qwen3-VL-235B`. Qwen alone ships at least three tool formats.

**THE CHEAP CHECK, and it should come first — read the chat template.** The model states
its own format, in the file, before anything is deployed:

```bash
grep -oE '<tool_call>|\[TOOL_CALLS\]|<function|<parameter' /opt/vllm/models/<model>/chat_template.jinja | sort | uniq -c
```

`<tool_call>{"name":…,"arguments":…}</tool_call>` → `hermes`. `[TOOL_CALLS]` → `mistral`.
This settled Qwen3-VL-235B in ten seconds after a wrong guess had already cost a deploy
cycle. Prefer it over any inference from the model family.
*2026-08-08 · Qwen3-VL-235B*

### Engine refuses to start on an unknown `--tool-call-parser` / `--reasoning-parser`
**Cause.** A guessed parser name. Validated against `choices=` at **argument-parse time**,
so it fails in seconds — but changing it costs a whole deploy, which is the expensive part.
**Check.** `./sparky.sh probe parsers` — but read it carefully. Three attempts at an
authoritative list failed: the registry stays empty even after all 43 parser modules
import cleanly, and vLLM's own argument parser (which *is* authoritative) cannot be built
without a GPU, which the probe deliberately lacks. What comes back is the **module list**,
a candidate set rather than a lookup — `qwen3_xml` is a name we serve with today whose
module is `qwen3_engine`.
**Prefer a name already proven on this cluster** — `hermes`, `minimax_m2`, `qwen3_xml`,
`step3p5`, `mistral`, `deepseek_r1` — ideally from the same vendor family. If no module
suggests a parser at all (as for Nemotron), ship **without** tool flags rather than
guessing: plain chat is unaffected, and a wrong name costs a deploy.
*2026-08-08 · the Qwen3-VL / Nemotron / DeepSeek profiles*

---

## What the pattern says

Seven of eight are **checkpoint or configuration** problems, not cluster problems: the
container was fine every time, every architecture probed as supported, TP=2 and NCCL were
solid. What breaks is the *file we staged* and the *flags we chose*.

Two consequences worth keeping:

1. **The repo name lies.** `…-NVFP4` was `MIXED_PRECISION` once and `NVFP4_AWQ` another
   time. Read `config.json`; never the directory name.
2. **Instrument rather than reason.** Three recorded beliefs turned out false on
   2026-08-08 — FP8 KV is auto-enabled from the checkpoint, prefix caching is on by
   *default*, and the smoke gate silently skipped `quality` whenever tool-shape failed.
   None were discovered by thinking; all three came from reading what the engine actually
   logged. When a doc asserts a runtime state, **go and check the runtime**.
