# ADR-0017: `sparky prune` — model removal as a deploy-context fleet operation

**Date:** 2026-07-27
**Status:** Rejected — subsumed by [ADR-0018](0018-provision-select-split.md)

> **Rejected 2026-07-29.** ADR-0018 makes `deploy` **convergent** — it reconciles the model
> store to the allowlist, deleting de-allowlisted weights fleet-wide (plan-and-confirm,
> never the active model, weights-only). So model removal is *"take it out of the allowlist,
> `deploy`"* — no separate `prune` command and no per-node `sudo rm`. One mechanism handles
> add and remove; a standalone `prune` is redundant. The problem framing below (removal was a
> manual per-node sudo dance) still holds and motivated the convergent-`deploy` decision;
> the proposed *command* is what's rejected.

## Context

Model **addition** is codified and fleet-wide: `./sparky.sh download` stages weights
into the head's inbox, and `./sparky.sh deploy` runs the `model` role, which ingests
them into the canonical store (`/opt/vllm/models/<model>`, owned by the `vllm` user)
and rsync-mirrors the store to every node — all through the `deploy` automation
identity (ADR-0003). A fresh node or disaster-recovery deploy stages weights with no
manual per-node work.

Model **removal** has no such pipeline. Deleting a model means a hand-run
`sudo rm -rf /opt/vllm/models/<model>` **on every node**, because the canonical store
is `vllm`-owned and geoff's sudo is deliberately narrow (systemctl/docker/journalctl/
install — ADR-0001). This is annoying, scales badly with node count, and is dangerous
(a fat-fingered path is unrecoverable). It also blocks re-acquisition: the `model`
role skips ingest when a model already exists in canonical, so a **corrupt** copy
can't be replaced by a clean re-download until the corrupt one is manually removed —
exactly the recovery we hit this session when a deploy ingested a half-downloaded
`Qwen3-VL-235B` and the clean re-download then sat un-ingestable in the inbox.

This is the same asymmetry ADR-0013 closed for container images: a large, versioned,
all-nodes artifact whose *addition* was codified but whose *lifecycle management* was
hand-done. Weights **removal** is the remaining gap. And the fleet orchestrator
(ADR-0016) needs a programmatic teardown/cleanup primitive anyway — an autonomous
sweep that acquires and evaluates candidates must be able to reclaim their space
without a human `sudo`-ing every node.

The key enabler already exists: the `deploy` identity has `NOPASSWD` sudo and the
cross-node SSH key, and it **already writes the vllm-owned store** (the `model`
role's mirror runs as `deploy`). So removal can go through the *same* automation
context as addition — no new privilege, and the human's narrow sudo stays narrow.

## Options considered

**A. Status quo — per-node `sudo rm`.** Zero infra, but it's the annoyance itself:
one sudo hop per node, linear in node count, and a raw `rm -rf` on the weight store
with no guardrails. Rejected.

**B. Broaden geoff's `NOPASSWD` to include `rm` on `/opt/vllm/models`.** Removes the
password prompts, but by **expanding the human's passwordless-root surface** — the
exact thing ADR-0001 forbids (geoff never holds passwordless root; automation does).
A broad `rm` NOPASSWD rule is also a footgun. Rejected.

**C. `sparky prune <model>` through the deploy-context (chosen).** Removal runs as
`deploy` (which already owns the store) across all nodes as one operation, behind the
same single automation gate as `deploy`/`teardown` — one `sudo -u deploy` password
prompt for the human, or **zero** via the control panel (`User=deploy`). Symmetric
with addition, no new privilege, guardrailed.

## Decision

Add **`./sparky.sh prune <model>`** — a deploy-context fleet operation that removes a
model from the **canonical store on every node**, making model lifecycle symmetric:

- **add** = `download` → `deploy` (ingest + mirror), **remove** = `prune`.

Design:

- **Mechanism.** A `prune.yml` ansible playbook (`hosts: all`) that removes
  `{{ vllm_models_dir }}/{{ model }}` on each node, invoked exactly like the other
  lifecycle verbs — `sudo -u deploy ansible-playbook prune.yml -e model=<name>` (or
  directly as `deploy` from the control panel). It reuses the established automation
  gate, inventory, and identity; no new privileged path.
- **Canonical store only.** `prune` targets the served store. The head's inbox is
  transient staging (`deploy:cluster`, geoff-writable *without* sudo) and is left
  alone — so pruning a corrupt canonical copy lets a **clean inbox copy re-ingest on
  the next deploy**, instead of deleting it. Inbox cleanup never needed sudo and isn't
  prune's job.
- **Guardrails (load-bearing, enforced not advisory):**
  - **Refuse to prune a live model.** Read `current-topology.json`; if `<model>` is
    served by the deployed profile, abort with a message to teardown/switch first —
    never pull weights out from under a running engine.
  - **Scoped + validated name.** Only `<vllm_models_dir>/<name>` where `<name>` is an
    existing entry in the store (no arbitrary paths, no traversal).
  - **Confirm.** Interactive confirmation showing name · size · nodes, unless `--yes`;
    the control-panel action gets a confirm modal like `teardown` (ADR-0008).
- **Reports** what it freed per node (size), and is idempotent (absent → no-op).

## Consequences

- **The per-node sudo dance is gone** — one gate (or zero via the panel), all nodes
  handled, and model removal reads like model addition. The `Qwen3-VL-235B` corrupt-
  copy recovery becomes `./sparky.sh prune Qwen3-VL-235B-A22B-Instruct-NVFP4` instead
  of two hand-run `sudo rm`s.
- **ADR-0001 is preserved.** No broadening of geoff's sudo; removal reuses the
  `deploy` automation identity that already owns the store (contrast Option B).
- **Operationalizes the fleet orchestrator (ADR-0016).** Autonomous acquire/evaluate
  sweeps get the reclaim/teardown primitive they need, panel-callable (no password),
  behind the same allowlist/authorization envelope as agent deploys.
- **Closes the weights-lifecycle asymmetry** the way ADR-0013 closed the container-
  image one — same principle (a fleet artifact's management belongs in the automation
  context, codified), different verb.
- **The guardrails are the risk surface.** An rm-across-nodes is only safe to expose
  (especially to an agent) because of the live-model refusal and path-scoping; those
  must be enforced. A bug there deletes weights fleet-wide — so the check-before-delete
  and name-validation get the same care as the `run_action` profile guard in the panel.
- **Cost.** A small `prune.yml` + a `sparky prune` command + an optional panel action —
  mirroring `teardown`. Status flips to **Accepted** when `sparky prune` lands.
