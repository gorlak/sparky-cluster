# Update pathways

Changing what the cluster *serves* is a one-liner — `./sparky.sh activate <profile>`,
no root, nothing else to touch. This doc is for the changes that alter **what may run**:
container bumps, new models, new roles, front-end settings. Those are `deploy` territory
(ADR-0018) and several hand-maintained spots must move together. Each pathway is a
checklist of **every place to touch**, ending with the same two steps:

- **Defects.** Consult [`defects.md`](defects.md) filtered to what you changed, and
  re-test any row whose *Clears when* your change satisfies, as [`defects.md`](defects.md)
  prescribes. This is
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

## Bump a container image

Every image the cluster runs — vLLM **and** the front-end (Open WebUI, Caddy,
Prometheus, Grafana, the exporters) — is declared in `container_images` and pinned by
**digest**. That list is the version manifest: what is deployed is exactly what is
written there, on every node.

Two rules the pinning exists to enforce, both learned the hard way:

- **A floating tag is not a version.** `alpine:latest` pulled on the two nodes days
  apart gave two *different* images. `:latest`, and even `caddy:2`, move under you.
- **Pull and run must name the same thing.** `docker pull repo@sha256:…` creates no
  local tag, so a compose file or unit naming `repo:tag` finds nothing on a fresh node
  and triggers an *unpinned runtime pull*. Both the `*_image` var and the
  `container_images` entry reference the digest; the entry references the var, so
  they cannot drift apart.

E.g. NVIDIA ships `26.07-py3`, or Open WebUI releases a new version.

1. **Get the digest** — [[version-discovery]] owns how (it needs `docker`, which is
   password-gated). What matters here is that a digest, not a tag, is what moves.
2. **`ansible/group_vars/all.yml`:** update the `*_image` var to
   `<repo>@sha256:<digest>`, with the human tag **and version** in a trailing comment —
   the digest is authoritative, the comment is what makes the diff readable. The
   `container_images` entry already references the var, so nothing else moves.
   Set `hosts: head` on an entry that only the head runs; a worker holding Grafana
   wastes disk on an image it can never run.
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
7. **Validate** as above. The `images` role pulls/builds at deploy, on the nodes each
   image is placed on — no manual `docker pull`. Then **`./sparky.sh activate <profile>`**:
   the deploy installs the new image reference but will not restart a serving engine to
   pick it up. For a *front-end* image there is no activation step — the compose file
   re-renders and the container is recreated by the deploy itself.

**Old images are reclaimed by `deploy --evict`** (since 2026-08-10). The image store
converges to `container_images` exactly as weights converge to the allowlist, plus the
dangling layers each rebuild of a derived image leaves behind. So a superseded image goes
when the declaration stops naming it — there is no delete-list to maintain.

Never run `docker image prune -a` by hand — see [[version-discovery]] for why. `--evict`
is safe where that is not, because it converges to the declaration rather than to whatever
happens to be running.

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
   Three fields are **required and enforced by `lint`** (all added 2026-08-10):
   - **the name itself** is the lowercased canonical HF model name, **copied verbatim**,
     plus at most one suffix from `topology.VARIANT_SUFFIXES` when the same weights are
     served a second way — topology (`-single`) or optimization (`-eagle`, `-mtp3`). Nothing else may be
     appended — in particular **never a quant or precision of your own**: a quant appears
     in a name only when the vendor put it in the repo name. (This line used to say "an
     optional `-flavour` suffix", which the test has always refused; that mismatch produced
     a `-fp8` profile on 2026-08-11.) `profile_name`,
     the engine `name` and `served_as` are all that same string — one name everywhere,
     so a scoreboard row, a systemd unit and a Hub page are obviously the same model.
   - **`hf_repo:`** the exact upstream `org/Name`. The org is *not* recoverable from
     the model name (`Qwen3-Coder-Next-NVFP4` is RedHatAI's, not Qwen's), and this is
     what makes a scoreboard row link to the Hub.
   - **`archetypes:`** what the profile is an example of, from `topology.ARCHETYPES`.
     Tests bind to the shape rather than to a model name, so a fleet change does not
     break tests that never cared about that model.

   You may now **omit** `nodes`, `port` and `tensor_parallel_size` — they default to
   the whole fleet (head first), 8000, and `len(nodes)`. State them only to differ, as
   the retired single-node configs do.
4. **Docs:** a fact sheet `docs/models/<model>.md`; the profile table in
   [`profiles.md`](profiles.md) and the README profile table.
5. **Defect register** → check [`defects.md`](defects.md) for anything gating this
   model/quant/arch (e.g. DEF-0004 for AWQ/Marlin MoE, DEF-0006 for Step-3.7 VL).
6. **The `all` runbook** — add the profile to `runbooks/all.yml`. It declares
   `covers: allowlist`, so `lint` **fails** until you do; that is the point, because the
   alternative is discovering the gap much later as a missing scoreboard row.
7. **Validate**: `lint` picks up the new profile automatically and now validates the
   whole allowlist — run it and read what it reports; then `check` → `deploy` → `activate <name>` →
   `bench <label>`. The activation runs the smoke gate itself.

### Removing a model / profile

Deletion is the same mechanism run backwards — there is no separate `prune` command
(ADR-0018 rejected ADR-0017 for exactly this reason):

1. **Archive, don't just delete:** `git mv ansible/profiles/<name>.yml
   docs/models/retired/` and add the retirement banner (date, one-line reason, link
   to the tombstone). The directory is invisible to both profile loaders — they glob
   `profiles/*.yml` non-recursively — and a test asserts it can never leak into the
   allowlist. It exists because deleting the `.yml` threw away the *engineering*: the
   memory math, the parser names read from chat templates, the quant findings. Recovering
   those from `git log` requires knowing they exist, so in practice nobody looks and the
   next person re-derives them — which for a parser name costs a deploy.
2. **`./sparky.sh deploy`** — reports which weights are now unreferenced, per node, and
   deletes nothing.
3. **`./sparky.sh deploy --evict`** when you've read the plan — *read it*, don't skim:
   anything in the store that no profile claims is on that list. Three guards apply: it
   plans first, it converges weights **and images** (since 2026-08-10 — the image store is
   converged to `container_images` the same way, plus dangling layers; the old claim that
   shared base layers made this unsafe was wrong, since Docker refcounts layers and
   `docker rmi` refuses an image a container holds), and it **never deletes the
   active model** — if the live profile is the one leaving, the deploy drives the fleet
   to `empty` and waits for the engine to stop first.
4. **Tombstone:** add a row to [`models/tombstones.md`](models/tombstones.md) with a
   falsifiable *reconsider-when*. Required whenever the weights are **evicted**, even if
   the model is not rejected — once they are gone a discovery sweep will propose
   re-downloading, which is the exact waste that register exists to prevent. Mark those
   "evicted, not condemned" so the verdict is not overstated.
5. **Docs:** drop its rows from [`profiles.md`](profiles.md) and the README table.

To keep the weights but stop it being activatable — a candidate parked on an upstream
fix — set **`blocked: true`** instead of deleting. See the README's allowlist section for the two gestures and what each costs.

## Add or change a runbook

A runbook is a named, reviewable procedure (ADR-0020) — `runbooks/<name>.yml`, started by
`./sparky.sh run <name>` or from the panel. The repo is where it is **authored**; a deploy
is what makes it **startable** (ADR-0021), because both callers name a member of the same
installed set and a network-facing one must not be able to run whatever is in a checkout.

1. **Write `runbooks/<name>.yml`.** Jobs are `{profile, regiments}`; `defaults.regiments`
   applies to any job that does not name its own. Steps invoke sparky commands, and only
   those in the **Operate** scope — `deploy` and `admin-password` are excluded by
   construction, and args are argv, never a shell string. [[development]] has the scope
   table; `sparky/runbook.py` enforces both.
2. **Put the decision rule in the file, before the numbers exist.** A runbook that
   commandeers the cluster for hours should say what its outcomes mean, so the result is
   read against a rule rather than rationalised after the fact. A *standing* campaign —
   one whose job list should track the allowlist rather than answer a question — declares
   **`covers: allowlist`** instead, and `lint` then fails whenever the two disagree.
   `runbooks/all.yml` is the one that does.
3. **`./sparky.sh lint`** validates the repo's runbooks alongside profiles — a step naming
   a privileged command fails here rather than two hours into a campaign.
4. **Try it in the foreground first**: `./sparky.sh sweep runbooks/<name>.yml --dry-run`
   prints the plan and runs nothing; without `--dry-run` it runs the job list from its
   path, which is the iteration loop before the runbook is installed.
5. **`./sparky.sh deploy`** publishes it to `/opt/cluster/runbooks/`. Until then
   `./sparky.sh run` lists it as *not deployed* and refuses to start it.
6. **`./sparky.sh run <name>`** starts it detached and logged; `run --stop` ends it,
   `run <name> --follow` tails it. See [[operations]].

Removing one is `git rm` plus a deploy — the publish is `--delete`, so the installed set
follows the repo.

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
