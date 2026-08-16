<!-- One wordmark, one home. It must live under ansible/ because that is what gets
     published to /opt/cluster and served by the panel and the landing page; a copy in
     docs/ purely for this line would be a second file to keep in step. -->
<img src="ansible/roles/control-panel/files/app/static/sparky.png" alt="Sparky" width="202">

# Sparky Cluster — vLLM + Open WebUI

A two-node NVIDIA DGX Spark (GB10) cluster serving LLMs with vLLM + Open WebUI,
managed by Ansible and driven by a single Python entrypoint, **`sparky`**. One node
is the head (`sparky`), the other a worker (`snoopy`); declarative *profiles* decide
what serves where.

## Node naming — a Peanuts theme

**Sparky** was the lifelong nickname of
[Charles M. Schulz](https://en.wikipedia.org/wiki/Charles_M._Schulz), creator of
*Peanuts* — so the head node bears his name (and, fittingly, `sparky` is always the
"head" 🥁), and the project takes its name from it. Every **worker** node is named
after a *Peanuts* character, starting with `snoopy`. If the cluster scales past two
nodes, take the next worker name from this fixed roster, in order:

`snoopy`, `woodstock`, `charlie`, `linus`, `lucy`, `schroeder`, `sally`

**Invariant:** `sparky` is always the head; workers are always *Peanuts* characters.

---

## Driving it — `sparky`

`sparky` is the **single operator entrypoint** (ADR-0015). It's the outer layer;
Ansible is the config/execution engine it drives, and the harness that talks to
running models. Run it from the repo with the root wrapper `./sparky.sh` (a thin
`uv run` shim — no install, works from any cwd).

**Two operations, two privilege levels** (ADR-0018). This is the shape of the whole
system, so it's worth reading before anything else:

| | `deploy` | `activate` |
|---|---|---|
| What it does | converge the **whole fleet** to the allowlist | make one **already-deployed** model the live one |
| Changes what's serving? | **no** — selection-neutral | **yes** — it's the only thing that does |
| Privilege | root, via `deploy`'s NOPASSWD sudo | **none** |
| Who runs it | geoff, password-gated | geoff *or* an agent *or* the web panel |
| How often | occasionally — when the model set or its flags change | routinely |

*The agent gets `activate`; humans get `deploy`.*

**`sparky` commands carry a SCOPE**, shown as a group in `--help` and enforced by a test
(`tests/test_cli_surface.py`) so a new command cannot join without declaring one. The
scope answers the question you have before you type: *will this ask for my password, and
can the agent run it?*

| scope | what it means | commands |
|---|---|---|
| **Provision** | password-gated (`sudo -u deploy`), control node only | `deploy` `admin-password` |
| **Operate** | no privilege, agent-drivable, needs a live cluster | `activate` `status` `fleet` `logs` `smoke` `bench` `eval` `run` `suite` `scoreboard` `report` `topology` `teardown` `probe` |
| **Develop** | repo only, no cluster, no privilege | `lint` `test` `download` |

That split is ADR-0018's subject, so the *provision* group is deliberately tiny — two
commands — and a test fails if it grows past three.

`./sparky.sh --help` lists every command under its scope. The two that matter most:

```bash
./sparky.sh deploy       # converge the fleet to the allowlist — password-gated
```

```bash
./sparky.sh activate <profile>   # choose what serves — no password
```

**`deploy`** first **publishes** the repo to the deploy-owned runtime tree
(`/opt/cluster`), then runs `ansible-playbook` there as the `deploy` user — its
`NOPASSWD` sudo is the automation gate (`sudo -u deploy` prompts for your password;
that's the gate into the automation context). It takes **no profile argument**: it
installs every allowlisted profile's weights, images, engine env files and the
activation grants. It preserves whatever is currently serving (falling to `empty`
only if that profile left the allowlist) and **never auto-promotes** a model.

**`activate`** writes a desired profile to a group-writable file — *no sudo at all* —
then triggers a small, fixed, root-owned reconciler (`/usr/local/sbin/vllm-activate`)
through a single-command sudoers entry. The reconciler re-validates the request
against the installed engine files **on every node**, writes each node's desired
markers as a transaction, and drives systemd to match; workers are reached over
forced-command SSH. Any node's failure drives the fleet to `empty` rather than
guessing. See [ADR-0018](docs/adr/0018-provision-select-split.md).

**Live state:**
- `http://sparky.flummoxed.net/admin` — control panel (status + activate); basic_auth
- `./sparky.sh status` — live health on both nodes; `./sparky.sh fleet` — the allowlist
- `/opt/cluster/current-topology.json` — what **is** running (reconciler-written)
- `/opt/cluster/fleet.json` — what **may** run (deploy-written)

> The `sparky` command is a Python package, not just a CLI — the same functions are
> importable (`from sparky.foundation import topology`, `from sparky.measure import bench,
> report`, `from sparky.serve import ansible`), so the cluster can be driven from a script
> or a notebook too. The package is a four-tier stack — `foundation · verify · serve ·
> measure`, imports pointing downward and enforced (ADR-0027). See "The harness" below.

---

## Current state — profiles (the allowlist)

Profiles live at `ansible/profiles/<name>.yml`; each captures the full
`serving_topology` (engines, models, nodes, ports, `gmu`, `max_model_len`). A profile's name is
the upstream repo's model name **lowercased and copied verbatim** — the quant appears
because the vendor put it there, never because we appended one; the only suffixes we
invent are topology suffixes (`-single` = the single-node TP=1 shape). Bare big-shared
profiles are TP=2 across both nodes. See `docs/profiles.md` and
`topology.name_matches_repo`, which enforces it.
Single-node serving runs on **snoopy by design** — sparky is the head (frontends) + dev
node, so single-node models serve on the resource-richer worker.

**The profiles directory *is* the allowlist** (ADR-0018) — there is no separate
manifest to drift. A profile file means "keep these weights and install these
engines"; `activate` then picks one to serve. Two gestures follow:

- **`blocked: true`** — parked. Weights and engine files are kept (so re-testing costs
  no download), but it cannot be activated.
- **delete the `.yml`** — it leaves the allowlist, so the next `deploy` **evicts its
  weights** from the nodes that held them (reported first; `--evict` to apply).

*Block to park it; delete the file to evict it.*

**Big-shared (TP=2 across both nodes)** — the shape, not *a* shape. TP=2 beats TP=1 on
decode, throughput and KV capacity for every model measured; the paired numbers are in
[`docs/profile-tuning.md`](docs/profile-tuning.md).

| Profile | Shape | decode | usable ctx / KV |
|---|---|---|---|
| `qwen3-vl-235b-a22b-instruct-nvfp4` | Qwen3-VL-235B — **75.0%** MMLU-Pro subset, vision + tools (`hermes`) | 23.8 tok/s | 131k / 534k |
| `nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4` | Nemotron-3-Puzzle-75B — hybrid Mamba; **the long-context model** | 32.0 tok/s | 131k / **28.2M** |
| `nvidia-nemotron-3-super-120b-a12b-nvfp4` | Nemotron-3-Super-120B-A12B — Puzzle's uncompressed upstream; `MIXED_PRECISION` despite the name | *decode unmeasured* | 262k / 23.5M |
| `qwen3.6-35b-a3b-nvfp4` | Qwen3.6-35B-A3B — the fast generalist, **and vision-capable** (passes the vision gate) | **100.2 tok/s** | 262k / 16.3M |
| `qwen3-coder-next-nvfp4` | Qwen3-Coder-Next | 54.0 tok/s | 262k / 5.98M |
| `minimax-m2.7-nvfp4` | MiniMax-M2.7 — soaked 64 min clean; reasons past the eval's cap on 32% of items | 24.9 tok/s | 131k / 449k |
| `mistral-small-4-119b-2603-nvfp4` | Mistral-Small-4-119B — **119B total, ~6.6B active** (128 experts, 4+1); MLA, vision. The European option | 49.0 tok/s | 262k / 2.35M |

**Every profile is offering far less context than it holds.** The `usable ctx` column is
`max_model_len` — a number we chose — against the KV cache actually allocated. Nemotron
serves 131k out of 28.2M. Raising these is config, not hardware.

**Single-node (TP=1 on snoopy)** — one left, and it is parked. The performance case for this
shape is gone; the only remaining argument is fleet occupancy, since TP=2 takes both nodes
and leaves ~24 GiB of dev headroom on sparky rather than the whole box.

None are live — no current model makes the case for leaving a node free. Retired configs
are kept in [`docs/models/retired/`](docs/models/retired/), and the verdicts in
[`docs/models/tombstones.md`](docs/models/tombstones.md).

| Profile | Shape |
|---|---|
| `empty` | nothing serving; full hardware available. Also the **fail-safe target** — always activatable |

Every engine serves on **port 8000**, and at most one is live fleet-wide at a time.
That is not incidental: it is what lets the stable model endpoint be a *static*
health-checked upstream list that follows activation with no config rewrite. `deploy`
asserts it.

See [`docs/profiles.md`](docs/profiles.md) for what each serves and how to switch;
[`docs/profile-tuning.md`](docs/profile-tuning.md) for the *why* — picking
`gpu_memory_utilization` as a deliberate split between vLLM and system/dev memory,
plus the GB10 unified-memory accounting quirk.

**Rejected models** live in [`docs/models/tombstones.md`](docs/models/tombstones.md) —
the register that *owns* those verdicts, so a discovery sweep never re-litigates one. Read
it before proposing a model.

### Services

**Always running** (independent of what's activated): `caddy` (reverse proxy on
`:80`), `open-webui` (chat UI, fronted at `chat.{web_domain}`),
`control-panel.service` (FastAPI status + activate, running as the low-privilege
`activator` identity, at `/admin` behind basic_auth), `prometheus`, `grafana` (at
`metrics.{web_domain}`), and `node-exporter` + `nvidia-gpu-exporter` on both nodes.

**Per-engine**: **one systemd template unit**, `vllm@.service`, instanced per engine —
`vllm@qwen3-vl-235b-a22b-instruct-nvfp4.service` — with the *same* instance name on every node it spans
(head vs. worker is computed from the node's position in the engine's `nodes` list,
not baked into the name; see ADR-0003). Everything that varies per engine lives in
`/opt/vllm/engines/<engine>.env`, rendered by `deploy`: image, model, rank, ports,
flags. The unit *logic* — the two boot gates, the marker lifecycle, the docker
invocation — lives in exactly one rarely-changing file (ADR-0018).

Every allowlisted engine's unit is **enabled on the node(s) it runs on**, all the
time. What actually boots is decided by two `ConditionPathExists` gates: a unit comes
up **iff** it is *desired* (`/opt/vllm/active/<engine>` — written by that node's
reconciler during an activation) **and** *was cleanly stopped last time*
(`/opt/vllm/state/vllm-<engine>.running` absent — ADR-0009).

### Web access

Caddy fronts `:80` and routes by hostname/path:
- `http://sparky.flummoxed.net/` — landing page · `/admin` — control panel (basic_auth)
- `http://chat.sparky.flummoxed.net/` — Open WebUI (login required)
- `http://api.sparky.flummoxed.net/` — **the stable model endpoint**
- `http://metrics.sparky.flummoxed.net/` — Grafana (anonymous view)

Needs a **wildcard DNS** record `*.sparky.flummoxed.net → sparky's IP`.

**The model endpoint is fixed and model-agnostic.** Its upstream list is *static* over
every node (`lb_policy first` + active health checks), never rewritten: since exactly
one engine is ever alive on :8000, routing follows activation with no reload and no
persisted state, and after a reboot Caddy converges the moment the restored engine
passes health. Every engine also advertises a stable served-model name (`sparky`)
alongside its real one, so **chat is portable across activations** while bench and
eval still address the real model. Open WebUI and Prometheus point at that one
address, configured once at deploy time.

`/admin` sits behind **basic_auth** (ADR-0008 built the seam; ADR-0018 turns it on,
since the panel now holds the activation grant). The password hash is a runtime
secret, never in git — set it once with `./sparky.sh admin-password`. Open WebUI has
**auth enabled**: the admin account is the first sign-up, then open sign-up closes
(admin adds users in Admin Panel → Users). Auth knobs are the `webui_*` vars in
`group_vars/all.yml`.

---

## Architecture

### Nodes & inventory

**sparky** (head) and **snoopy** (worker) are identical GB10 boxes:
- NVIDIA GB10 (Blackwell), compute capability 12.1 (sm_121), 128 GiB unified memory
  (121 GiB usable), CUDA 13.0, Driver 580.159.03, Ubuntu 24.04.4, Python 3.12.3.
- **ConnectX-7 at 200 Gbit** on `enp1s0f1np1` carries all NCCL/TP traffic (RoCE /
  RDMA active on `rocep1s0f1`): sparky `10.0.200.12`, snoopy `10.0.200.13`. The
  second ConnectX-7 pair is unused (NVIDIA guidance). A 10GbE LAN
  (`192.168.100.x`) is management-only.
- Passwordless SSH between nodes via geoff's `~/.ssh/id_ed25519_shared`. geoff has
  **no passwordless sudo** beyond the activation reconciler (ADR-0018 retired the old
  `systemctl`/`docker`/`journalctl`/`install` grant — see the identity model below).

`ansible/inventory.yml` lists sparky (head, local connection) and snoopy (worker,
ssh). Ansible is agentless — snoopy needs only Python + SSH.

### Identity model (who runs what)

Four identities, and **no web-API path to root** (ADR-0001, tightened by ADR-0018):
- **`geoff`** — human admin. Normal password-gated sudoer. Edits config, reviews,
  triggers deploys. Never holds passwordless root.
- **`deploy`** — automation identity. `NOPASSWD: ALL` on both nodes, owns the
  published runtime copy at `/opt/cluster` and its own SSH key. Ansible runs **as
  deploy**; geoff enters this context via `sudo -u deploy …` — his password is the
  gate, and it is now the **only** way in. No service runs as `deploy`.
- **`activator`** — the low-privilege **activation identity**. Runs the control panel
  and is what an agent acts as. Holds write access to `/opt/cluster/desired-profile`, an
  SSH key whose forced command on each worker is the reconciler, and **four
  single-command sudoers entries** — one fixed program each, and nothing else. Three
  validate their input; the fourth cannot, because the untrusted string *is* the payload,
  so it is bounded by what it confines that payload to:

  | program | ADR | what it is for |
  |---|---|---|
  | `vllm-activate` | 0018 | make an allowlisted profile live |
  | `vllm-probe` | 0019 | ask a **deployed** image a read-only question, so evaluating a model does not need `sudo docker` |
  | `vllm-suite` | 0021 | start a suite as a unit of its **own** — not privilege, *lifetime*: a measurement must not die with the panel that started it |
  | `vllm-sandbox` | 0024 | run one coding-benchmark answer under confinement — the only one handed a payload it cannot validate, so it is bounded by what it confines the payload **to** |

  Deliberately **not** in the `docker` group — docker group membership is root-equivalent
  and would re-open the hole this closes. Each program's allowlist is deploy-written, so
  **probing a new image, or running a new suite, is a deploy.**

- **`vllm`** — service account owning the model weights (uid 996, no home/shell).

Groups: **`cluster`** (geoff + deploy) owns `/opt/cluster` (mode 2775 + default ACLs)
so both can edit the project in place; **`activate`** (activator + geoff) carries the
activation grants, which is why `./sparky.sh activate` needs no password; **`adm`**
(geoff) makes the journal readable with no privilege at all.

Within `/opt/cluster` the two identities own different *kinds* of thing: **`deploy`
provisions and `activate` records.** The measurement artifacts a run produces — the trend
store, the run breadcrumbs, the smoke verdict, the scoreboard — are `activate`-writable;
the trees a deploy later executes are not, and the directory carries the sticky bit so
that stays true.

**geoff has no passwordless sudo either** — he keeps `(ALL : ALL) ALL` behind his
password, but nothing passwordless, because anything running as geoff inherits it, an agent
most of all. The deploy asserts on every node that the four bounded programs are his only
passwordless grants — exhaustively, so a fourth appearing fails the deploy. ADR-0018 argues
the case, including why a `docker` grant *is* a root grant.

The point of the split: `deploy` legitimately needs root to *provision* (apt, systemd
units, weights, docker), so it has blanket NOPASSWD — but nothing that faces the
network inherits it. Activation is still privileged, but it is **on rails**: the only
invocable thing is one fixed, input-validating program that can do nothing but
activate a deployed, allowlisted profile. sudo and sshd logs distinguish human,
automation, and agent actions for free.

`deploy` was created once by `ansible/bootstrap-deploy.sh` (the only step that can't
be Ansible — it creates the user Ansible runs as); `activator` and the `activate`
group are created by the `activate` role on every deploy. Ansible itself is apt's
`ansible-core`, only on sparky (the control node).

> After the first deploy that creates the `activate` group, **log out and back in**
> (or `newgrp activate`) so geoff picks up the membership — otherwise
> `./sparky.sh activate` will prompt for a password it shouldn't need.

### Runtime: the NVIDIA vLLM container

All CUDA-linked code runs inside an `nvcr.io/nvidia/vllm` image — the pypi aarch64
torch wheel is CPU-only, so a pip/venv install can't serve on this hardware; NVIDIA's
image ships matching torch + vLLM compiled for sm_121.

**The container is per-profile, and the fleet currently runs one.** `vllm_image` in a
profile overrides the `group_vars` default, so a container bump is adopted model by model
rather than fleet-wide — which is what keeps a bad image from taking the whole fleet with
it. Nothing straddles two containers today; the mechanism is there for when something
must.

| Container | Runs | Why |
|---|---|---|
| **26.07-py3** (vLLM 0.24.0, NCCL 2.30.7) — via the derived `dgx-spark/vllm:26.07-xgrammar-fix` | **every profile** | current. The derived image patches xgrammar (DEF-0010) — NVIDIA shipped it *below* vLLM's own declared minimum, breaking all tool-calling |

Progress and history are tracked in
[`docs/upgrades/`](docs/upgrades/container-nvidia-vllm-26.07-py3.md).

- **Multi-node:** vLLM 0.19 dropped Ray; native multinode uses
  `--nnodes / --node-rank / --master-addr / --headless` over torch.distributed. Both
  nodes rendezvous at `10.0.200.12:29500` over ConnectX-7.
- **Update path:** add/adjust the image in `container_images` (`group_vars/all.yml`)
  and point `vllm_image` at it, then `./sparky.sh deploy` — the `images` role pulls
  (or builds) it on every node before the units run (ADR-0013), and the unit files
  change so both services restart onto the new image. No manual `docker pull`/`build`.

### NCCL configuration

`/opt/vllm/nccl-env.conf` on both nodes (byte-identical, managed by the `common`
role from `roles/common/files/nccl-env.conf`) pins NCCL to ConnectX-7 / RoCE:

```
NCCL_SOCKET_IFNAME=enp1s0f1np1
NCCL_IB_HCA=rocep1s0f1
NCCL_IB_GID_INDEX=3
NCCL_NET_GDR_LEVEL=5
NCCL_IB_DISABLE=0
# NCCL_DEBUG=INFO   # uncomment for debugging
```

### Serving topology as a projection

A profile declares the serving topology **once**, as structured data, and `deploy`
projects it into the engine's env file — image, model, rank, ports, flags — so the
engine's whole per-variant surface is one flat, diffable file generated from one
declaration. Schema: [`docs/serving-topology.md`](docs/serving-topology.md).

The projection stops at the engine. Dependents — Open WebUI, Prometheus, Caddy — do not
vary by model at all; they point at a **fixed, model-agnostic endpoint** configured once at
deploy time. That is what makes `activate` unprivileged: choosing a model touches nothing
but systemd units and marker files.

---

## The harness

`sparky` is a real `uv`-managed Python package (ADR-0010), not a shell of wrappers.
The layer boundary: **Ansible = declarative config; Python (`sparky`) = the programs
that talk to the cluster; `sparky` itself = the operator entrypoint over both.**

- **Shared library, in four tiers** (imports point downward, enforced — ADR-0027):
  - **`foundation/`** — `api` (the vLLM client: readiness, chat, tool-shape probe),
    `topology` (profiles + `current-topology.json`), `scope` (the command scopes),
    `fleetlock` (the deploy↔run mutex). Depends on nothing else in the package.
  - **`verify/`** — the activation sanity checks: `text_sanity` (the multiturn
    corruption heuristics + the conversation that applies them), `vision_sanity` (the
    image gate), `smoke` (the gate that aggregates them). "Did serving come up right?",
    not "how good is it?".
  - **`serve/`** — `fleet` (the allowlist and what it implies per node), `activate` (the
    request + the reconciler trigger, and the smoke gate it runs on the way up), `ansible`
    (the deploy invoker). The tier that changes the cluster.
  - **`measure/`** — sub-grouped by role (it alone carries thirteen modules):
    **`loop/`** (`suite` naming/validation · `runner` the loop · `suitectl` the detach
    trigger), **`instruments/`** (`bench` throughput · `evals` MMLU-Pro · `coding` with its
    `sandbox` confinement and `reference` yardstick · `soak` · `tools`), and **`record/`**
    (`store` the trend db · `report` A/B compare · `scoreboard`).
- **Installed, not only published.** `deploy` puts the harness in a venv at
  `/opt/cluster/harness` (ADR-0021), because a detached suite run is a systemd unit and
  needs an interpreter that exists on a path root can name.
- **Tests** (`./sparky.sh test`, no hardware, seconds): ADR-0011's layered regiment —
  Layer 1 `lint` (ansible syntax-check **plus** validating the whole allowlist:
  fleet-wide-unique engine names, the one front port, flags that survive the env-file
  round trip), Layer 2 render tests (the `vllm@.service` boot gates; each engine's
  env file — rank, image, flags, ports, the reconnect hash), Layer 3 unit tests for
  the **reconciler's decision** — `(profile × env files × markers × live units) → the
  marker set + start/stop plan`, including reject-unknown-profile and
  fail-to-`empty` — and the control panel.
- **Smoke gate** — `./sparky.sh smoke` runs at the end of every `activate`: it probes
  each engine for readiness, the tool-call shape Open WebUI sends, and multiturn
  output quality (ADR-0012). It gates *activation*, not deploy — a selection-neutral
  deploy brings nothing up, so there'd be nothing for it to probe. The result lands in
  `/opt/cluster/last-smoke.json`, pass or fail; the reconciler deletes that file at
  the start of every activation so a stale verdict can't be read as a fresh one.

**Suites** are named, reviewed procedures — `suites/<name>.yml`, a flat
`(profile × regiment)` job list, started by `./sparky.sh run <name>` or from the panel
(ADR-0020, ADR-0021). A run is a systemd unit of its own, so it outlives the shell or the
web request that started it; it appends to a per-suite log, holds the cluster
exclusively, and resumes rather than restarts if it is stopped. The repo is where they are
authored; a deploy is what makes one startable.

Design records live in [`docs/adr/`](docs/adr/) — one file per decision:
ADR-0010 the harness, 0011 the test regiment, 0012 benchmarks, 0019 the bounded image probe, 0015 sparky as the
operator entrypoint, 0018 the deploy/activate split, 0020/0021 suites.

---

## Operations & recovery

### From scratch

The cluster can be rebuilt from the repo alone — weights are re-downloaded, everything else
is converged by a deploy. One step is not Ansible and cannot be:
`ansible/bootstrap-deploy.sh` creates the `deploy` user that Ansible then runs as.

The sequence is in [`skills/operations`](skills/operations/SKILL.md).

### Fail-safe boot (ADR-0009, extended by ADR-0018)

**Boot never depends on the reconciler** — recovery is the safety-critical path, and
custom code can't be a single point of failure for it. Every allowlisted unit stays
`enabled`; two `ConditionPathExists` gates decide what actually starts:

```ini
ConditionPathExists=/opt/vllm/active/%i               # desired — PER-NODE, reconciler-written
ConditionPathExists=!/opt/vllm/state/vllm-%i.running  # cleanly stopped last time
```

A unit boots **iff desired AND cleanly-stopped-last-time.** On a **clean reboot**
systemd attempts every enabled unit and the desired gate skips all but the last
activated profile → serving auto-restores with no reconciler involvement (the markers
carry the decision; the script needn't even exist). On a **hang / hard-reset / power
cut** the surviving `.running` marker skips the unit, so the node comes up **empty and
reachable** instead of re-attempting a risky load unattended.

Both gates were verified independently on 2026-08-08, and the negated gate then fired for
real the same day when a bad model froze a node — see
[ADR-0009](docs/adr/0009-fail-safe-boot.md) for the results and
[`docs/bring-up-failures.md`](docs/bring-up-failures.md) for the failure itself.

Recovery from the fail-safe state is an **activation**: `./sparky.sh activate
<profile>` (or the panel's "Re-activate") clears the marker and starts the engines.
The worker unit retries the rendezvous every 20 s until sparky's is up; the head
allows `TimeoutStartSec=1200` for snoopy to join, and a peer that never arrives falls
to `empty` rather than hanging. Open WebUI has `restart: always`, so Docker brings it
back on boot with no intervention; Caddy comes up with its static upstreams and
converges the moment the restored engine passes health.

> **Restored ≠ promoted.** A reboot *mid-run* restores the *last-activated* profile,
> which during a run is a transient candidate rather than the promoted serving model.
> Re-kick the suite, or re-activate the one you meant. A rough edge, not a hazard —
> the node is up and reachable throughout. A run that **finishes** puts back whatever was
> serving when it started: a measurement should not decide what serves, and left alone it
> would promote its last job by accident. Being *stopped* skips that, deliberately —
> systemd kills a stopped run before an activation could finish, so it leaves the
> candidate live and says so.

### Adding a model

Seven steps, and **exactly one needs a password**: the deploy that installs the weights and
engine files fleet-wide. Discovery, staging, checkpoint analysis, container probing,
activation and verification are all unprivileged — which is the capability ADR-0018's split
and ADR-0019's bounded probe were built to produce.

The procedure lives in [`skills/model-bringup`](skills/model-bringup/SKILL.md) (the
sequence and its traps) and [`skills/model-evaluation`](skills/model-evaluation/SKILL.md)
(fit checks and flags); [`docs/updating.md`](docs/updating.md) lists everything that must
move together. Removal is the same mechanism run backwards.

### When something goes wrong

Bring-up failures are catalogued in [`docs/bring-up-failures.md`](docs/bring-up-failures.md),
keyed on the literal error text — paste the error there. Operational symptoms (NVML inside
a container, `VLLM_HOST_IP`, NCCL, the Open WebUI model picker) are in
[`skills/operations`](skills/operations/SKILL.md).

---

## Project layout

The Ansible project + the `sparky` package live in this git repo — the **source of
truth**. `./sparky.sh deploy` publishes to `/opt/cluster` (the runtime copy the
`deploy` user reads) and applies it; you edit in the repo, the live copy only
changes on deploy.

```
<repo>/
├── README.md                  # this file (imported by CLAUDE.md as agent context)
├── sparky.sh                  # root wrapper → uv run sparky (the operator entrypoint)
├── pyproject.toml · uv.lock   # the sparky package (uv-managed)
├── sparky/                    # the harness, four tiers (ADR-0027): cli + foundation/
│                              #   (api topology scope fleetlock) · verify/ · serve/ ·
│                              #   measure/ (loop/ instruments/ record/)
├── tests/                     # pytest — render + control-panel + unit tests
├── scripts/download.py        # model staging (uv PEP-723 script; `sparky download`)
├── skills/                    # agent skills (model-discovery, model-evaluation, …)
├── docs/                      # profiles, profile-tuning, serving-topology,
│   ├── adr/                   #   control-interface, models/, upgrades/, and ADRs
│   ├── updating.md            #   change-pathway checklists (bump a container, add a model…)
│   ├── defects.md             #   register of open defects, each with a clears-when
│   ├── synchronization.md     #   who may touch the fleet and when — the locks, wait-vs-refuse
│   └── …
├── benchmark/                 # legacy bench scripts (being absorbed into sparky bench)
└── ansible/                   # THE Ansible project
    ├── inventory.yml · ansible.cfg · site.yml · teardown.yml
    ├── bootstrap-deploy.sh    # one-time: create the deploy user + /opt/cluster
    ├── group_vars/ · profiles/    # profiles/ IS the allowlist
    └── roles/
        ├── fleet/             #   read every profile → the allowlist + per-node facts
        ├── selection/         #   preserve the live model, or fall to `empty`
        ├── vllm/              #   vllm@.service (one template unit) + per-engine env files
        ├── activate/          #   the reconciler + the four bounded triggers + the identity
        ├── harness/           #   the harness venv a detached suite run executes (ADR-0021)
        ├── model/ · images/   #   convergent weights (per node) · images (ADR-0013)
        ├── fleet-state/       #   record the fleet, then converge the selection
        └── caddy/ · open-webui/ · control-panel/ · prometheus/ · grafana/ · exporters/

/opt/vllm/                     # the serving surface (root-owned, deploy-written)
├── models/                    #   weights this node holds
├── engines/<engine>.env       #   the whole per-variant surface, one file per engine
├── engines/allowlist          #   activatable profiles — re-validated on every activation
├── active/<engine>            #   DESIRED (per node) — written by the reconciler
└── state/vllm-<engine>.running  # CLEANLY-STOPPED sentinel (ADR-0009)

/opt/cluster/                  # PUBLISHED runtime tree (deploy-owned)
├── ansible/                   #   what ansible runs from (published each deploy)
├── sparky/                    #   the published harness SOURCE
├── harness/                   #   …installed into a venv — what a detached run executes
├── suites/                  #   installed suites: what MAY be started (ADR-0021)
├── suite-logs/<name>.log    #   one appended log per suite
├── desired-profile            #   THE REQUEST — group-writable, written with no sudo
├── current-topology.json      #   what IS running (reconciler-written)
├── fleet.json                 #   what MAY run (deploy-written)
├── last-smoke.json            #   the activation gate's last verdict
└── benchmark/                #   the trend store, plus a run's breadcrumbs
    ├── benchmark.db          #     the SQLite trend store
    └── breadcrumbs.json      #     resume state — activate-owned, so it can be replaced
```

---

## Known shortcomings

- **Open WebUI config is env-authoritative, not UI-authoritative.** The `open-webui`
  role sets `ENABLE_PERSISTENT_CONFIG=false`, so every config env var is re-asserted on
  **every** deploy. Trade-off: the **Admin Panel is effectively read-only for config**
  (settings changed there don't survive a deploy). To change a setting durably, set its
  env var in `group_vars/all.yml` (most admin settings are PersistentConfig values with
  a matching env var). User accounts, chats, and uploads are *data*, not config, and are
  unaffected. Since ADR-0018 that set is small and constant — Open WebUI is a vanilla,
  model-agnostic client — so this costs much less than it used to.
- **No per-model Open WebUI settings.** The client is deliberately model-agnostic, so a
  model cannot carry its own system prompt or sampling defaults in the UI. The bet is
  evidence-based (a transcript scan found every output-quality fix to be serving-side,
  in vLLM flags, and every Open WebUI env var to be auth/infrastructure only) and the
  escape hatch is a per-model connection if it ever breaks. Model config belongs in
  profiles.
- **A deploy that re-renders the live engine does not restart it.** `deploy` is
  selection-neutral: it will not drop a healthy engine as a side effect. The change is
  installed and reported as *pending* (in `sparky status`, the panel, and the deploy's
  own output); `./sparky.sh activate <profile>` applies it when you choose to take the
  reload.
- **`sparky bench` is interactive-only, and can't measure a single-node profile.** Both
  fall out of `vllm bench serve` living inside the container: reaching it means `sudo
  docker exec` (ADR-0018 retired the passwordless `docker` grant — a `docker` grant *is*
  root), and the container is only reachable on its own node, so bench refuses every
  `-single` profile — including whatever is usually serving. Deliberately left there:
  ADR-0016 rebuilds the regiment **HTTP-native** against the stable endpoint, where
  neither problem exists, rather than cutting a second privileged door into the boundary
  ADR-0018 just closed. See ADR-0018's errata.
- **No panel-triggered infra deploys.** Deliberate: the panel is off the `deploy`
  identity, so it can activate but not provision. Adding a model, changing a flag, or
  bumping a container is a password-gated CLI deploy. This is the automation given up
  in exchange for having no web-API path to root (ADR-0018).
- **Vision loses small detail in large images — silently.** A subject held at ~1% of the
  frame gets a confident wrong answer rather than a refusal: the encoder downscales, and
  detail below its effective resolution is gone before the model sees it. **Crop to the
  region of interest.** Detail in [`docs/profiles.md`](docs/profiles.md).

---

## Future work

The coding benchmark that used to head this list is built —
[ADR-0024](docs/adr/0024-coding-measurement.md), `./sparky.sh coding`. Every off-the-shelf
option is contaminated for models this recent (LiveCodeBench, the contamination-free one,
stopped updating in June 2025), so the answer is **constraining the interface** rather than
hiding the problem: an answer is compiled against a contract the problem declares, may
reach nothing the problem did not provide, and runs under confinement in an empty root.
What remains is to grow it — the sets are `python-basic` (an example, not a ranking
instrument) and `c-basic` (two problems, `draft`) — and the measurement axes ADR-0024
stages after correctness: instrumented primitives and their empirical complexity exponent,
impossible problems scored on whether a model recognises them, and per-objective scoring.

Also: voice mode (STT/TTS) alongside Open WebUI, and re-enabling the tabled perf
options (ADR-0014).

---

## Using with Claude Code

A tracked, thin `CLAUDE.md` imports this README (for full context) and documents the
agent skills in `skills/` — so cloning + opening Claude Code works with no setup.
Skills are **not** auto-registered as slash commands (that needs the vendor-specific
`.claude/skills/` location); instead `CLAUDE.md` tells Claude to read the relevant
`skills/<name>/SKILL.md` on demand. `.claude/` (local settings) is gitignored.

Development and git conventions — who stages, who commits, what belongs in one — live in
[`skills/development/SKILL.md`](skills/development/SKILL.md), which owns them. They are
not restated here: a rule in three places is a rule that drifts in two.
