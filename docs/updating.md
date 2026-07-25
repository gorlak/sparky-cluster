# Update pathways

The cluster is built on **projection** — a profile declares the serving topology once
and every dependent (vLLM units, Prometheus, Open WebUI, the control panel, Caddy) is
generated from it (ADR-0003), so a *profile switch* touches exactly one file. This doc
is for the changes that **don't** project automatically — container bumps, new
models/roles, front-end settings — where several hand-maintained spots must move
together. Each pathway is a checklist of **every place to touch**, ending with the same
two steps:

- **Defects.** Consult [`defects.md`](defects.md) filtered to what you changed, and
  **re-test one at a time** any row whose *Clears when* your change satisfies. This is
  how an update efficiently closes out debt instead of silently carrying it.
- **Validate.** `./sparky.sh lint` → `check <profile>` → deploy behind the fail-safe
  net (ADR-0009) → `smoke` → (for serving/perf changes) `bench`.

Write the ADR / update the living docs **in the same commit** as the change
([documentation skill](../skills/documentation/SKILL.md)).

---

## Bump the vLLM container image

E.g. NVIDIA ships `26.07-py3` and you want a profile (or the default) on it.

1. **Get the digest.** `sudo docker images --digests` on a node → the new `sha256:`.
2. **`ansible/group_vars/all.yml` → `container_images`:** update the `pull:` entry to
   the new `<ref>@sha256:<digest>` (and the `vllm_image` default if the *default*
   moves — usually it doesn't; new containers are opt-in per profile).
3. **Derived image `FROM` (if one builds on it).** Update the digest in the matching
   `ansible/roles/images/files/<context>/Dockerfile` `FROM` line — this is a **second
   place the same digest lives** (the base pull + the Dockerfile base); they must move
   together. (ADR-0013.)
4. **Per-profile `vllm_image:`.** Point each profile that should run the new image at
   it (`ansible/profiles/*.yml`). Leave AWQ/Marlin-MoE profiles on 26.04 — see DEF-0004.
5. **Upgrade tracker.** Create/update `docs/upgrades/container-<coordinate>-<tag>.md`
   ([documentation skill](../skills/documentation/SKILL.md) naming) and walk its **WAR
   register** — drop any WAR whose *Remove when* now holds.
6. **Defect register** → filter [`defects.md`](defects.md) to the container rows
   (DEF-0001–0005, and DEF-0007 on a vLLM-version bump). Re-test each cleared row **one
   at a time**, behind the fail-safe net; a **soak test** is required to clear DEF-0002
   (strikes 35–55 min in). Update statuses with results.
7. **Validate** as above. The `images` role pulls/builds on every node at deploy — no
   manual `docker pull`.

## Add or change a model / profile

The mechanical steps are in the README ("Adding models / profiles"); the fan-out:

1. **Stage weights:** `./sparky.sh download <hf-repo>` (the `model` role mirrors to all
   nodes).
2. **Fit check first:** the [model-evaluation skill](../skills/model-evaluation/SKILL.md)
   — memory math, `config.json`, never `--quantization` on a self-declaring checkpoint.
3. **Profile:** copy the nearest-shape `ansible/profiles/<name>.yml` and edit
   `serving_topology` + `vllm_image`. Confirm its image is in `container_images`.
4. **Docs:** a fact sheet `docs/models/<model>.md`; the profile table in
   [`profiles.md`](profiles.md) and the README profile table.
5. **Defect register** → check [`defects.md`](defects.md) for anything gating this
   model/quant/arch (e.g. DEF-0004 for AWQ/Marlin MoE, DEF-0006 for Step-3.7 VL).
6. **Validate**: `lint` picks up the new profile automatically; then `check` → deploy →
   `smoke` → `bench <label>`.

## Add a role / always-on service

1. **Role** under `ansible/roles/<name>/` (tasks, defaults, files/templates).
2. **Wire** it into `ansible/site.yml` (right play/host; mind ordering) and, if it
   should be torn down, `ansible/teardown.yml`.
3. **Vars** in `group_vars/all.yml` (kept there, not role defaults, if teardown needs
   them too — see the existing image/port vars).
4. **Docs:** README "Services" list; an **ADR** if it's a new pattern/trade-off
   ([documentation skill](../skills/documentation/SKILL.md)).
5. **Tests:** add to the ADR-0011 regiment (render test / control-panel unit test) as
   applicable.
6. **Validate**: `lint` → `test` → `check` → deploy.

## Change an Open WebUI / front-end setting

Open WebUI config is **env-authoritative, not UI-authoritative** (ADR-0006): the role
sets `ENABLE_PERSISTENT_CONFIG=false`, so Admin-Panel changes don't survive a deploy.

1. Set the setting's **env var** in `group_vars/all.yml` (or the profile) — the
   `webui_*` knobs — **not** in the Admin Panel.
2. Note it in README "Known Shortcomings" if it's a durable trade-off.
3. **Validate**: `check` → deploy → confirm in the UI (user accounts/chats are data and
   are unaffected).

---

**See also:** [`defects.md`](defects.md) (the register these pathways consult) ·
[documentation skill](../skills/documentation/SKILL.md) (which doc a change needs) ·
[development skill](../skills/development/SKILL.md) (staging + commit ownership).
