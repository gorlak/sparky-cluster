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

## Privatize repo for public Codeberg publishing

Remove personal identifiers so the repo is safe to publish publicly. Approach:
`.example` files for anything gitignored; generic placeholders in docs.

**What to extract into gitignored files:**

- `ansible/inventory.yml` → gitignore it; commit `ansible/inventory.yml.example`
  with `<head-node>` / `<worker-node>` / `<head-ip>` / `<worker-ip>` placeholders.
- New `ansible/group_vars/local.yml` (gitignored) + `local.yml.example` — moves
  these out of the committed `all.yml`:
  - `web_domain` (currently `sparky.flummoxed.net`)
  - `master_addr` (currently `10.0.200.12` — also tracked by the "inventory-driven"
    item below; can be derived instead: `hostvars[groups['head'][0]].vllm_host_ip`)
  - `admin_user` (currently hardcoded as `geoff` in `bootstrap-deploy.sh`)

**What to update in committed files:**

- `bootstrap-deploy.sh` — replace hardcoded `ADMIN_USER=geoff`,
  `SNOOPY=geoff@10.0.200.13`, SSH key path with variables sourced from a
  local config or with clear top-of-file "edit these" comments.
- `ansible/Makefile` — replace `SNOOPY := deploy@10.0.200.13` similarly.
- Hostname guard `[[ "$(hostname)" == "sparky" ]]` in bootstrap — replace with
  a configurable `HEAD_HOSTNAME` var or remove the guard entirely (it's just a
  safety check).
- Docs/comments — replace `sparky`/`snoopy`/`geoff`/`flummoxed.net` with
  `<head-node>`/`<worker-node>`/`<admin>`/`<your-domain>` throughout README,
  docs/, and script comments.

**Nice-to-have:** rename the Ansible inventory hosts from `sparky`/`snoopy` to
`head`/`worker` so profile YAMLs (`nodes: [sparky, snoopy]`) don't leak
hostnames either. More invasive — do last or separately.

**Test approach:**
1. `ansible-playbook --syntax-check site.yml` with example files in place —
   catches missing variable references before touching the cluster.
2. `shellcheck ansible/bootstrap-deploy.sh` — catches shell regressions.
3. `make check PROFILE=step-3.5-fp8` (dry run) — confirms templates render correctly.
4. Full `make deploy` on the actual cluster to confirm end-to-end.

> Cross-reference: the "Make node identity fully inventory-driven" item below
> overlaps on `master_addr` and the shell leaks — this item supersedes the
> shell parts of that one.

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
6. If it earns its keep (more than a novelty), fold talkie into `minimax-m2.7-awq`'s
   sparky-side headroom while minimax lends TP — note talkie's ~26 GiB BF16
   weights would eat most of `minimax-m2.7-awq`'s ~30 GiB outside-vLLM budget at the
   current `gmu 0.75`, so the gmu may need adjusting.

**Cost to plan around:** talkie-server loads BF16 (~26 GiB resident on GPU; no
idle unload), single-request, 2048-token context, ~7 tok/s decode. Fine for fun,
not a workhorse — see [[ollama-gb10-verified]] for why Ollama's lighter lane was
ruled out.
