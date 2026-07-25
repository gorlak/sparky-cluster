# ADR-0013: Container images as first-class sourced artifacts (`images` role)

**Date:** 2026-07-03 (accepted 2026-07-24)
**Status:** Accepted

## Context

Model weights have a codified, reproducible, idempotent, all-nodes pipeline: the
`model` role sources them (`./sparky.sh download` → inbox), moves them to the
canonical store, and mirrors them to every node — a fresh node or disaster-recovery
`./sparky.sh deploy` stages weights automatically (see ADR-0003, the `model` role,
and the README deploy sequence).

**Container images have no such pipeline — they are hand-managed.** The runbook
says *"`docker pull <tag>` on both nodes, digests must match, then `./sparky.sh deploy`"*
— a manual step, and the digest-match is an admonition, not something enforced.
The gap became concrete on 2026-07-03: the 26.06 migration needed a **derived**
image (NVIDIA's `nvcr.io/nvidia/vllm:26.06-py3` re-pinned to `fastapi<0.137` to
dodge the `prometheus_fastapi_instrumentator` serving crash — see
`docs/upgrades/container-nvidia-vllm-26.06-py3.md`'s WAR register). That image was
built **by hand on both nodes**. It works, but it is not reproducible: a new node
or a rebuild depends on someone remembering the incantation, and the two nodes'
images could silently drift.

This is an asymmetry worth closing. Container images are the same *kind* of
artifact as weights — large, versioned, immutable, and required byte-identical on
every node — yet they are the one unmanaged one. Profiles already **declare** the
image (`vllm_image:`, with the per-profile 26.04/26.06 pin) but nothing
**guarantees** it is present; the `vllm` role just assumes `docker run` will find
it.

## Options considered

**A. Status quo — manual `docker pull` / `docker build` per node.**
Zero infra, but not reproducible (DR / new-node needs manual steps), drift risk
(per-node pulls/builds aren't enforced-identical), and "digests must match" stays
aspirational. Rejected — the derived-image WAR made the cost real.

**B. Mirror images like the `model` role (rsync a directory).**
Reuse the exact weights pattern: treat Docker's image store as a canonical dir and
rsync it to all nodes. **Rejected** — Docker owns `/var/lib/docker` (content-
addressed layers, overlayfs); rsyncing it is unsafe and fights the daemon. Images
have their own distribution primitives; don't reinvent them with a file copy.

**C. A container registry on sparky (build once → push → pull).**
Nodes pull from a local `registry:2`. Scales well and guarantees identical digests
everywhere. But it is **new infra** (registry service + persistent storage), both
daemons must **trust** it (insecure-registry on the LAN, or TLS), and it becomes a
**deploy-time dependency** (registry down at pull → deploy fails). Overkill for two
nodes patching a thin layer: the 21 GiB base is already on every node, so building
the derived layer per-node costs ~1 s. **Deferred, not rejected** — a good
*backend* once node count or image weight grows.

**D. An `images` role using Docker's native ops (chosen).**
A role, run early in `site.yml` (parallel to `model`), that declaratively ensures
every required image is present on every node — `docker pull` (pinned by digest)
for upstream images, `docker build` from committed repo Dockerfiles for derived
images. Idempotent via image-present / digest checks. The registry (C) becomes an
optional distribution backend *inside* this later, not a prerequisite.

## Decision

**Option D.** Container images become first-class sourced artifacts managed by a
new **`images` role** — symmetric *in principle* to the `model` role, but using
Docker's native pull/build rather than rsync (the mechanism differs because Docker
already owns image distribution; the principle — sourced, versioned, idempotent,
present on every node, codified in-repo — is the same).

Design:

- **Declaration.** A list of required images in `group_vars` (`container_images:`),
  each entry either:
  - `{ pull: "<ref>" }` — upstream, ideally `<ref>@sha256:<digest>` (**pinned by
    digest**); or
  - `{ build: "<tag>", context: "<dir>" }` — derived, built from a committed
    `roles/images/files/<dir>/Dockerfile`.
  Per-profile `vllm_image:` continues to **select** which image a profile runs;
  the role **guarantees** the selected images (and their bases) exist.
- **Mechanism — raw `docker` CLI.** The role drives `docker image inspect` /
  `docker pull` / `docker build` via the `command` module, not `community.docker` —
  matching how the rest of the repo talks to Docker (raw `docker run` in the vllm
  units) and needing **no docker-py SDK** on the nodes.
- **Idempotence.** A `docker image inspect` guard runs first; `pull` and `build` are
  skipped entirely when the image is already local (mirrors the `model` role's "skip
  if already canonical"). A derived image rebuilds when absent, or when its entry
  sets `force: true`.
- **All-nodes.** Runs on `hosts: all` (like `common` / `model`) — each node
  pulls/builds independently; **no rsync of image layers**. For a `build`, the
  context ships **with the role** (`files/<dir>/`, published in the `ansible/` tree)
  and is staged to each node before `docker build`. A registry backend can later
  replace per-node build with build-once-push-pull, with no profile changes.
- **Ordering.** Runs in the first play (after `model`), **before** the `vllm` role's
  play in `site.yml`, so images are guaranteed present before any unit `docker run`s
  them.
- **No inbox.** The `model` role's inbox→canonical staging does **not** transfer —
  Docker's local image store *is* the canonical store; pull/build write straight to
  it.
- **Pinned by digest.** The shipped `container_images` pulls are pinned as
  `<ref>@sha256:<digest>` (the registry manifest digest is content-addressed, so the
  same tag resolves to the same digest on every node — that's what makes the two
  nodes byte-identical by construction). The derived image's Dockerfile `FROM` is
  pinned to the **same** 26.06 digest the role pulls as the base, so the build
  resolves to the already-present image with no registry round-trip and is
  reproducible. Bumps are a deliberate digest change; re-read with
  `docker images --digests`.

## Consequences

- **Reproducibility.** DR / new-node `./sparky.sh deploy` now builds+pulls the exact
  images automatically, exactly as it already stages weights. The runbook's manual
  "`docker pull` on both nodes" step goes away.
- **Derived-image WARs are codified, not hand-built.** The 26.06 `fastapi<0.137`
  patch (and any future patched image) lives as a repo Dockerfile + a declaration,
  survives a rebuild, and is auditable — no artifact that exists only because
  someone typed `docker build` once.
- **"Digests must match" becomes enforced**, not admonished — the upstream pulls are
  pinned by digest in `container_images`, so an image bump is a deliberate digest
  change and both nodes are byte-identical by construction (which is the point).
- **Registry stays optional.** Per-node build is fine at two nodes (shared base,
  thin/fast derived layers); the role is structured so a registry backend slots in
  later when scale makes build-once-distribute cheaper — see Option C.
- **Cost.** A new role plus a base-image declaration to maintain; upstream images
  are pinned by digest (deliberate bumps).
- **Relationships.** Operationalizes ADR-0004 (*which* container runtime) by
  managing *how the chosen images reach the nodes*; complements ADR-0003 (profiles
  declare `vllm_image`; this guarantees it exists). Supersedes the manual image-pull
  step in the README deploy sequence.

Implementation landed with this ADR: the `images` role (`roles/images/`), the
`container_images` declaration in `group_vars/all.yml` covering the 26.04 base, the
26.06 base, and the `dgx-spark/vllm:26.06-fastapi-fix` derived image, its Dockerfile
relocated from repo-root `docker/` to `roles/images/files/vllm-26.06-fastapi-fix/`
(so it ships with the role — repo-root `docker/` was never published to the runtime
tree), and the `images` role wired into `site.yml`'s first play before `vllm`.
