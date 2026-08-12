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
caught **five of the eight** failures below — which is the one thing this file can say
that the skills cannot, because the evidence is the catalogue underneath it.

It is an **assembly, not an owner**: the size and `config.json` checks belong to
[[model-evaluation]], the probes and the chat-template read to [[model-bringup]]. Go there
for what the answers mean; this list is only the order that costs least.

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

### Smoke gate fails `tool-shape: 400` — model loads, serves, and is still refused
**Cause.** The engine has **no** tool parser at all, so it rejects the request shape Open
WebUI sends. Note what this is *not*: the model is fine. `ready: yes`, `quality: pass`,
weights loaded, endpoint answering. Only the gate says no.
**This is the bill for the entry above.** "Ship without tool flags rather than guessing"
is safe for *serving* — plain chat is unaffected — but it is not free: a profile that
cannot pass the smoke gate **cannot be activated by a sweep**, so it silently drops out
of every measurement. Nemotron's TP=1 and TP=2 halves both failed this way on 2026-08-10,
costing a whole pair of the TP=2 comparison.
**Check — read the chat template; it is authoritative and costs nothing.**
`chat_template.jinja` (or `chat_template` inside `tokenizer_config.json`) spells out the
exact tool syntax the model was trained on:
- `<tool_call>{"name": …, "arguments": …}</tool_call>` → **hermes**
- `<tool_call><function=NAME><parameter=KEY>value</parameter></function>` → **qwen3_coder**
This is how `hermes` was settled for Qwen3-VL after `qwen3_xml` returned HTTP 200 with
garbage, and how `qwen3_coder` was settled for Nemotron-3-Puzzle. The template plus a name
**already proven on this cluster** is evidence, not a guess — which is the bar, since the
failure mode for a wrong *name* is a refusal to start and another deploy.
*2026-08-10 · `nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4{,-single}`*

### Metrics silently stop: dashboards empty, Prometheus targets still `up`
**Two independent causes, both on 2026-08-09, both invisible to a liveness check.**

**GPU exporter — NVML broken by `daemon-reload`.** `nvidia_smi_command_exit_code 255`,
`nvidia_smi_failed_scrapes_total` climbing, HTTP 200 throughout and the Prometheus target
**up** for ~15 hours. A `systemctl daemon-reload` during any deploy breaks NVML inside a
container that does not share the host cgroup namespace — the README's own gotcha, and a
deploy runs daemon-reload every time. **Fix:** `cgroup: host` in the compose file.

**node-exporter — the `cpufreq` collector blocks on GB10.** Sampled alone, 4 of 5
requests took >6s while every other collector answered in <10ms. Full scrapes hung,
Prometheus timed out at its 10s default, and each aborted scrape **leaked** the in-flight
counter until it hit the cap of 40 and every scrape 503'd forever.
**Fix:** `--no-collector.cpufreq` (the cause) plus `--web.max-requests=0` (so a future
stall cannot become a permanent wedge). Neither costs anything: CPU utilization comes
from `node_cpu_seconds_total`, not cpufreq.

**Check.** Assert metrics are **produced**, not that the endpoint answers — the panel now
does this per node (`_exporter_ok`): a non-zero `nvidia_smi_command_exit_code` is a
failure, and so is an exporter emitting almost no series, whatever the status code.
And when diagnosing a hanging exporter, **sample collectors repeatedly** — one sample
each showed everything healthy and hid a collector that fails 4 times in 5.
*2026-08-09 · node-exporter + nvidia_gpu_exporter*

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

### `rsync: send_files failed to open ".cache/huggingface/trees/….json": Permission denied (13)`
**Cause.** `hf download` leaves its own bookkeeping *inside* the model directory —
`.cache/huggingface/` with locks, `.metadata` stubs and, since a newer CLI, a
`trees/*.json` written **mode 0600 owned by `vllm`**. The model mirror's sender runs as
`deploy`, which cannot read another user's 0600 file, so rsync exits **rc 23** and fails
the deploy. Note where it fails: *after* transferring the weights. 75 GiB moved, then the
task aborts on a 7 KiB metadata file.
**Why it had never fired.** Every staged model carries a `.cache`; only the newest
download carried `trees/`. A latent break in the mirror, waiting for an upstream CLI to
add one subdirectory.
**Check.** There isn't a useful pre-flight — the fix is structural, and it is in place:
the mirror now passes `--exclude=.cache/`, asserted by `tests/test_roles.py`. That is
correct on the merits rather than a workaround: this is resume metadata for an interrupted
fetch, and the mirror is not a resumable fetch. vLLM never reads any of it.
**Recovery.** Re-run the deploy. rsync `--size-only --partial` resumes, so the 75 GiB
already on the worker is not re-sent.
*2026-08-10 · NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4*

### `DistNetworkError: The server socket has failed to listen on any local network address. port: 29501`
**Cause.** A **previous** engine's unit was still in its `RestartSec` gap when the next one
was activated, and the reconciler did not stop it. The stop plan was
`[e for e in others if active.get(e)]`, fed by `systemctl is-active --quiet`, which is
true only for `active`. A failing unit between restarts reads `activating` (sub
`auto-restart`) — **not serving, and not free either**. The plan recorded `"stop": []`,
systemd restarted the old engine twenty seconds later, it took port 29501 back, and the
incoming head failed to bind five times in ~100 s (`StartLimitBurst=5`, `RestartSec=20`)
and was quarantined.

The engine that would not die was `mistral-medium-3.5-128b-nvfp4`, failing on **DEF-0012**
exactly as its register row predicted. The engine that paid for it was
`qwen3-vl-235b-a22b-instruct-nvfp4`, which had served fine that morning — so **the visible
casualty was not the broken model**, and the head's `init_process_group` traceback points
at the network, not at the neighbour holding the port.
**Check.** Two, and the first is the one that would have cost nothing:

1. **Before putting a profile in a runbook or campaign, grep the defect register for it.**
   `grep -i <profile> docs/defects.md` — DEF-0012 said "the engine never serves, text
   included" and named the WAR in the profile as one that does not work. `updating.md`
   ends every pathway by consulting that register; this run did not, and a 🔴 row is
   exactly the signal that a profile belongs behind `blocked: true` rather than in an
   unattended job list.
2. The reconciler now stops any unit that is **not `inactive`**, rather than only those
   that are `active` (`plan()` / `unit_state()`, `tests/test_activate.py`). Enumerating
   which states hold resources is what invited the omission — the only state that owns
   nothing is `inactive`.

**Second-order.** A campaign quarantines per profile, which contained the blast radius but
also disguised it: two profiles were quarantined and only one was broken. When two
consecutive activations fail, suspect the *first* one's corpse before believing the second
model is at fault.
*2026-08-11 · Mistral-Medium-3.5 → Qwen3-VL-235B · DEF-0012*

### `AttributeError: 'Step3VLProcessor' object has no attribute '_get_num_multimodal_tokens'`
**Cause.** *Re-diagnosed 2026-08-11 — the two earlier readings of this error were both
wrong, and each cost a month of parking.* The traceback's own path is the tell:
`vllm/model_executor/models/transformers/multimodal.py` is vLLM's **generic
transformers-backend fallback**, entered only when vLLM has **no native implementation of
the architecture**. So the error is not really about a missing method; it is about being
on the fallback path at all.

And the object it names belongs to neither of the repos previously blamed. This checkpoint
is `Step3p7ForConditionalGeneration`, whose `config.json` carries an `auto_map` pointing at
**remote code inside the checkpoint directory** —
`"AutoProcessor": "processing_step3.Step3VLProcessor"`. The `Step3VLProcessor` in the
traceback is therefore a file in `/opt/vllm/models/<model>/processing_step3.py`, shipped by
StepFun with the weights. It defines `get_num_image_tokens`, never the underscore-prefixed
method the fallback demands. Verified: our staged copy is byte-identical (sha256
`983f9fc3…`) to the Hub's current file, and **neither** `stepfun-ai/Step-3.7-Flash-NVFP4`
nor `stepfun-ai/Step-3.7-Flash` defines the method as of today.

Three repos were blamed in turn — vLLM, then transformers, then the container — and the
file was on our own disks the whole time. **When a traceback names a class, find out which
package actually defines it before deciding whose bug it is**; a checkpoint with an
`auto_map` and a global `--trust-remote-code` is a fourth source of code in the process,
and it is the one nobody thinks of.

**Check.** Ask whether vLLM implements the architecture, not whether some class has some
method:
```bash
./sparky.sh probe archs Step3p7ForConditionalGeneration
```
`probe attr` was the wrong instrument here and misreported for a month: it returns `false`
for an **absent class** exactly as it does for an absent method, so
`probe attr vllm.model_executor.models.step3_vl Step3VLProcessor._get_num_multimodal_tokens`
answered `false` on 2026-08-10 because `step3_vl` has no `Step3VLProcessor` — a module for
a *different* model (Step-3, not Step-3.7). Use `probe archs` for "does vLLM know this
model"; reserve `probe attr` for a method on a class you have confirmed exists.

**Unblock path.** vLLM **0.24.0 (26.07) ships a native `step3p7` module**, so the fallback
that raised is never entered. Probed 2026-08-11: `probe archs Step3p7ForConditionalGeneration`
→ `true`, and `probe attr vllm.model_executor.models.step3p7 Step3p7ForConditionalGeneration`
→ `true`. The profile was unparked on that basis — no new container required.

If it recurs anyway (config/processor resolution can still reach `auto_map` under the
template's global `--trust-remote-code` even when the *model* class is native), the next
option is StepFun's own `vllm/vllm-openai:stepfun37`. Its `stepfun37-arm64-cu130` variant
is real, is `linux/arm64`, and carries `TORCH_CUDA_ARCH_LIST=… 12.0 12.1` — native sm_121
cubins, where NGC 26.07 reaches sm_121 only via `12.0+PTX`. **But it is not a drop-in
pull**: it sets `ENTRYPOINT ["vllm","serve"]` while our unit already appends
`vllm serve /models/…`, so the command would double up. Adopting it means a derived image
that clears the entrypoint (`roles/images/files/<context>/`), not a `pull:` line. Its
`NVIDIA_REQUIRE_CUDA` also caps at `driver<576` against our 580.159.03, which the probe
would not catch — the probe runs without `--gpus`, so the runtime never evaluates it.
*2026-08-11 · Step-3.7-Flash-NVFP4 → DEF-0006*

### Engine never becomes ready — `No available shared memory broadcast block found in 60 seconds` (head) + `DistBackendError … possible application crash on rank 0` (worker)

**Cause.** The two ranks were given **different serve flags**, so they built different
`VllmConfig`s and disagreed about how many collectives to run. TP=2 then deadlocks: each
blocks on a collective the other never issues. The head loops the shm-broadcast message
forever; the worker sits in whatever collective it reached and eventually dies blaming
rank 0 — **which is a red herring**, rank 0 did not crash.

Seen 2026-08-12 with `--speculative-config` in `head_extra_args` only. Rank 0 ran EAGLE's
extra draft profiling pass; rank 1 did not. Rank 1 blocked ~12 minutes in
`profile_run → _dummy_sampler_run → compute_logits → tensor_model_parallel_all_gather`.
Cost: an hour of stalled cluster, and **no error in either log named the flag**.

**Check.** Compare the ranks — the fastest possible diagnosis, and it is conclusive:
```bash
grep -o "VLLM_SERVE_ARGS=.*" /opt/vllm/engines/<engine>.env | tr ' ' '\n' | sort > /tmp/head
ssh <worker> "grep -o 'VLLM_SERVE_ARGS=.*' /opt/vllm/engines/<engine>.env" | tr ' ' '\n' | sort | diff /tmp/head -
```
Ignore `--node-rank`, `--headless` and `--master-addr`, which are *supposed* to differ.

**Fix / prevention.** Put every MODEL-configuring flag on **both** ranks. `sparky lint`
now enforces this for `fleet.BOTH_RANK_FLAGS` (`--tokenizer-mode`, `--config-format`,
`--load-format`, `--speculative-config`, `--quantization`, `--kv-cache-dtype`) and refuses
the deploy — so this class cannot reach an activation again. API-surface flags
(`--tool-call-parser`, `--reasoning-parser`, `--enable-auto-tool-choice`) are head-only by
design and are deliberately NOT in that set.

**The pattern.** This was the third rank-desync in two days — `--tokenizer-mode` head-only,
then `--config-format` without `--load-format`, then this. Each presented completely
differently (a config-time refusal, a `KeyError` at weight load, a silent deadlock), which
is exactly why it kept being re-diagnosed from scratch instead of recognised.
*2026-08-12 · mistral-small-4-119b-2603-nvfp4-eagle*

### `assert m.max_query_len <= self.reorder_batch_threshold  # decode only` → `AssertionError`

**Cause.** **MLA attention and speculative decoding are incompatible on the `TRITON_MLA`
backend.** `TRITON_MLA` is decode-only — it asserts one query token per sequence — but
spec-decode submits `num_speculative_tokens + 1` query tokens when the target model
verifies a proposal. Fires in `build_for_cudagraph_capture`, after the weights load, so it
looks like a late/mysterious startup failure rather than a flag conflict.

**Check.** Before combining spec-decode with an MLA model (`kv_lora_rank` present in
`params.json`/`config.json`), grep the startup log for the backend vLLM chose:
```bash
journalctl -u vllm@<engine>.service | grep -oE "Using [A-Z_]+ (MLA )?(attention|prefill) backend"
```
`Using TRITON_MLA attention backend` plus a `--speculative-config` is the failing pair.
Note prefill and decode can differ — ours used `FLASH_ATTN MLA` for prefill and
`TRITON_MLA` for decode, and only decode asserts.

**Fix — and there ISN'T one on sm_121 / vLLM 0.24.0.** Mistral's recipe specifies
`--attention-backend FLASH_ATTN_MLA`, and it was worth trying, but the flag parses and then
the backend refuses:

    ValueError: Selected backend AttentionBackendEnum.FLASH_ATTN_MLA is not valid for
    this configuration

FLASH_ATTN MLA is valid for **prefill** on this hardware (every activation logs
`Using FLASH_ATTN MLA prefill backend`) but not for **decode**. So both MLA decode
backends are ruled out — `TRITON_MLA` forbids multi-token queries, `FLASH_ATTN_MLA` does
not exist for this configuration. **Speculative decoding and MLA cannot be combined here.**

The only lever left is `VLLM_MLA_DISABLE=1`, which trades ~25x the KV per token
(22.5 KiB → ~576 KiB) and a context ceiling near 40k for maybe 1.5–2x decode. Not worth it.
**THE CHECK, and it is one grep of a log you already have.** vLLM prints the backends it
considered, and for this model on sm_121 it considered exactly one:

```bash
journalctl -u vllm@<engine>.service | grep -oE "out of potential backends: \[[^]]*\]"
#   out of potential backends: ['TRITON_MLA']
```

**A single-entry list is the answer.** vLLM's enum has six MLA backends — `TRITON_MLA`,
`FLASH_ATTN_MLA`, `FLASHINFER_MLA`, `FLASHMLA`, `CUTLASS_MLA`, `FLASHMLA_SPARSE`, all
present in the build (`probe attr vllm.platforms.interface AttentionBackendEnum.<NAME>`) —
but only the ones in that list are *available for this model on this hardware*. If it
contains only `TRITON_MLA`, no `--attention-backend` value can help, because there is
nothing else to select. FlashInfer being installed (0.6.14 here, the version Mistral's own
recipe names) does **not** make `FLASHINFER_MLA` available; availability is decided per
model and platform, not by the package being present.

Doing this grep first would have saved two of the three activations spent here.

**And clean up the corpse before retrying.** A worker that fails while the head restarts
leaves rank 1 alive, spinning `Failed to check the "should dump" flag on TCPStore` against
a dead rendezvous, which poisons the next attempt too. The fastest tell is **asymmetric
memory** — a healthy TP=2 holds a similar amount on both nodes, so 8 GiB on the head
against 47 on the worker means the head never loaded and the worker is left over. `free -g`
on both nodes beats reading either log.
*2026-08-12 · mistral-small-4-119b-2603-nvfp4-eagle*
