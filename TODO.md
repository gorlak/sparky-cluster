# Cleanup / deferred work

Low-priority tech-debt and polish. Not a roadmap — "maybe someday, maybe not."
Feature/roadmap items live in the README's *Future Work* section instead.

---

## Make node identity fully inventory-driven

Today the cluster is *mostly* config-driven: playbooks target the `head`/`worker`
groups and roles/templates pull per-host details (`vllm_host_ip`, `node_rank`)
from `ansible/inventory.yml`. But a few identity/IP/domain values are hardcoded
**outside** the inventory, so adapting the repo to a differently-named cluster
means editing several spots instead of just the inventory:

- `ansible/group_vars/all.yml` — `master_addr: 10.0.200.12` **duplicates** the
  head's IP; `web_domain: sparky.flummoxed.net` bakes in the head's domain.
- `ansible/Makefile` — `SNOOPY := deploy@10.0.200.13` (worker IP, used by
  `logs-worker`).
- `ansible/bootstrap-deploy.sh` — worker IP `10.0.200.13` and a
  `hostname == "sparky"` control-node assumption.

**Possible fix:**
- Derive `master_addr` from the head host:
  `master_addr: "{{ hostvars[groups['head'][0]].vllm_host_ip }}"` (removes the
  duplication outright).
- Keep `web_domain` as a single, clearly-labeled config knob.
- `Makefile` / `bootstrap-deploy.sh` are shell (can't easily read inventory) —
  leave each as a single top-of-file variable and add a short README
  "adapt to your cluster" section listing exactly what to change.

**Why:** makes the repo genuinely clone-and-edit-one-place for others with
similar hardware but different hostnames/IPs.

> **Note (2026-05-25):** the `serving_topology` refactor in
> [`docs/serving-topology.md`](docs/serving-topology.md) makes node identity
> inventory-only (head/worker becomes a per-engine computed rank, `master_addr`
> per-engine) — its T1 phase closes the Ansible-side of this item. The shell
> leaks (`Makefile`, `bootstrap-deploy.sh`) remain as documented above.

---

## Talkie — its own profile via `algal/talkie-server` (fun lane)

Talkie-lm uses a custom `TalkieForCausalLM` arch that **neither vLLM nor stock
Ollama can load** (verified 2026-05-26: stock Ollama errors `unknown model
architecture: 'talkie'`). [algal/talkie-server](https://github.com/algal/talkie-server)
is a small FastAPI wrapper around plain HuggingFace `transformers` with
`trust_remote_code=True` that exposes an OpenAI-compatible API
(`/v1/chat/completions`, `/v1/models`, `/health`) — the right runtime for us.
It's already tested on DGX Spark / Grace+GB10 / sm_121.

**The plan:**
1. Clone `algal/talkie-server`, pin `torch==2.X+cu130` from the **release** index
   (`https://download.pytorch.org/whl/cu130`) instead of the README's nightly —
   stable cu130/aarch64 wheels exist now (2.9.0 through 2.12.0 as of 2026-05-26).
2. Containerize it (matches our `vllm`/`ollama` pattern), pinned torch version.
3. Open a PR back to `algal/talkie-server` swapping nightly → stable cu130.
4. Add a new `kind: talkie` to `serving_topology` + a `roles/talkie` (template a
   `talkie-<name>.service`, the same prune/bring-up shape as the `ollama` role).
   Extend T3's compose template to include `kind: talkie` engines in
   `OPENAI_API_BASE_URLS` (they serve `/v1`).
5. Ship a **`fun` profile** with just talkie on sparky to evaluate it in isolation.
6. If it earns its keep (more than a novelty), fold talkie into `multi` as
   sparky's idle-capacity lane while it lends TP.

**Cost to plan around:** talkie-server loads BF16 (~26 GiB resident on GPU; no
idle unload), single-request, 2048-token context, ~7 tok/s decode. Fine for fun,
not a workhorse — see [[ollama-gb10-verified]] for why Ollama's lighter lane was
ruled out.
