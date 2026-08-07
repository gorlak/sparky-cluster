# Update pathways

Changing what the cluster *serves* is a one-liner — `./sparky.sh activate <profile>`,
no root, nothing else to touch. This doc is for the changes that alter **what may run**:
container bumps, new models, new roles, front-end settings. Those are `deploy` territory
(ADR-0018) and several hand-maintained spots must move together. Each pathway is a
checklist of **every place to touch**, ending with the same two steps:

- **Defects.** Consult [`defects.md`](defects.md) filtered to what you changed, and
  **re-test one at a time** any row whose *Clears when* your change satisfies. This is
  how an update efficiently closes out debt instead of silently carrying it.
- **Validate.** `./sparky.sh lint` → `check` → `deploy` → `activate <profile>` behind
  the fail-safe net (ADR-0009) → the activation's `smoke` gate → (for serving/perf
  changes) `bench`.

> **Deploy does not restart a live engine.** It is selection-neutral: a change to the
> serving profile is installed and reported as *pending*, and takes effect on the next
> `activate`. So every pathway below ends with an explicit `activate` — that is the
> step that actually applies it.

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
   The image is part of each engine's `ENGINE_SPEC_HASH`, so a bump moves the hash on
   **both** ranks of a TP=2 engine and they restart as a matched pair.
5. **Upgrade tracker.** Create/update `docs/upgrades/container-<coordinate>-<tag>.md`
   ([documentation skill](../skills/documentation/SKILL.md) naming) and walk its **WAR
   register** — drop any WAR whose *Remove when* now holds.
6. **Defect register** → filter [`defects.md`](defects.md) to the container rows
   (DEF-0001–0005, and DEF-0007 on a vLLM-version bump). Re-test each cleared row **one
   at a time**, behind the fail-safe net; a **soak test** is required to clear DEF-0002
   (strikes 35–55 min in). Update statuses with results.
7. **Validate** as above. The `images` role pulls/builds on every node at deploy — no
   manual `docker pull`. Then **`./sparky.sh activate <profile>`**: the deploy installs
   the new image reference but will not restart a serving engine to pick it up.

## Add or change a model / profile

The mechanical steps are in the README ("Adding models / profiles"); the fan-out:

1. **Stage weights:** `./sparky.sh download <hf-repo>` — into the **inbox**, where they
   stay until a profile claims them. The `model` role only adopts weights the allowlist
   references, then mirrors to **each node that runs them** (per-node disk tracks what a
   node actually serves). So a candidate acquired ahead of its profile is safe: eviction
   only ever looks at the store, never the inbox.
2. **Fit check first:** the [model-evaluation skill](../skills/model-evaluation/SKILL.md)
   — memory math, `config.json`, never `--quantization` on a self-declaring checkpoint.
3. **Profile:** copy the nearest-shape `ansible/profiles/<name>.yml` and edit
   `serving_topology` + `vllm_image`. Confirm its image is in `container_images`.
4. **Docs:** a fact sheet `docs/models/<model>.md`; the profile table in
   [`profiles.md`](profiles.md) and the README profile table.
5. **Defect register** → check [`defects.md`](defects.md) for anything gating this
   model/quant/arch (e.g. DEF-0004 for AWQ/Marlin MoE, DEF-0006 for Step-3.7 VL).
6. **Validate**: `lint` picks up the new profile automatically and now validates the
   whole allowlist (fleet-wide-unique engine names, the one front port, flags that
   survive the env-file round trip); then `check` → `deploy` → `activate <name>` →
   `bench <label>`. The activation runs the smoke gate itself.

### Removing a model / profile

Deletion is the same mechanism run backwards — there is no separate `prune` command
(ADR-0018 rejected ADR-0017 for exactly this reason):

1. **Delete** `ansible/profiles/<name>.yml`. That takes it out of the allowlist.
2. **`./sparky.sh deploy`** — reports which weights are now unreferenced, per node, and
   deletes nothing.
3. **`./sparky.sh deploy --evict`** when you've read the plan — *read it*, don't skim:
   anything in the store that no profile claims is on that list. Three guards apply: it
   plans first, it converges **weights only** (images are left to Docker's GC — shared
   base layers make convergent image deletion unsafe), and it **never deletes the
   active model** — if the live profile is the one leaving, the deploy drives the fleet
   to `empty` and waits for the engine to stop first.
4. **Docs:** drop its rows from [`profiles.md`](profiles.md) and the README table.

To keep the weights but stop it being activatable — a candidate parked on an upstream
fix — set **`blocked: true`** instead of deleting. *Block to park it; delete the file
to evict it.*

## Add a role / always-on service

1. **Role** under `ansible/roles/<name>/` (tasks, defaults, files/templates).
2. **Wire** it into `ansible/site.yml` (right play/host; mind ordering) and, if it
   should be torn down, `ansible/teardown.yml`. Always-on services are **not**
   profile-conditional any more — `deploy` is whole-fleet, so there is no
   `enable_<x>` toggle to add.
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

Since ADR-0018 Open WebUI is a **vanilla, model-agnostic OpenAI client** pointed at the
fixed endpoint, so this set is small, constant, and never touched by an activation.

1. Set the setting's **env var** in `group_vars/all.yml` — the `webui_*` knobs — **not**
   in the Admin Panel.
2. Note it in README "Known Shortcomings" if it's a durable trade-off.
3. **Validate**: `check` → `deploy` → confirm in the UI (user accounts/chats are data
   and are unaffected).

Per-model Open WebUI settings are deliberately **not** supported: model config belongs
in the profile (vLLM flags), which is where the transcript evidence says every real
output-quality fix has actually lived. The escape hatch, if that ever breaks, is a
per-model connection.

## Change the /admin password

`./sparky.sh admin-password` → `./sparky.sh deploy`. The hash lives at
`/opt/cluster/admin-basic-auth.hash` (a runtime secret, never in git); a deploy refuses
to serve the panel without one, because the panel holds the activation grant.

---

**See also:** [`defects.md`](defects.md) (the register these pathways consult) ·
[documentation skill](../skills/documentation/SKILL.md) (which doc a change needs) ·
[development skill](../skills/development/SKILL.md) (staging + commit ownership).
