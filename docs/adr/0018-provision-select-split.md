# ADR-0018: Fleet serving as `deploy` + `activate` — convergent provisioning, a selection reconciler, and no web-API root

**Date:** 2026-07-29 (revised 2026-08-03, while Proposed: the selection mechanism was
simplified — the resident selector daemon + custom authenticated RPC became a **oneshot
reconciler with bounded triggers** (single-command sudoers + forced-command SSH), the
per-variant rendered units became a **systemd template unit + per-engine env files**, and
the persisted Caddy rewrite became **static health-checked upstreams**. Same boundary and
invariants; fewer invented parts.)
**Status:** Proposed

## Context

Designing agent-driven evaluation (ADR-0016) exposed a smell we'd carried from the start:
**a web API — the control panel, no auth — can invoke `NOPASSWD: ALL` sudo.** It falls out
of routing *everything* through one `ansible-as-deploy` path. Ansible legitimately needs
root to *provision* (apt, systemd units, `/opt/vllm` weights, docker), so `deploy` got
blanket NOPASSWD (ADR-0001/0002), and the whole automation surface — the panel included —
inherited root. To let an agent drive it we then had to argue "contain-and-audit," because
a compromised agent on that path *is* root.

The real fix is to stop conflating two operations with very different privilege:

- **`deploy` — the whole fleet, no profile argument.** Converge the model store, images,
  and unit set to the **allowlist** (the published fleet definition). This *sets the
  boundary of what may run.* It genuinely needs root, and it is **selection-neutral**: it
  does not change what's serving.
- **`activate <model>` — make one already-deployed model the live one.** This is the only
  operation that changes what's serving, chosen from the deployed set. It needs **no root**.

("provision" and "select" were the working names; `deploy`/`activate` are the verbs. The
familiar `deploy` survives — it now means *deploy the fleet*, not *deploy one profile*.)

The security goal: **no web-API path to root.** And a transcript scan (2026-07-29) settled a
load-bearing question — across six sessions, every "garbage output" fix was **serving-side**
(vLLM flags in profiles), and every Open WebUI env var was **auth/infrastructure only**,
never model behavior. So Open WebUI can be a **vanilla, model-agnostic** client; model config
belongs in profiles, exactly where the split puts it.

## Options considered

**A. Status quo — one `ansible-as-deploy` path, `NOPASSWD: ALL`, the panel triggers it.**
A web API that can run anything as root, with ansible used as the *activate* engine when it's
really a *provision* engine. Rejected.

**B. Scope `deploy`'s NOPASSWD to specific commands** (narrow sudoers over `systemctl …`).
Shrinks the surface, but a *family* of general-purpose systemctl invocations is still a
shell-shaped hole reachable from a web API, and ansible still does activation. A patch, not
the boundary. Rejected — with a distinction that survives into option C: the problem is
passwordless access to *general-purpose commands*, not passwordless triggering per se. One
fixed program that validates its input against the allowlist is a different animal, and C
uses exactly that as its local trigger.

**C. Split `deploy` (convergent provision) from `activate` (a fixed oneshot reconciler with
bounded triggers), and take the web API off the privileged identity (chosen).** `deploy` =
human-initiated, password-gated ansible. `activate` = write desired state → one small, fixed
reconciler script, invoked through two decades-old bounded channels (a single-command sudoers
entry locally; forced-command SSH cross-node). The panel and other services leave the
`deploy` identity, so `NOPASSWD: ALL` is reachable only through geoff's password gate. (An
earlier draft of this option specified a resident per-node selector *daemon* exposing a custom
authenticated RPC; it was simplified away — a daemon and a protocol reimplement what sudo and
sshd already do natively, and the security property lives in the *fixedness* of the
reconciler, not in how the request is transported.)

## Decision

**Option C.** Two operations, two privilege levels, and no web-API root.

### `deploy` — converge the fleet (privileged, human, password-gated, occasional)
`ansible-playbook` run by geoff with his password. It makes reality match the **allowlist**
(the fleet definition):
- installs **one systemd template unit** (`vllm@.service`, instance `%i` = engine) per node —
  the unit *logic* (head/worker rank, the two `ConditionPathExists` gates, the marker
  lifecycle) lives in exactly one rarely-changing file — plus **one rendered env file per
  allowlisted `(profile, variant)` engine** (`/opt/vllm/engines/<engine>.env`: image, flags,
  rank, ports). The env files are the whole per-variant surface — flat, diffable, easy to
  plan-and-confirm — and `activate` is pure activation (`systemctl start vllm@<engine>`),
  never rendering;
- builds/pulls images (ADR-0013) and stages weights (ADR-0003) **only on the nodes each
  profile runs on** — a single-node/snoopy model isn't mirrored to the head (beyond the
  head's canonical download copy); a TP=2 model goes to both. Per-node disk tracks what each
  node actually runs, not the whole fleet;
- **convergent — including removal.** A model no longer in the allowlist has its **weights
  deleted** on the nodes that held it (the store is made to match the allowlist; no litter).
  Three guards make that safe: it **plans and confirms** deletions (`check`/dry-run surfaces
  them Terraform-style — no *silent* loss); it **never deletes the active model** — if the
  live model is being removed, `deploy` first drives `activate empty`, **waits for the engine
  to stop**, then deletes; and it converges **weights only** (images are left to Docker's GC —
  shared base layers make convergent image-deletion unsafe);
- **selection-neutral:** it preserves the currently-active model if its units still exist,
  otherwise falls to `empty` (safe default; never auto-promotes another model). `deploy`
  takes **no profile argument**.

Model-specific config (chat template, tool-call/reasoning parsers, quant handling, sampling)
lives in the profile — installed by `deploy`, activated by `activate`.

**The allowlist is the profiles directory** — implicit, not a separate manifest: a
deployable/keepable model *is* an `ansible/profiles/*.yml`. Two gestures follow: a profile
marked **`blocked: true`** keeps its weights but can't be activated (parked — e.g. waiting on
an upstream fix); **deleting the `.yml`** removes it from the allowlist, so the next `deploy`
evicts its weights (behind plan-and-confirm). *Block to park it; delete the file to evict it.*
The `.yml` files are the policy — no separate list to drift.

### `activate <model>` — choose the live model (unprivileged, human *or* agent)
The **request** is a **desired profile** written to a group-writable input on the head
(`/opt/cluster/desired-profile`) — **no sudo**. The **reconciler** is a small, fixed **oneshot
script** (`vllm-activate`, root-owned, installed by `deploy` on each node) — **not a daemon**:
nothing runs until an activation is requested, so there is no resident process to keep alive,
monitor, or trust while idle. It does exactly one narrow thing: activate a deployed,
allowlisted profile, **re-validating the request against the installed env files** before it
acts.

Two **bounded triggers** invoke it — both standard idioms; no new protocol, no token store:

- **Local (head):** a **single-command sudoers entry** —
  `NOPASSWD: /usr/local/sbin/vllm-activate` — granted to the low-privilege activation
  identity (the panel service / the agent). Unlike option B's scoped-`systemctl` sudo, the
  only invocable thing is one fixed, input-validating program.
- **Cross-node:** **forced-command SSH** — an `authorized_keys` entry on each worker
  (`command="/usr/local/sbin/vllm-activate"`, `no-pty`, `no-port-forwarding`, …) for a
  dedicated activation key. The key *is* the credential and the forced command *is* the
  bound; the requested profile arrives via `SSH_ORIGINAL_COMMAND` and is **re-validated on
  the worker**. Exit code + stdout roll errors straight back to the originator —
  synchronous, no polling — and the transport and its audit trail (sshd, `auth.log`)
  already exist.

`activate` **orchestrates synchronously**: the head reconciler reads the request, reconciles
its own node, invokes each involved worker's reconciler over the forced-command channel, and
collects per-node results; any node's failure drives the fleet to `empty` (the fail-safe
invariant below) and reports the error to the originator.

**Request vs. gate.** The head-side `desired-profile` is what's *asked*; the **per-node
`/opt/vllm/active/<engine>` markers** are what *gate boot* — each node's systemd reads its own
(below), written by that node's reconciler during its run, **never shared over a filesystem**.
So there's no head-only path a worker can't see.

Each node's reconcile is **transactional and marker-first**: it writes that node's local
active-markers as an all-or-nothing set, then drives `systemctl` to match (start the target's
units, stop the rest — Caddy needs nothing; see below). **The markers are the source of truth**
— on any mid-reconcile failure the reconciler re-drives `systemctl` back to the markers (or
rolls the markers back and reports the error), so live-state is never left ≠ markers; a
reconciler killed mid-run is recovered the same way by the next run.

### Fail-safe & boot recovery — systemd stays the boot authority (extends ADR-0009)
Boot must never depend on the reconciler: recovery is the safety-critical path, and custom
code can't be a single point of failure for it. So units stay **`enabled`**, gated by *two*
`ConditionPathExists` checks — one new, one straight from ADR-0009:

```ini
# vllm@.service  (one template per node; %i = engine)
ConditionPathExists=/opt/vllm/active/%i                # desired — PER-NODE, reconciler-written
ConditionPathExists=!/opt/vllm/state/vllm-%i.running   # ADR-0009 unclean-shutdown marker
EnvironmentFile=/opt/vllm/engines/%i.env               # rendered at deploy — the variant surface
```

A unit boots **iff desired AND cleanly-stopped-last-time.** On a clean reboot, systemd attempts
all enabled units and `ConditionPathExists` skips everything except the last-desired profile →
it auto-restores **with no reconciler involvement**. On a hang/hard-reset, the surviving
`.running` marker skips the unit → **empty and reachable** (ADR-0009, unchanged). This
**extends** ADR-0009 (adds the desired gate) rather than replacing it. The reconciler is in the
*live-swap* path only — and as a oneshot it has no "down" state to reason about: a missing or
broken script means you can't *change* the model, while boot, recovery, and the current model
all keep working.

**Caddy needs no writes at all.** Every profile's front engine listens on the same well-known
port on whichever node serves it, and the stable endpoint is a **static health-checked
upstream list** over all nodes (`lb_policy first` + active health checks). Exactly one
instance per port is ever alive (the reconciler stops-then-starts), so routing follows
activation with **no config rewrite, no reload, no persisted state** — and on boot Caddy
comes up static and converges the moment the restored engine passes health. The discipline
this buys: **at most one live engine per port, fleet-wide**; if simultaneously-live engines
are ever wanted, each engine class gets its own port/hostname route — a decision to write
down before that day, not machinery to build now. (Replicas of one model, if ever revisited,
are the same mechanism — two healthy upstreams, Caddy balances — the round-robin the retired
`-dual` shape lacked.)

**Restored ≠ promoted.** A reboot *mid-sweep* restores the *last-`activate`d* profile — during a
sweep that's a transient candidate, not the promoted serving model. So the sweep records a
**promoted model** (distinct from "last activated") and re-`activate`s it on completion/abort; a
mid-sweep reboot recovers to whatever was last activated, fixed by re-kicking the sweep or
re-`activate`ing the promoted model. A rough edge, not a hazard — the node is up and reachable
throughout.

**Invariants (the safety contract):**
- **Fail to `empty` on any uncertainty** — a reconciler that can't read/parse desired state or
  hits any error activates *nothing*; it never guesses a model onto the GPU.
- **Reachability is never gated on a model** — the reconciler touches only vLLM units; a broken
  reconciler or a failed `activate` leaves the node up and SSH-reachable.
- **Peer-join timeout, not hang** — a TP=2 activation whose peer never joins falls to `empty`
  within a bounded time (ADR-0009's `TimeoutStartSec` / `Restart=on-failure`), never waits forever.
- **Boot is systemd, never the reconciler** — the reconciler needn't even exist at boot; the
  markers carry the decision.

### The serving surface is model-agnostic and stable
Caddy (ADR-0007) fronts a **fixed model endpoint** over the static health-checked upstream
list (above) — a TP=2 engine answers from the head, a single-node engine from snoopy, and
Caddy finds whichever is alive with no activation-time involvement. Open WebUI and Prometheus
point at that fixed address, configured once at `deploy` time.

**Open WebUI is a vanilla, model-agnostic OpenAI client.** Units advertise **both** a stable
`served-model-name` (e.g. `sparky`) and their real name (vLLM takes multiple), so bench/eval
target the real name while chat targets the stable one; `DEFAULT_MODELS=sparky` + a model
filter tame picker duplication. Conversations become portable across activations. Per-model
Open WebUI settings are **deferred** with a per-model connection as the escape hatch — a bet
the transcript evidence shows is safe.

**During an eval sweep the human-facing serving is suspended.** A sweep commandeers the
cluster, so Open WebUI / the stable endpoint are **taken down** (or flipped to an "eval in
progress" page) for the duration — evals run under their real names with no collision, and the
promoted model + chat are restored on completion. (See ADR-0016 for sweep behavior.)

### The boundary is real
The agent gets `activate` — and nothing privileged. `deploy` is human, password-gated. The
allowlist + installed env files **are the policy**, and the agent can't rewrite them because it
can't `deploy`. Source-side guardrails become *real*, not theater — the "an agent that
controls the source defeats any check" problem was downstream of conflating deploy with
activate. A one-line statement of the whole model: **the agent gets `activate`; humans get
`deploy`.**

## Consequences — including where this changes prior ADRs

- **No web-API path to *arbitrary* root.** The panel and always-on services leave the `deploy`
  identity; `deploy`'s NOPASSWD is reachable only through geoff's password gate (pragmatic
  option (ii) — keep broad sudo for human provisioning, remove the passwordless *web* surface).
  Activation is still a privileged action a web surface can trigger, but it's **on rails** — the
  reconciler only ever activates a deployed, allowlisted profile, never runs arbitrary commands.
  The real control is then **who may write `desired-profile` and hold the two trigger grants**
  (the sudoers line, the forced-command key); that ACL — installed at `deploy` time — is the
  activation boundary.
- **A real boundary, not containment.** This **supersedes ADR-0016's deploy-primitive and
  authorization sections**: the agent-deploy primitive becomes an agent-`activate` primitive
  (write desired state → the reconciler). ADR-0016's loop, sweep representation, and eval concepts
  stand — a sweep is now: human `deploy`s the variant set, then the agent `activate`s across it.
- **Tightens ADR-0001 (identity).** No service runs as passwordless-root; the automation
  identity's NOPASSWD is only enterable via geoff's password. The activation grants belong to
  a **dedicated low-privilege activation identity** (the panel service / the agent) — not
  geoff — so sudo/sshd logs distinguish human, automation, and agent actions for free.
- **Turns on ADR-0008's auth seam.** A human-only panel behind the firewall could defer auth;
  a panel that is deliberately agent-drivable and holds the activation grant cannot — the
  Caddy `basic_auth` seam (built for exactly this moment) is enabled as part of this work.
- **Refocuses ADR-0002 (ansible).** Ansible is the **`deploy`** engine only — human-initiated,
  password-gated — not the from-a-web-API activation engine.
- **Simplifies ADR-0006 (Open WebUI env-authoritative).** Open WebUI config becomes a
  `deploy`-time constant (vanilla, model-agnostic); `activate` never touches it.
- **Shrinks ADR-0008 (control panel).** The panel drops its `User=deploy` deploy action; it
  writes `desired-profile` (`activate`) and reads status, running **low-privilege**.
- **Rejects ADR-0017 (`sparky prune`).** Convergent `deploy` subsumes model removal —
  *"take it out of the allowlist, `deploy`"* — so there is no separate `prune` command or
  per-node `sudo rm`. One mechanism handles add and remove.
- **Cost.** `deploy` is heavier (renders all variants' env files, stages weights, builds
  images) and **loses automation** — no panel-triggered infra deploys, by design. Per-node disk
  tracks what each node runs (the allowlist sizes the fleet, not every node). A new per-node
  reconciler script to build and trust — on the order of a hundred lines of oneshot code, no
  daemon lifecycle, no protocol, no token store; every other moving part (systemd, sudo, sshd,
  Caddy health checks) is stock and already audited. The forced-command fan-out is per-worker
  and scales to the whole Peanuts roster — SSH fan-out is how ansible manages fleets orders of
  magnitude larger.

**Remaining build details** (design decided; resolve in code): the reconciler script itself
(marker transaction → `systemctl` drive → per-node result reporting); the `deploy`-installed
grants (the sudoers line, the forced-command key, the `desired-profile` write ACL); and the
`deploy` ↔ in-flight-sweep mutex — a `flock` on a well-known file that both `sparky deploy`
and the sweep runner take, nothing more. (Two build items from the earlier draft are
**eliminated by design**: the Caddy upstream-rewrite/reload path — replaced by static
health-checked upstreams — and the authenticated RPC endpoint + token — replaced by sshd
forced commands.)

## Test plan (following ADR-0011's layered regiment)

The boot/recovery path is the safety-critical part — and it's testable **without hard-resetting
nodes**.

- **Layer 2/3 (no hardware, in the regiment):**
  - reconciler decision logic — `(requested profile × installed env files × markers) → the
    marker set + unit start/stop plan`, including reject-unknown-profile and fail-to-`empty`;
  - convergent-`deploy` plan — what weights are deleted, and the **active-model-protection**
    guard (refuses to delete the live model without a prior `activate empty`);
  - allowlist logic — `blocked` = keep, missing `.yml` = evict;
  - **render tests** (ADR-0011 Layer 2): the template unit carries *both*
    `ConditionPathExists` gates and the `EnvironmentFile`; a profile's engines render to the
    expected env files (rank, image, flags, ports).
- **Integration (real, deliberate):**
  - clean reboot → the last-active profile auto-restores;
  - **planted-marker + reboot → comes up `empty`** — leave a `.running` marker (simulates an
    unclean shutdown, *no hard-reset needed*) and confirm the node boots empty + reachable;
  - `activate` round-trip (head + worker) with the health-checked upstream following — and no
    Caddy reload observed;
  - reconciler script removed/broken → boot still restores via markers; a live `activate`
    fails cleanly with the error reported at the originator.
- **Fault injection:** kill the reconciler mid-`activate` (the next run re-drives to the
  markers); the worker's forced-command invocation times out; corrupt/absent desired state →
  fail-to-`empty`; a TP=2 peer that never joins → `empty`, not hang.

Status flips to **Accepted** when `deploy`/`activate` are implemented — the per-node
reconciler with its two bounded triggers, a working no-sudo `activate`, the panel off the
`deploy` identity (with `basic_auth` on), convergent `deploy`, and Open WebUI vanilla.
