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
`uv run` shim — no install, works from any cwd):

```bash
./sparky.sh deploy <profile>     # publish + apply a profile (site.yml); restarts only changed units
./sparky.sh check <profile>      # dry-run a deploy (--check --diff); makes nothing
./sparky.sh teardown [--webui]   # stop + disable vLLM on both nodes (--webui also stops Open WebUI)
./sparky.sh status               # systemd state of the vLLM units on both nodes
./sparky.sh logs [head|worker]   # follow a node's vLLM journal

./sparky.sh smoke                # post-deploy gate: readiness + tool-shape + multiturn quality
./sparky.sh bench <label>        # run vllm bench serve scenarios → record to the trend store
./sparky.sh report <a> <b>       # compare two benchmark labels (direction-aware A/B)
./sparky.sh topology <profile>   # show a profile's engines / nodes / ports / served names

./sparky.sh test [-k …]          # harness unit tests (pytest)
./sparky.sh lint                 # ansible syntax-check across every profile + teardown
./sparky.sh download <hf-repo>   # stage a model into the inbox
```

`deploy` first **publishes** the repo to the deploy-owned runtime tree
(`/opt/cluster`), then runs `ansible-playbook` there as the `deploy` user — its
`NOPASSWD` sudo is the automation gate (`sudo -u deploy` prompts for your password;
that's the gate into the automation context). Ansible diffs current vs. desired
state and tears down / reconfigures / brings up as needed.

**Live state** (which profile is deployed, which engines are up):
- `http://sparky.flummoxed.net/admin` — control panel (status + per-engine actions)
- `./sparky.sh status` — systemd state on both nodes
- `/opt/cluster/current-topology.json` — written at the end of every deploy

> The `sparky` command is a Python package, not just a CLI — the same functions are
> importable (`from sparky import topology, bench, report, ansible`), so the cluster
> can be driven from a script or a notebook too. See "The harness" below.

---

## Current state — profiles

Profiles live at `ansible/profiles/<name>.yml`; each captures the full
`serving_topology` (engines, models, nodes, ports, `gmu`, `max_model_len`) plus
front-end toggles. Names are the `<model>-<version>-<quant>` triple; a `-single`
suffix marks the single-node (snoopy) TP=1 shape, while bare big-shared profiles are
TP=2 across both nodes. Single-node serving runs on **snoopy by design** — sparky is
the head (frontends) + dev node, so single-node models serve on the resource-richer
worker. (The per-node `-dual` duplicate shape was retired: two independent endpoints
of one model buy nothing without a round-robin fronting them.)

| Profile | Shape |
|---|---|
| `step-3.5-fp8` | Step-3.5-Flash-FP8 TP=2 across both nodes (fully-committed big-shared) — stable |
| `step-3.7-nvfp4` | Step-3.7-Flash-NVFP4 TP=2 on 26.06 — **⛔ blocked** (upstream vLLM VL bug, DEF-0006; hidden from deploy UI) |
| `minimax-m2.7-awq` | MiniMax-M2.7-AWQ TP=2 across both nodes (big-shared, ~30 GiB/node dev headroom) |
| `minimax-m2.7-nvfp4` | MiniMax-M2.7-NVFP4 TP=2 on 26.06 — NVFP4 A/B vs the AWQ profile |
| `qwen3-coder-nvfp4-single` | Qwen3-Coder-Next (NVFP4) on snoopy, TP=1 (sparky free for dev) |
| `qwen3.6-35b-nvfp4-mtp3-single` | Qwen3.6-35B-A3B (NVFP4, **MTP-3**) on snoopy — reasoning-generalist; 2.3× single-stream decode (ADR-0014) |
| `empty` | nothing serving; full hardware available |

See [`docs/profiles.md`](docs/profiles.md) for what each serves and how to switch;
[`docs/profile-tuning.md`](docs/profile-tuning.md) for the *why* — picking
`gpu_memory_utilization` as a deliberate split between vLLM and system/dev memory,
plus the GB10 unified-memory accounting quirk.

**Do not use:** `Qwen3.5-122B-A10B-FP8` — froze sparky during load (DEF-0008,
[`docs/defects.md`](docs/defects.md)).

### Services

**Always running** (independent of the deployed profile): `caddy` (reverse proxy on
`:80`), `open-webui` (chat UI, fronted at `chat.{web_domain}`),
`control-panel.service` (FastAPI status + actions as `User=deploy`, at `/admin`),
`prometheus`, `grafana` (at `metrics.{web_domain}`), and `node-exporter` +
`nvidia-gpu-exporter` on both nodes.

**Per-profile** (dynamic): one `vllm-<engine>.service` per vLLM engine the active
profile declares — the *same* unit name on every node it spans (head vs. worker is
computed from the node's position in the engine's `nodes` list, not baked into the
name; see ADR-0003).

### Web access

Caddy fronts `:80` and routes by hostname/path:
- `http://sparky.flummoxed.net/` — landing page · `/admin` — control panel
- `http://chat.sparky.flummoxed.net/` — Open WebUI (login required)
- `http://metrics.sparky.flummoxed.net/` — Grafana (anonymous view)

Needs a **wildcard DNS** record `*.sparky.flummoxed.net → sparky's IP`. Open WebUI
has **auth enabled**: the admin account is the first sign-up, then open sign-up
closes (admin adds users in Admin Panel → Users). Auth knobs are the `webui_*` vars
in `group_vars/all.yml`.

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
- Passwordless SSH between nodes via geoff's `~/.ssh/id_ed25519_shared`.
  Passwordless sudo for geoff is limited to `systemctl`, `docker`, `journalctl`,
  `install`.

`ansible/inventory.yml` lists sparky (head, local connection) and snoopy (worker,
ssh). Ansible is agentless — snoopy needs only Python + SSH.

### Identity model (who runs what)

Three-tier separation of concerns:
- **`geoff`** — human admin. Normal password-gated sudoer. Edits config, reviews,
  triggers deploys. Never holds passwordless root.
- **`deploy`** — automation identity. `NOPASSWD: ALL` on both nodes, owns the
  published runtime copy at `/opt/cluster` and its own SSH key. Ansible runs **as
  deploy**; geoff enters this context via `sudo -u deploy …` (his password is the
  gate), and the control panel runs as a `User=deploy` service (no password).
- **`vllm`** — service account owning the model weights (uid 996, no home/shell).
- **`cluster`** group — geoff + deploy, owns `/opt/cluster` (mode 2775 + default
  ACLs) so both can edit the project in place.

`deploy` was created once by `ansible/bootstrap-deploy.sh` (the only step that can't
be Ansible — it creates the user Ansible runs as). Ansible itself is apt's
`ansible-core`, only on sparky (the control node).

### Runtime: the NVIDIA vLLM container

All CUDA-linked code runs inside `nvcr.io/nvidia/vllm:26.04-py3` — the pypi aarch64
torch wheel is CPU-only, so a pip/venv install can't serve on this hardware; NVIDIA's
image ships matching torch + vLLM compiled for sm_121.

- **Why 26.04:** SM12.1 CUTLASS kernels were broken in 26.03 (fixed in vLLM PR
  #38126). **Do not move to 26.06 globally yet** — it ships NCCL 2.30.5, whose NVLS
  regression hard-hangs dual GB10 at multinode bring-up. The 26.06 upgrade (needed
  for NVFP4) is per-profile and upstream-blocked — tracked in
  [`docs/upgrades/container-nvidia-vllm-26.06-py3.md`](docs/upgrades/container-nvidia-vllm-26.06-py3.md).
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

A profile declares the serving topology **once**, as structured data; every
model-dependent service (the vLLM units, Prometheus scrape targets, Open WebUI
connections, the control panel, Caddy) is a **projection** of that one declaration,
so `./sparky.sh deploy <profile>` reshapes serving *and* reconfigures its dependents
in the same run — they stay in sync by construction. Schema:
[`docs/serving-topology.md`](docs/serving-topology.md).

---

## The harness

`sparky` is a real `uv`-managed Python package (ADR-0010), not a shell of wrappers.
The layer boundary: **Ansible = declarative config; Python (`sparky`) = the programs
that talk to the cluster; `sparky` itself = the operator entrypoint over both.**

- **Shared library** — `topology` (profiles + `current-topology.json`), `api` (the
  vLLM client: readiness, chat, tool-shape probe), `store` (SQLite trend db),
  `quality` (multiturn corruption heuristics), `multiturn` (the quality
  conversation), `bench` / `report` (the `vllm bench serve` runner + A/B compare),
  `ansible` (the deploy invoker).
- **Tests** (`./sparky.sh test`, no hardware, seconds): ADR-0011's layered regiment
  — Layer 1 `lint` (ansible syntax-check), Layer 2 template-render tests (the
  `vllm.service.j2` logic — rank, fail-safe markers, the reconnect hash), Layer 3
  control-panel unit tests.
- **Smoke gate** — `./sparky.sh smoke` runs as the last step of every deploy (the
  `smoke` role in `site.yml`): it probes each engine for readiness, the tool-call
  shape Open WebUI sends, and multiturn output quality, and **fails the deploy** if
  an engine is corrupt — *before* the topology is recorded as live (ADR-0012).

Design records live in [`docs/adr/`](docs/adr/) — one file per decision:
ADR-0010 the harness, 0011 the test regiment, 0012 benchmarks, 0015 sparky as the
operator entrypoint.

---

## Operations & recovery

### From scratch / disaster recovery

```bash
# 0. Clone the repo on a fresh control node (or it's already here).
git clone <remote> <repo>

# 1. (Once per cluster) create the deploy identity + /opt/cluster. Run as geoff on
#    sparky; prompts for sudo on both nodes. Then log out/in (or `newgrp cluster`).
bash <repo>/ansible/bootstrap-deploy.sh

# 2. Stage model weights into the inbox on the control node. The deploy moves them
#    into the canonical store (/opt/vllm/models) on the head and rsync-mirrors to
#    every node — no manual copy to snoopy.
./sparky.sh download stepfun-ai/Step-3.5-Flash-FP8

# 3. Deploy a profile — publishes the repo, then handles both nodes end to end.
./sparky.sh deploy step-3.5-fp8
```

The `model` role is idempotent (skips install if weights are already at
`/opt/vllm/models/<MODEL>`). NCCL config and the `vllm` user are handled by the
`common` role every run.

### Fail-safe boot (ADR-0009)

Deploying leaves each vLLM unit `enabled`, but a persistent per-engine marker gates
auto-start via `ConditionPathExists`. A **clean reboot** auto-restores serving; a
**hang / hard-reset / power cut** leaves the marker behind, so the next boot skips
the unit and the node comes up **empty and reachable** instead of re-attempting a
risky load unattended. Recovery is just a deploy (it clears the marker) — or the
control panel's per-engine restart. On snoopy the worker unit has
`Restart=on-failure`/`RestartSec=10` and retries until sparky's rendezvous is up;
sparky's head unit allows `TimeoutStartSec=1200` for snoopy to join. Open WebUI has
`restart: always`, so Docker brings it back on boot with no intervention.

### Adding models / profiles

1. Stage weights: `./sparky.sh download <hf-repo>`.
2. Copy an existing profile that matches your shape (`step-3.5-fp8.yml` /
   `minimax-m2.7-awq.yml` for big-shared TP=2; `qwen3-coder-nvfp4-single.yml` for
   single-node on snoopy) to `profiles/<name>.yml` and edit.
3. `./sparky.sh deploy <name>` — Ansible re-templates the units and restarts.

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
├── sparky/                    # the harness: cli, ansible, topology, api, store,
│                              #   quality, multiturn, bench, report
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
    ├── group_vars/ · profiles/ · roles/
    └── …

/opt/cluster/                  # PUBLISHED runtime tree (deploy-owned)
├── ansible/                   #   what ansible runs from (published each deploy)
├── sparky/                    #   the harness the smoke gate's venv runs from
├── current-topology.json      #   the live topology (control panel reads it)
└── benchmark/benchmark.db     #   the SQLite trend store
```

---

## Known shortcomings

- **Open WebUI config is env-authoritative, not UI-authoritative.** The `open-webui`
  role sets `ENABLE_PERSISTENT_CONFIG=false`, so every config env var is re-asserted
  from the profile on **every** deploy — that's what lets a profile switch re-point
  Open WebUI at a new set of engines with no clicks. Trade-off: the **Admin Panel is
  effectively read-only for config** (settings changed there don't survive a deploy).
  To change a setting durably, set its env var in `group_vars/all.yml` or the profile
  (most admin settings are PersistentConfig values with a matching env var). User
  accounts, chats, and uploads are *data*, not config, and are unaffected.
- **`--kv-cache-dtype fp8` + `--enable-prefix-caching` are disabled on `step-3.5-fp8`**
  for stable multi-turn operation (Nth-turn garbage / nonstop thinking on vLLM 0.19).
  Tracked as DEF-0007 ([`docs/defects.md`](docs/defects.md)); re-enabling is governed by
  ADR-0014's optimization register; use `./sparky.sh bench` to quantify the win once
  re-enabled.

---

## Future work

The near-term direction is a **fleet orchestrator**: a single head that autonomously
sweeps the whole fleet — verify every profile (deploy → smoke regression), re-take
benchmarks across every model-hosting profile (deploy → bench → store), and update
models. This builds on `sparky deploy` as a programmable primitive (loop + assert),
plus a scoped non-interactive deploy-context and durable breadcrumbs so a multi-hour
sweep resumes and quarantines a node-killer profile instead of re-freezing. This is now
specified as the **continuous-evaluation outer loop** — human-authorized, agent-driven
sweeps measuring quality vs. performance per tier — in
[ADR-0016](docs/adr/0016-continuous-evaluation-outer-loop.md) (the loop, the CI-style
sweep matrix of profile × variant × regiment, and the eval concepts), with the serving
control model — **`deploy`** (convergent whole-fleet provisioning) + **`activate`** (an
unprivileged selection reconciler), and **no web-API path to root** — in
[ADR-0018](docs/adr/0018-provision-select-split.md). Also: voice mode
(STT/TTS) alongside Open WebUI, and re-enabling the tabled perf options (ADR-0014).

---

## Using with Claude Code

A tracked, thin `CLAUDE.md` imports this README (for full context) and documents the
agent skills in `skills/` — so cloning + opening Claude Code works with no setup.
Skills are **not** auto-registered as slash commands (that needs the vendor-specific
`.claude/skills/` location); instead `CLAUDE.md` tells Claude to read the relevant
`skills/<name>/SKILL.md` on demand. `.claude/` (local settings) is gitignored.

**Geoff runs all git commits** — prepare and stage, but never `git commit` unless
asked.
