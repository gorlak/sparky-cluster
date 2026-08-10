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
| **Operate** | no privilege, agent-drivable, needs a live cluster | `activate` `status` `fleet` `logs` `smoke` `bench` `eval` `sweep` `scoreboard` `report` `topology` `teardown` `probe` |
| **Develop** | repo only, no cluster, no privilege | `lint` `test` `download` |

That split is ADR-0018's subject, so the *provision* group is deliberately tiny — two
commands — and a test fails if it grows past three. `check` used to be a third; it is now
`deploy --check`, because it was `deploy` in every way that mattered (same code path, same
password gate) while reading like a development command.

```bash
# deploy — set the boundary of what MAY run (privileged, human, password-gated)
./sparky.sh deploy               # converge the fleet to ansible/profiles/*.yml
./sparky.sh deploy --evict       # …and reclaim the disk of de-allowlisted weights
./sparky.sh deploy --check       # dry-run (--check --diff); makes nothing
./sparky.sh deploy --check --evict   # …and what an evicting deploy would delete
./sparky.sh admin-password       # set the /admin basic_auth password (once)

# activate — choose what IS running (no root; this is the agent-drivable half)
./sparky.sh activate <profile>   # make it live: request → reconciler → wait → smoke gate
./sparky.sh activate             # what's live, and what's activatable
./sparky.sh activate empty       # stop serving; hardware free
./sparky.sh teardown             # alias for `activate empty` (--webui also stops the front-end)

# look at it
./sparky.sh fleet                # the allowlist: deployed / live / parked, and where the weights are
./sparky.sh status [--json]      # live health, both nodes (exit code = the verdict)
./sparky.sh logs [head|worker]   # follow a node's vLLM journal

# measure it
./sparky.sh sweep <spec.yml>     # run a job list end to end (ADR-0016) — resumable
#   long ones: detach with setsid (skills/operations) — it needs no TTY, unlike deploy
./sparky.sh smoke                # gate: readiness + tool-shape + multiturn quality
./sparky.sh bench <label>        # run vllm bench serve scenarios → record to the trend store
./sparky.sh report <a> <b>       # compare two benchmark labels (direction-aware A/B)
./sparky.sh topology <profile>   # show a profile's engines / nodes / ports / served names
./sparky.sh probe <what> [args]  # ask a DEPLOYED image a read-only question (no root)

# develop it
./sparky.sh test [-k …]          # harness unit tests (pytest)
./sparky.sh lint                 # ansible syntax-check + validate the whole allowlist
./sparky.sh download <hf-repo>   # stage a model into the inbox
```

**`deploy`** first **publishes** the repo to the deploy-owned runtime tree
(`/opt/cluster`), then runs `ansible-playbook` there as the `deploy` user — its
`NOPASSWD` sudo is the automation gate (`sudo -u deploy` prompts for your password;
that's the gate into the automation context). It takes **no profile argument**: it
installs every allowlisted profile's weights, images, engine env files and the
activation grants. It preserves whatever is currently serving (falling to `empty`
only if that profile left the allowlist) and **never auto-promotes** a model.

> **Run a long deploy under `tmux`.** `deploy` runs in the foreground and needs a TTY
> for the sudo password, so it cannot simply be backgrounded — and a dropped SSH session
> (a phone client quitting, a laptop sleeping) sends SIGHUP and kills it mid-run. A
> deploy that moves weights can run for tens of minutes, which is plenty of time for
> that to happen.
>
> ```bash
> tmux new -s deploy       # then run ./sparky.sh deploy inside it
> ```
>
> Being killed this way is *safe* — Ansible is idempotent, rsync re-sends a partial
> file rather than trusting it, and a run that never reaches the `fleet-state` role has
> not touched the allowlist or the engine files, so the fleet keeps serving what it was.
> Re-running picks up where it left off. `tmux attach -t deploy` to get back to it.

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
> importable (`from sparky import topology, bench, report, ansible`), so the cluster
> can be driven from a script or a notebook too. See "The harness" below.

---

## Current state — profiles (the allowlist)

Profiles live at `ansible/profiles/<name>.yml`; each captures the full
`serving_topology` (engines, models, nodes, ports, `gmu`, `max_model_len`). Names are
the `<model>-<version>-<quant>` triple; a `-single` suffix marks the single-node
(snoopy) TP=1 shape, while bare big-shared profiles are TP=2 across both nodes.
Single-node serving runs on **snoopy by design** — sparky is the head (frontends) +
dev node, so single-node models serve on the resource-richer worker. (The per-node
`-dual` duplicate shape was retired: two independent endpoints of one model buy
nothing without a round-robin fronting them.)

**The profiles directory *is* the allowlist** (ADR-0018) — there is no separate
manifest to drift. A profile file means "keep these weights and install these
engines"; `activate` then picks one to serve. Two gestures follow:

- **`blocked: true`** — parked. Weights and engine files are kept (so re-testing costs
  no download), but it cannot be activated.
- **delete the `.yml`** — it leaves the allowlist, so the next `deploy` **evicts its
  weights** from the nodes that held them (reported first; `--evict` to apply).

*Block to park it; delete the file to evict it.*

**Big-shared (TP=2 across both nodes)** — the shape, not *a* shape. Measured 2026-08-10
across three paired profiles: TP=2 beats TP=1 on decode (1.34–1.59×), throughput (+41–50%)
and KV capacity, on every model tried. The `-single` twins were deleted once that landed —
see [`docs/profile-tuning.md`](docs/profile-tuning.md), which used to claim the opposite.

| Profile | Shape | decode | usable ctx / KV |
|---|---|---|---|
| `qwen3-vl-235b-a22b-instruct-nvfp4` | Qwen3-VL-235B — **75.0%** MMLU-Pro subset, vision + tools (`hermes`) | 23.8 tok/s | 131k / 534k |
| `nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4` | Nemotron-3-Puzzle-75B — hybrid Mamba; **the long-context model** | 32.0 tok/s | 131k / **35.2M** |
| `nvidia-nemotron-3-super-120b-a12b-nvfp4` | Nemotron-3-Super-120B-A12B — Puzzle's uncompressed upstream; `MIXED_PRECISION` despite the name | *new* | 262k / ~31M est |
| `qwen3.6-35b-a3b-nvfp4` | Qwen3.6-35B-A3B — the fast generalist | **100.2 tok/s** | 262k / 16.3M |
| `qwen3-coder-next-nvfp4` | Qwen3-Coder-Next | 54.0 tok/s | 262k / 5.98M |
| `minimax-m2.7-nvfp4` | MiniMax-M2.7 — soaked 64 min clean; reasons past the eval's cap on 32% of items | 24.9 tok/s | 131k / 449k |
| `mistral-medium-3.5-128b-nvfp4` | Mistral-Medium-3.5 (`MIXED_PRECISION`, `--tokenizer-mode mistral`) — the European option | — | — |
| `step-3.7-flash-nvfp4` | **⛔ parked** (`blocked: true`; upstream VL bug, DEF-0006 — re-probed on 26.07, still missing) | — | — |

**Every profile is offering far less context than it holds.** The `usable ctx` column is
`max_model_len` — a number we chose — against the KV cache actually allocated. Nemotron
serves 131k out of 35.2M. Raising these is config, not hardware.

**Single-node (TP=1 on snoopy)** — one left, and it is parked. The performance case for this
shape is gone; the only remaining argument is fleet occupancy, since TP=2 takes both nodes
and leaves ~24 GiB of dev headroom on sparky rather than the whole box.

None are live. The last one, `qwen3-vl-32b-instruct-nvfp4-single`, was retired on 2026-08-10 —
not because [DEF-0013](docs/defects.md) still blocks it (it does), but because its niche
closed: it existed as the cheapest route to vision, and `qwen3-vl-235b-a22b-instruct-nvfp4` serves vision
*and* tools at 75.0%. Retired configs are kept in
[`ansible/profiles/retired/`](ansible/profiles/retired/) — the memory math and the verified
parser names, so reviving one costs no re-derivation.

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
the register that *owns* those verdicts, so a discovery sweep never re-litigates one.
Two entries today: `Qwen3.5-122B-A10B-FP8` (froze sparky during load — never deploy) and
`MiniMax-M3` (does not fit under TP=2). Read it before proposing a model.

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
  and is what an agent acts as. Holds exactly four things: write access to
  `/opt/cluster/desired-profile`, a single-command sudoers entry for
  `/usr/local/sbin/vllm-activate`, an SSH key whose forced command on each worker
  is that same reconciler, and — since ADR-0019 — a second single-command entry for
  `/usr/local/sbin/vllm-probe`, which introspects an already-deployed container image
  and can do nothing else. Deliberately **not** in the `docker` group — docker group
  membership is root-equivalent and would re-open the hole this closes.

  The probe exists because *evaluating* a model means asking questions of the container
  ("does this vLLM know `Mistral3ForConditionalGeneration`?", "what NCCL does it
  ship?"), and every one of those otherwise needs `sudo docker` — i.e. root. It is
  bounded the same way the reconciler is: the image must be in a root-owned,
  deploy-written allowlist, the probe is a key into a fixed set of programs, arguments
  must be bare identifiers, and the docker flags are constants with no bind mounts, no
  `--gpus`, no network and no capabilities. **Probing something new is a deploy.**
- **`vllm`** — service account owning the model weights (uid 996, no home/shell).

Groups: **`cluster`** (geoff + deploy) owns `/opt/cluster` (mode 2775 + default ACLs)
so both can edit the project in place; **`activate`** (activator + geoff) carries the
activation grants, which is why `./sparky.sh activate` needs no password; **`adm`**
(geoff) makes the journal readable with no privilege at all.

**geoff has no passwordless sudo either.** He used to carry
`NOPASSWD: /usr/bin/systemctl, /usr/bin/docker, /usr/bin/journalctl, /usr/bin/install`
— a convenience, and four separate passwordless routes to a root shell (`docker run -v
/:/host`; `systemctl edit` runs `$EDITOR` as root; `journalctl` pages through `less`,
and `!sh` is a shell; `install` writes any file anywhere). ADR-0018 removed it. Not
because geoff shouldn't have root — he keeps `(ALL : ALL) ALL` behind his password —
but because **anything running as geoff inherits it, an agent most of all**, and "the
agent gets `activate` and nothing privileged" is not true of a machine where the agent
can `sudo docker`. A boundary a `docker run` walks around is theatre. The deploy
asserts, on every node, that the reconciler is geoff's only passwordless grant.

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

**The container is per-profile — and as of 2026-08-10 the fleet is back to one.**
`vllm_image` in a profile overrides the `group_vars` default, so a container bump is
adopted model by model rather than fleet-wide — which is what made the 26.07 campaign
survivable when one model turned out to be a node-killer on it. The mechanism stays; it
simply has nothing to straddle right now.

| Container | Runs | Why |
|---|---|---|
| **26.07-py3** (vLLM 0.24.0, NCCL 2.30.7) — via the derived `dgx-spark/vllm:26.07-xgrammar-fix` | **every profile** | current. The derived image patches xgrammar (DEF-0010) — NVIDIA shipped it *below* vLLM's own declared minimum, breaking all tool-calling |

Nothing runs on 26.06 any more: 26.07 fixed its fastapi defect (DEF-0005) and the
derived image built for it has been deleted. **26.04 left with `step-3.5-flash-fp8`**, the last
profile pinned to it — retired on measurement, see
[`docs/models/tombstones.md`](docs/models/tombstones.md). A single-container fleet is a
simplification, not a policy: the next model that needs a different image gets one. Progress is tracked in
[`docs/upgrades/container-nvidia-vllm-26.07-py3.md`](docs/upgrades/container-nvidia-vllm-26.07-py3.md).
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

What changed with ADR-0018 is *how far* the projection reaches. It used to run all
the way out to the dependents: a profile switch re-templated Open WebUI, Prometheus
and Caddy in the same run, keeping them in sync by construction. That coupling is
what forced a model switch to be a privileged, whole-config operation. Now the
dependents don't vary by model at all — they point at a **fixed, model-agnostic
endpoint** (above) configured once at deploy time. Same guarantee, reached by removing
the dependency instead of re-deriving it, and *that* is what makes `activate`
unprivileged: choosing a model touches nothing but systemd units and marker files.

---

## The harness

`sparky` is a real `uv`-managed Python package (ADR-0010), not a shell of wrappers.
The layer boundary: **Ansible = declarative config; Python (`sparky`) = the programs
that talk to the cluster; `sparky` itself = the operator entrypoint over both.**

- **Shared library** — `topology` (profiles + `current-topology.json`), `fleet` (the
  allowlist and what it implies per node), `activate` (the request + the reconciler
  trigger), `api` (the vLLM client: readiness, chat, tool-shape probe), `store`
  (SQLite trend db), `quality` (multiturn corruption heuristics), `multiturn` (the
  quality conversation), `bench` / `report` (the `vllm bench serve` runner + A/B
  compare), `ansible` (the deploy invoker).
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

Design records live in [`docs/adr/`](docs/adr/) — one file per decision:
ADR-0010 the harness, 0011 the test regiment, 0012 benchmarks, 0019 the bounded image probe, 0015 sparky as the
operator entrypoint, 0018 the deploy/activate split.

---

## Operations & recovery

### From scratch / disaster recovery

```bash
# 0. Clone the repo on a fresh control node (or it's already here).
git clone <remote> <repo>

# 1. (Once per cluster) create the deploy identity + /opt/cluster. Run as geoff on
#    sparky; prompts for sudo on both nodes. Then log out/in (or `newgrp cluster`).
bash <repo>/ansible/bootstrap-deploy.sh

# 2. Set the /admin password. The panel holds the activation grant, so a deploy
#    refuses to serve it without one.
./sparky.sh admin-password

# 3. Stage model weights into the inbox on the control node. The deploy moves them
#    into the canonical store (/opt/vllm/models) on the head and mirrors to each node
#    that runs them — no manual copy to snoopy.
./sparky.sh download stepfun-ai/Step-3.5-Flash-FP8

# 4. Deploy the fleet — publishes the repo, then handles both nodes end to end.
#    Installs every allowlisted profile; brings nothing up.
./sparky.sh deploy

# 5. Log out/in (or `newgrp activate`) to pick up the activation group, then choose
#    what serves. This one needs no password.
./sparky.sh activate qwen3-vl-235b-a22b-instruct-nvfp4
```

The `model` role is idempotent (skips install if weights are already at
`/opt/vllm/models/<MODEL>`). NCCL config and the `vllm` user are handled by the
`common` role every run.

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

**Verified on 2026-08-08**, both gates independently, without a hard reset: a clean
reboot of snoopy left five of six enabled instances skipped for want of a desired marker
and started the sixth on its own (no reconciler involved); a planted `.running` marker
then made that same engine skip on the *negated* gate, leaving the node up, reachable
and serving nothing. systemd names the failing condition in the journal either way.

**Then it fired for real, the same day.** Activating `minimax-m2.7-awq-2607` (the
DEF-0004 experiment) exhausted host memory during weight load and **froze sparky** —
unresponsive, recovered only by a physical power cycle. The `.running` marker survived
the reset, which is precisely the signal it exists to carry: the stop was not clean, so
on boot systemd refused to re-attempt the load that had just killed the machine.

> `vllm@minimax-awq-2607.service was skipped because of an unmet condition check`
> `(ConditionPathExists=!/opt/vllm/state/vllm-minimax-awq-2607.running)`

sparky came back **empty and reachable in four minutes** rather than freezing again
unattended, and recovery was an ordinary unprivileged `activate`. The synthetic test
showed the gates work; this showed the design was aimed at the right hazard.

Recovery from the fail-safe state is an **activation**: `./sparky.sh activate
<profile>` (or the panel's "Re-activate") clears the marker and starts the engines.
The worker unit retries the rendezvous every 20 s until sparky's is up; the head
allows `TimeoutStartSec=1200` for snoopy to join, and a peer that never arrives falls
to `empty` rather than hanging. Open WebUI has `restart: always`, so Docker brings it
back on boot with no intervention; Caddy comes up with its static upstreams and
converges the moment the restored engine passes health.

> **Restored ≠ promoted.** A reboot *mid-sweep* restores the *last-activated* profile,
> which during a sweep is a transient candidate rather than the promoted serving model.
> Re-kick the sweep, or re-activate the promoted model. A rough edge, not a hazard —
> the node is up and reachable throughout.

### Bringing up a new model — the whole lifecycle

Seven steps, and **only step 5 needs root or a human.** Everything else is
agent-drivable, which is the point of the ADR-0018 split and ADR-0019's probe.

| # | Step | Command | Needs geoff? |
|---|---|---|---|
| 1 | **Discover** a candidate | [`skills/model-discovery`](skills/model-discovery/SKILL.md) — and check [`docs/models/tombstones.md`](docs/models/tombstones.md) first, so a rejected model is never re-litigated | no |
| 2 | **Stage** the weights | `./sparky.sh download <hf-repo>` → the inbox | no |
| 3 | **Analyse** the checkpoint | `du -sh`, then `config.json`: architecture, real quant, KV scheme. Do the per-node memory math at your TP ([`skills/model-evaluation`](skills/model-evaluation/SKILL.md)) → a fact sheet in `docs/models/` | no |
| 4 | **Probe** the container | `./sparky.sh probe archs <Arch>` · `probe quant` · `probe versions` — does the vLLM we run actually support it? | no |
| 5 | **Profile + deploy** | write `ansible/profiles/<name>.yml`, `./sparky.sh lint`, then **`./sparky.sh deploy`** — adopts the weights, installs the engine fleet-wide | **yes** — password-gated |
| 6 | **Activate** | `./sparky.sh activate <name>` — reconciler, then the smoke gate | no |
| 7 | **Verify** | tool-choice shapes, a concurrency soak, `./sparky.sh bench` | no |

**Steps 3 and 4 are the cheap ones, and skipping them is expensive.** The checkpoint
rarely matches its own repo name — `Mistral-Medium-3.5-128B-NVFP4` is actually
`MIXED_PRECISION` (FP8 *and* NVFP4 layers) and declares an FP8 KV cache, both of which
change the flags and the memory math. And an architecture the container does not know
fails minutes into a load rather than in the twenty seconds a probe costs.

For step 5, copy the profile whose *shape* matches — every live profile is big-shared
TP=2, so start from `minimax-m2.7-nvfp4.yml`, or `nvidia-nemotron-3-super-120b-a12b-nvfp4.yml` when the
checkpoint is `MIXED_PRECISION` (which the repo name will not tell you). If you genuinely
need TP=1 to leave a node free, take a config from
[`ansible/profiles/retired/`](ansible/profiles/retired/) and re-verify its parsers first.

**Removing** is the same mechanism, run backwards: delete the `.yml` and
`./sparky.sh deploy`. The deploy reports which weights are now unreferenced and
leaves them alone; `./sparky.sh deploy --evict` deletes them. It will never delete the
model that is currently serving — if the live profile is the one leaving the
allowlist, the deploy drives the fleet to `empty` first and waits for the engine to
stop. **`--evict` reclaims images too** (since 2026-08-10), converging the image store
to `container_images` exactly as it converges weights to the allowlist — plus the dangling
layers that every rebuild of the derived image leaves behind. It was previously claimed
that shared base layers made this unsafe; that was wrong. Docker refcounts layers, so
removing an image frees only what nothing else needs, and `docker rmi` refuses outright
while a container holds it — including the engine serving right now, which is exactly the
behaviour we want. Those failures are reported, not fatal. The cost of the old belief was
26.04 and 26.06 sitting on both nodes long after nothing ran them.

One authoring constraint (ADR-0018): a serve flag travels to systemd as one
single-quoted env value that is re-split on whitespace, with no quote processing.
Spaces and double quotes are fine — `--tool-call-parser step3p5` and
`--speculative-config {"method":"mtp"}` both work — but a **single quote** would
terminate the value early. `deploy` and `lint` reject one rather than mis-splitting it.

Memory budgeting is **per-profile**: `gpu_memory_utilization` is a deliberate split
between vLLM and what's left for OS + dev work, not a safety margin. See
[`docs/profile-tuning.md`](docs/profile-tuning.md).

### Troubleshooting

- **Garbage / multilingual output:** `--quantization fp8` present on an
  already-quantized checkpoint (double-quantization). Never pass `--quantization`
  for a checkpoint that declares its quantization in `config.json`.
- **Nonstop thinking after the Nth turn:** FP8 KV cache (`--kv-cache-dtype fp8`)
  and/or `--enable-prefix-caching` — disable one or both (see Known Shortcomings).
- **NVML init fails inside container** ("Failed to initialize NVML: Unknown Error"):
  missing `--cgroupns=host`, or a `systemctl daemon-reload` ran while the container
  was up. Restart the container.
- **`VLLM_HOST_IP` must match the ConnectX-7 IP per node** (sparky `10.0.200.12`,
  snoopy `10.0.200.13`) — the default route goes out the 10GbE NIC, so without it
  torch.distributed rendezvous advertises the wrong IP and fails.
- **API never comes up / NCCL errors:** `./sparky.sh logs head` and
  `./sparky.sh logs worker`; uncomment `NCCL_DEBUG=INFO` in
  `roles/common/files/nccl-env.conf` and redeploy.
- **Model not selected in Open WebUI:** click the model dropdown and select the
  engine — it doesn't auto-select on first load.

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
├── sparky/                    # the harness: cli, ansible, activate, fleet, topology,
│                              #   api, store, quality, multiturn, bench, report
├── tests/                     # pytest — render + control-panel + unit tests
├── scripts/download.py        # model staging (uv PEP-723 script; `sparky download`)
├── skills/                    # agent skills (model-discovery, model-evaluation, …)
├── docs/                      # profiles, profile-tuning, serving-topology,
│   ├── adr/                   #   control-interface, models/, upgrades/, and ADRs
│   ├── updating.md            #   change-pathway checklists (bump a container, add a model…)
│   ├── defects.md             #   register of open defects, each with a clears-when
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
        ├── activate/          #   the reconciler + its two bounded triggers + the identity
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
├── sparky/                    #   the published harness (agents + the sweep runner)
├── desired-profile            #   THE REQUEST — group-writable, written with no sudo
├── current-topology.json      #   what IS running (reconciler-written)
├── fleet.json                 #   what MAY run (deploy-written)
├── last-smoke.json            #   the activation gate's last verdict
└── benchmark/benchmark.db     #   the SQLite trend store
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
- **Vision loses small detail in large images — silently.** Verified end-to-end
  2026-08-08 (through Caddy, on the stable `sparky` alias): a 12 MB / 3 MP upload is
  accepted and answered correctly when the subject is a reasonable fraction of the
  frame. Hold the subject at ~1% of the width and the model returns HTTP 200 and a
  confident **wrong** answer rather than refusing. The vision encoder downscales, and
  detail below its effective resolution is gone before the model ever sees it. In
  practice: a small error message inside a full-screen screenshot may be misread, not
  flagged. Crop to the region of interest. There is no transport limit — the proxy
  passed 12 MB without complaint.
- ~~**`--kv-cache-dtype fp8` + `--enable-prefix-caching` disabled**~~ — **resolved
  2026-08-10.** This was a vLLM 0.19 multi-turn corruption (DEF-0007) carried by
  `step-3.5-flash-fp8`, the only profile where it was ever observed. Both options ran clean for
  30 growing-context turns on 0.24.0, and that profile has since been retired along with
  the 26.04 container, so no configuration can exhibit it. The defect is **closed** —
  deliberately, rather than watched forever on a shape that no longer exists.

---

## Future work

The **fleet orchestrator is built** (ADR-0016, accepted 2026-08-10). `./sparky.sh sweep
<spec.yml>` runs a flat `profile × regiment` job list end to end: exclusive lock so nothing
else touches the cluster, breadcrumbs after every regiment so an interrupted run resumes
rather than restarts, and per-profile quarantine so a node-killer costs one activation
instead of the night. Regiments today: `bench`, `quality`, `tools` (all four `tool_choice`
shapes — the smoke gate only sends one, which is how DEF-0011 hid), and `soak` (sustained
concurrency, watching for a stall rather than a crash).

What that leaves is a **measurement gap, not a mechanism gap**: there is still no coding
benchmark, and for software-development work that is the axis that matters most. Every
off-the-shelf option is contaminated for models this recent — LiveCodeBench, the
contamination-free one, stopped updating in June 2025 — and a real one needs an execution
sandbox for untrusted model output. That is the next ADR.

Also: voice mode (STT/TTS) alongside Open WebUI, and re-enabling the tabled perf
options (ADR-0014).

---

## Using with Claude Code

A tracked, thin `CLAUDE.md` imports this README (for full context) and documents the
agent skills in `skills/` — so cloning + opening Claude Code works with no setup.
Skills are **not** auto-registered as slash commands (that needs the vendor-specific
`.claude/skills/` location); instead `CLAUDE.md` tells Claude to read the relevant
`skills/<name>/SKILL.md` on demand. `.claude/` (local settings) is gitignored.

**Geoff runs all git commits** — prepare and stage, but never `git commit` unless
asked.
