---
name: version-discovery
description: Check everything versioned that this cluster runs for a newer release, and STAGE the bumps without applying them. Use when asked "what needs updating", before an upgrade round, when a defect's clears-when depends on a version moving, or when adding a node. Covers container images, the control panel's Python deps, vendored assets, the harness lock, model snapshots, and the host baseline.
---

## What this is for

`model-discovery` asks *"what new models exist that would fit?"*. This asks the other
question: **"what newer versions exist of the things we already run?"** — and stages the
answer as a reviewable diff.

It **stages, it does not apply.** An upgrade is a separate, deliberate act with its own
validation (`docs/updating.md`) and its own defect re-tests. Conflating "find out what's
available" with "change the cluster" is how you end up unable to tell whether the new
model or the new container broke something.

## The one rule: check the DEPLOYED version, not the declared one

They differ, and the gap is the interesting part. Before the 2026-08-08 pinning,
`group_vars` declared `ghcr.io/open-webui/open-webui:latest` while the node had been
sitting on **0.10.2** for months — a floating tag plus a compose file that never
re-pulls means *frozen at an unknown version*. The repo said one thing; reality was
another; nobody could have told you which.

So every check below has two halves: **what the repo declares**, and **what the node is
actually running**. A sweep that only reads the repo is checking its own homework.

## The inventory, by who owns the pin

### 1. Container images — `container_images` in `ansible/group_vars/all.yml`

The version manifest. Every image is pinned by digest with the tag + version in a
trailing comment, and referenced by digest everywhere it is pulled *and* run.

Deployed reality, per node (needs Geoff — `docker` is password-gated by ADR-0018):

```bash
sudo docker images --no-trunc --format '{{.Repository}}:{{.Tag}} {{.Digest}}' | sort
```

Live versions, without docker, straight from each service:

```bash
curl -s 127.0.0.1:8080/api/version; curl -s 127.0.0.1:3000/api/health; curl -s 127.0.0.1:9090/api/v1/status/buildinfo
```

Upstream: the registry's tag list (NGC for `nvcr.io/nvidia/vllm`, GHCR for Open WebUI,
Docker Hub for the rest). **Compare digests, not tags** — a tag can be repushed.

Staging a bump: change the `*_image` var to the new `<repo>@sha256:<digest>`, update
its trailing comment, set `hosts: head` if only the head runs it. `container_images`
references the var, so nothing else moves. Pathway: `docs/updating.md`.

> **Never `docker image prune -a`.** Docker calls an image "reclaimable" when no
> *running* container references it — not when it is unneeded. On a two-node cluster
> serving one profile, that includes the pinned images of every other profile.

### 2. The control panel's Python deps — `roles/control-panel/files/app/requirements.txt`

⚠️ **Currently unpinned** (`fastapi`, `uvicorn[standard]`, `jinja2`, `httpx`,
`python-multipart`, no constraints). The venv happens to hold `fastapi==0.136.3`
because pip does not upgrade an already-satisfied unconstrained requirement — the same
accidental freeze as the old `:latest` images. Delete the venv and redeploy and you get
whatever is current.

That is not theoretical: **fastapi 0.137 is exactly what broke vLLM's metrics
instrumentation** (DEF-0005, which we still carry a derived image to work around). The
panel is one patch release away from the same library that has already bitten this
cluster once.

```bash
/opt/cluster/control-panel/venv/bin/pip list --outdated
```

### 3. Vendored assets — `roles/control-panel/tasks/main.yml`

`htmx` is fetched at deploy time and **is** pinned (`htmx.org@2.0.3`). Check its
releases; bump the URL. Anything else vendored by URL belongs in this list.

### 4. The harness — `pyproject.toml` + `uv.lock`

`pyproject.toml` declares floors (`>=`); **`uv.lock` is the pin** — 30 packages, exact
versions. That one is genuinely reproducible.

```bash
uv lock --upgrade --dry-run
```

Bumping means re-locking and running `./sparky.sh test`. The lock is committed, so the
diff is the review.

### 5. Models — the store and the inbox

Distinct from `model-discovery`: this is **newer snapshots of models we already hold**,
which is easy to miss because the name barely changes. Real example — we staged
`DeepSeek-V4-Flash` (preview, 284B) and upstream later shipped
`DeepSeek-V4-Flash-0731` (304B, *"superseding the preview version, with substantially
enhanced agentic capabilities"*). Same model, different artefact.

```bash
./sparky.sh fleet
```

Check each model's HF repo for a newer revision. A newer snapshot is a **staging**
decision (`./sparky.sh download`), then a profile edit — see `model-evaluation` for the
fit check before committing to it.

### 6. The host baseline — not repo-managed

Ubuntu, NVIDIA driver, CUDA, `ansible-core` (apt, control node only). Record what they
are; do not bump them as part of a sweep. A driver change is a DGX Spark platform event
with its own blast radius, and several defect rows are conditioned on the driver.

```bash
. /etc/os-release && echo "$PRETTY_NAME"; nvidia-smi --query-gpu=driver_version --format=csv,noheader; ansible --version | head -1
```

## Finish the same way every update pathway does

1. **Re-read [`docs/defects.md`](../../docs/defects.md)** filtered to whatever moved. A
   bump is the *only* thing that clears most rows — each carries a **clears-when**
   naming the version it waits on. Re-test cleared rows **one at a time**; pulling
   several workarounds at once hides which was still load-bearing.
2. **Walk [`docs/updating.md`](../../docs/updating.md)** for the fan-out of whatever you
   staged — it lists every place a change must touch.
3. **Report as a table**: artefact · declared · deployed · available · what a bump would
   unblock. The last column is what makes it a decision rather than a chore.

## Output

A staged diff plus that table. Stop there — Geoff runs the commits
([`development`](../development/SKILL.md)), and applying the upgrade is a separate
change so that a regression has one candidate cause instead of two.
