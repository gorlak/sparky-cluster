# Sparky Cluster — vLLM + Open WebUI

A two-node NVIDIA DGX Spark (GB10) cluster serving LLMs with vLLM + Open WebUI,
managed by Ansible. One node is the head (`sparky`), the other a worker
(`snoopy`); profiles decide what serves where. The project takes its name from
the head node — see the Peanuts theme below.

## Node naming — a Peanuts theme

**Sparky** was the lifelong nickname of
[Charles M. Schulz](https://en.wikipedia.org/wiki/Charles_M._Schulz), the creator
of *Peanuts* — so the head node bears his name (and, fittingly, `sparky` is always
the "head" 🥁). Every **worker** node is therefore named after a *Peanuts*
character, starting with `snoopy`.

If the cluster ever scales past two nodes, take the next worker name from this
fixed roster, in order, so hostnames stay deterministic:

`snoopy`, `woodstock`, `charlie`, `linus`, `lucy`, `schroeder`, `sally`

**Invariant:** `sparky` is always the head; workers are always *Peanuts* characters.

---

## Current state

The cluster is managed by Ansible and switches between **profiles** —
declarative YAML configs under `ansible/profiles/` describing what serves
where. Today's profile family:

Profile names are the `<model>-<version>-<quant>` triple; a `-single`/`-dual`
topology suffix marks the per-node (TP=1) shapes, while the bare (suffixless)
big-shared profiles are TP=2 across both nodes.

| Profile | Shape |
|---|---|
| `step-3.5-fp8` | Step-3.5-Flash-FP8 TP=2 across both nodes (fully-committed big-shared) — stable |
| `step-3.7-nvfp4` | Step-3.7-Flash-NVFP4 TP=2 on 26.06 — successor candidate, **⛔ blocked** (upstream vLLM VL bug; hidden from deploy UI) |
| `minimax-m2.7-awq` | MiniMax-M2.7-AWQ TP=2 across both nodes (big-shared, ~30 GiB/node dev headroom) |
| `minimax-m2.7-nvfp4` | MiniMax-M2.7-NVFP4 TP=2 on 26.06 — NVFP4 A/B candidate vs the AWQ profile |
| `qwen3-coder-nvfp4-dual` | Qwen3-Coder-Next (NVFP4) single-node on each node (~55 GiB/node dev headroom) |
| `qwen3-coder-nvfp4-single` | Qwen3-Coder-Next (NVFP4) on snoopy only (sparky free for dev) |
| `qwen3.6-35b-nvfp4-dual` | Qwen3.6-35B-A3B (NVFP4) single-node on each node — reasoning-generalist A/B vs coder |
| `qwen3.6-35b-nvfp4-single` | Qwen3.6-35B-A3B (NVFP4) on snoopy only (sparky free for dev) |
| `empty` | nothing serving; full hardware available |

Apply with `make deploy PROFILE=<name>` from `ansible/`. See
[`docs/profiles.md`](docs/profiles.md) for what each one serves and how to
switch; [`docs/profile-tuning.md`](docs/profile-tuning.md) for the *why* —
picking `gpu_memory_utilization` as a deliberate split between vLLM and
system/dev memory, plus the GB10 unified-memory accounting quirk.

For the **live** state of the cluster (which profile is deployed, which
engines are up):

- `http://sparky.flummoxed.net/admin` — control panel (status + per-engine actions)
- `/opt/cluster/current-topology.json` — written at the end of every deploy
- `make status` — systemd state of vLLM units on both nodes

**Repo layout:** this project lives on sparky (the control node) wherever you cloned it. The source of truth is this git repo's `ansible/`
dir; `make deploy` publishes it to `/opt/cluster/ansible` (the runtime copy the
`deploy` user reads) and applies it. The old per-step shell scripts + root
Makefile are preserved in git history (the initial "archive of prior scripts"
commit), not in the working tree.

### Services

**Always running** (independent of which profile is deployed):

| Service | Node | Role |
|---|---|---|
| `caddy` (Docker) | sparky | reverse proxy on `:80`; routes by hostname/path |
| `open-webui` (Docker) | sparky | chat UI on `:8080`, fronted at `chat.{web_domain}` |
| `control-panel.service` (systemd, `User=deploy`) | sparky | FastAPI status + actions on `127.0.0.1:8088`, fronted at `/admin` |
| `prometheus` (Docker) | sparky | metrics TSDB on `:9090` |
| `grafana` (Docker) | sparky | dashboards on `:3000`, fronted at `metrics.{web_domain}` |
| `node-exporter` + `nvidia-gpu-exporter` (Docker) | both | system + GPU metrics on `:9100` / `:9835` |

**Per-profile** (dynamic — the active `serving_topology` decides which exist):

| Unit name pattern | Role |
|---|---|
| `vllm-<engine>.service` (systemd) | one per vLLM engine the active profile declares |
| `ollama-<engine>.service` (systemd) | one per Ollama engine (role exists; no profile uses it today) |

### Web access

Caddy fronts `:80` and routes by hostname/path:
- `http://sparky.flummoxed.net/` — landing page (links to services)
- `http://sparky.flummoxed.net/admin` — cluster control panel (status + actions)
- `http://chat.sparky.flummoxed.net/` — Open WebUI (login required)
- `http://metrics.sparky.flummoxed.net/` — Grafana dashboards (anonymous view)

Needs a **wildcard DNS record** `*.sparky.flummoxed.net → sparky's IP` (one
record; every future service is then just a new route in the Caddyfile, no DNS
change). Open WebUI runs at the root of its own hostname because it doesn't
support being served under a sub-path. The landing page is templated from
`landing_services` in `group_vars/all.yml` — add a service there to add a link.

Open WebUI has **auth enabled**. The admin account is created by the first
sign-up; open sign-up is then closed, so the admin adds users in **Admin Panel →
Users**. Auth knobs are the `webui_*` vars in `group_vars/all.yml`. (Note: a
from-scratch install must briefly re-enable sign-up to create that first admin —
see the comment on `webui_enable_signup`.)

### Pending investigation

`--kv-cache-dtype fp8` and `--enable-prefix-caching` were disabled on `step-3.5-fp8` to
achieve stable multi-turn operation. The failure mode was: Nth inference in a
conversation would produce garbage/nonstop thinking tokens. Hypothesis: FP8 KV
cache + prefix caching interact badly in vLLM 0.19 on this model.

Re-enable one at a time and run multi-turn conversations to narrow it down:
1. `--kv-cache-dtype fp8` alone
2. `--enable-prefix-caching` alone (BF16 KV cache)
3. Both together

---

## System Inventory

**sparky** (head node):
- NVIDIA GB10 (Blackwell), compute capability 12.1 (sm_121)
- 128 GiB unified memory (121 GiB usable)
- CUDA 13.0, Driver 580.159.03, Ubuntu 24.04.4 LTS
- Python 3.12.3, Docker 29.2.1
- 3.7 TB NVMe, 3.5 TB free
- ConnectX-7: 10.0.200.12/24 on enp1s0f1np1 (static, via netplan)
- 10GbE LAN: 192.168.100.2

**snoopy** (worker node):
- Identical GB10 setup, same driver/CUDA
- ConnectX-7: 10.0.200.13/24 on enp1s0f1np1
- 10GbE LAN: 192.168.100.3

**Interconnect:**
- ConnectX-7 at 200 Gbit on enp1s0f1np1 — all NCCL/TP traffic
- RoCE active (rocep1s0f1) — NCCL uses RDMA over this interface
- The second ConnectX-7 pair exists but is not used (NVIDIA guidance)
- 10GbE LAN (192.168.100.x) for management only

**SSH:** passwordless between sparky and snoopy via `/home/geoff/.ssh/id_ed25519_shared`
(geoff's key). Passwordless sudo on both nodes for geoff is limited to:
`systemctl`, `docker`, `journalctl`, `install` (everything else needs a password).

---

## Identity Model (who runs what)

Three-tier separation of concerns:

- **`geoff`** — human admin. Normal password-gated sudoer. Edits config, reviews,
  triggers deploys. Never holds passwordless root.
- **`deploy`** — automation identity. `NOPASSWD: ALL` on both nodes, owns the
  published runtime copy at `/opt/cluster/ansible` and its own SSH key
  (`/home/deploy/.ssh/id_ed25519`, authorized for `deploy@snoopy`). Ansible runs
  **as deploy**. geoff enters this context via `sudo -u deploy …` (his password is
  the gate); a future dashboard runs as a systemd service with `User=deploy` and
  needs no password.
- **`vllm`** — service account that owns the model weights (uid 996, no home, no
  shell). Unchanged from before.
- **`cluster`** group — shared group (geoff + deploy) owning `/opt/cluster`
  (mode 2775 + default ACLs `g:cluster:rwx`) so both can edit the project in place.

`deploy` was created once by `ansible/bootstrap-deploy.sh` (the only step that
can't be Ansible — it creates the user Ansible runs as). Re-runnable if needed.

Ansible itself is apt's **`ansible-core`** (in `/usr/bin`, so it's on sudo's
`secure_path` and tracks the system) — only on sparky, the control node. Ansible
is agentless: snoopy needs only Python + SSH, no Ansible install.

---

## Runtime: NVIDIA vLLM Container

All CUDA-linked code runs inside `nvcr.io/nvidia/vllm:26.04-py3`. The pypi
aarch64 torch wheel is CPU-only — a pip/venv install cannot serve models on
this hardware. NVIDIA's image ships matching torch + vLLM compiled for sm_121.

**Why 26.04:** SM12.1 CUTLASS kernels were broken in 26.03 (fixed in vLLM PR
#38126). 26.03 causes ~40 CUDA traps during warmup on GB10. And **do not move to
26.06 yet** — it ships NCCL 2.30.5, whose NVLS regression hard-hangs dual GB10 at
multinode bring-up. The 26.06 upgrade (needed for NVFP4) is an in-progress,
upstream-blocked migration tracked in
[`docs/upgrades/container-nvidia-vllm-26.06-py3.md`](docs/upgrades/container-nvidia-vllm-26.06-py3.md).

**Multi-node:** vLLM 0.19 dropped Ray entirely. Native multinode uses
`--nnodes / --node-rank / --master-addr / --headless` with torch.distributed
directly. Both nodes rendezvous at `10.0.200.12:29500` over ConnectX-7.

**Update path:** bump `vllm_image` in `ansible/group_vars/all.yml`, `sudo docker
pull <newtag>` on both nodes (digests must match), then `make deploy` — the unit
files change, so both services restart onto the new image.

---

## Project Layout

The Ansible project lives in this git repo at `ansible/` — the **source of truth**
(commit it, push it, clone it to rebuild/share). `make deploy` publishes it to
`/opt/cluster/ansible`, a generated runtime copy the `deploy` user reads (deploy
can't read your `0750` home). You edit in the repo; the live copy only changes when
you deploy. Publishing needs the `cluster` group (log in once after bootstrap).

```
<repo>/                          # git repo (source of truth)
├── README.md                  # canonical project docs (vendor-neutral)
├── CLAUDE.md                  # thin Claude Code entry point — imports README + skills
├── .gitignore                 # excludes .claude/, caches
├── Makefile                   # `make download`; delegates everything else to ansible/
├── scripts/                   # repo-root helpers
│   └── download.py            # `make download` — stage a HF model via uv (self-provisions hf)
├── skills/                    # agent skills (model-discovery, model-evaluation, documentation, development)
├── docs/                      # all project documentation
│   ├── adr/                   # Architecture Decision Records (one per shipped decision)
│   ├── models/                # per-model status notes (memory fit, serve flags)
│   ├── upgrades/              # versioned upgrade trackers, prefixed by kind:
│   │                          #   container-nvidia-vllm-26.06-py3, profile-step-3.7-flash
│   ├── profiles.md            # profile catalog + switching
│   ├── profile-tuning.md      # gmu math, workflow archetypes
│   ├── serving-topology.md    # profile schema
│   └── control-interface.md   # control-panel design
├── ansible/                   # THE Ansible project (git-tracked)
│   ├── ansible.cfg            # runs as deploy; become via sudo (NOPASSWD)
│   ├── inventory.yml          # sparky (head, local) + snoopy (worker, ssh)
│   ├── bootstrap-deploy.sh    # one-time: create deploy user + /opt/cluster
│   ├── Makefile               # publish (repo->/opt/cluster) + ansible-playbook
│   ├── site.yml               # deploy a profile (common→worker→head→webui)
│   ├── teardown.yml           # stop vLLM (head then worker); webui via --tags
│   ├── group_vars/            # all.yml (constants) + head.yml / worker.yml
│   ├── profiles/          # one file per profile: step-3.5, step-3.7, minimax, qwen*, empty
│   │   └── step-3.5.yml   # e.g. model, TP=2, serve flags, webui toggles
│   └── roles/
│       ├── common/            # vllm user, /opt/vllm dirs, NCCL conf (files/)
│       ├── model/             # ingest inbox→canonical (head), mirror to all nodes
│       ├── vllm/              # ONE template -> both unit files; restart-on-change
│       ├── open-webui/        # compose template + `docker compose up -d`
│       └── caddy/             # reverse proxy on :80 — landing page + service routes
└── benchmark/                 # vllm bench serve wrapper + compare tool
                               # (prior scripts live in git history, not the tree)

/opt/cluster/ansible/          # PUBLISHED runtime copy (deploy-owned; `make deploy`
                               # rsyncs the repo here, then runs from here)
```

---

## Cluster Operations

Run from the repo's `ansible/` directory. `make deploy`/`check`/`teardown` first
**publish** your edits (rsync repo → `/opt/cluster/ansible`), then run
`ansible-playbook` there as `deploy` (via `sudo -u deploy` — prompts for your
password; it's the gate into the automation context). `PROFILE` defaults to
`step-3.5-fp8`. Publishing needs the `cluster` group (log in once after bootstrap).

| Target | What it does |
|---|---|
| `make deploy` | Bring the cluster to a profile's state (`PROFILE=step-3.5-fp8`). Restarts services only if a unit changed. |
| `make check` | Dry run (`--check --diff`) — shows what would change, makes none |
| `make teardown` | Stop + disable vLLM on both nodes (head first, then worker; frees GPU) |
| `make teardown-all` | Also stops Open WebUI |
| `make status` | vLLM service status on both nodes |
| `make ping` | Connectivity + privilege-escalation check on both nodes |
| `make logs-head` | Follow sparky's vllm journal |
| `make logs-worker` | Follow snoopy's vLLM worker journal |

Switch profiles by passing `PROFILE=<name>` (e.g. `make deploy PROFILE=single-node`).
Ansible diffs current vs desired state: it tears down/reconfigures/brings up as needed.

---

## Deploy Sequence (from scratch / disaster recovery)

```bash
# 0. Get the repo (rebuild/new machine): clone it, or it's already here.
git clone <remote> <repo>   # on a fresh control node; clone wherever you like

# 1. (Once per cluster) create the deploy identity + /opt/cluster. Run as geoff
#    on sparky; prompts for sudo on both nodes.
bash <repo>/ansible/bootstrap-deploy.sh
#    Then log out/in (or `newgrp cluster`) to pick up the cluster group.

# 2. Download model weights into the inbox on the control node. `make download`
#    runs scripts/download.py via uv (provisions huggingface_hub itself — no local
#    install needed) and stages a flat copy into the inbox. On deploy the model role
#    moves it into the canonical store (/opt/vllm/models) on the head and rsync-mirrors
#    that store to every node — no manual copy to snoopy.
make download REPO=stepfun-ai/Step-3.5-Flash-FP8

# 3. Deploy a profile — publishes the repo to /opt/cluster, then handles both
#    nodes end to end (common, model install if staged, worker, head + API wait,
#    Open WebUI).
cd <repo>/ansible && make deploy PROFILE=step-3.5
```

The `model` role is idempotent: if the weights are already at
`/opt/vllm/models/<MODEL>` it skips the install. NCCL config and the vllm user
are handled by the `common` role on every run.

---

## NCCL Configuration

`/opt/vllm/nccl-env.conf` on both nodes (must be byte-identical):

```
NCCL_SOCKET_IFNAME=enp1s0f1np1
NCCL_IB_HCA=rocep1s0f1
NCCL_IB_GID_INDEX=3
NCCL_NET_GDR_LEVEL=5
NCCL_IB_DISABLE=0
# NCCL_DEBUG=INFO   # uncomment for debugging
```

Pinned to ConnectX-7 / RoCE. The 10GbE management NIC is not used for NCCL.

Managed by the `common` role from `roles/common/files/nccl-env.conf` (copied
verbatim to both nodes — guaranteed byte-identical). Edit it there, not on the hosts.

---

## Operational Notes

### `--quantization fp8` must NOT be used with FP8 checkpoints

`Step-3.5-Flash-FP8` has `quantization_config: {quant_method: fp8}` in its
`config.json`. vLLM auto-detects this. Passing `--quantization fp8` on the
command line causes double-quantization (re-quantizing already-FP8 weights),
which produces garbage/multilingual nonsense output. Never add this flag for
a model checkpoint that already declares its quantization in config.json.

### `VLLM_HOST_IP` must match the ConnectX-7 IP on each node

The default route goes out the 10GbE management NIC. Without `VLLM_HOST_IP`
set, vLLM advertises the wrong IP and torch.distributed rendezvous fails.

- sparky: `VLLM_HOST_IP=10.0.200.12`
- snoopy: `VLLM_HOST_IP=10.0.200.13`

### Unit naming

Every vLLM engine runs under one unit name, `vllm-<engine>.service`, **identical
on every node it spans** (e.g. the `minimax-m2.7-awq` profile's engine is
`vllm-minimax-m2.7-awq.service` on both sparky and snoopy). Head vs. worker is *not* in
the name — `rank` is computed from the node's position in the engine's
`nodes` list, so the same file renders as `head (rank 0, API on :port)` on
`nodes[0]` and `headless worker (rank N)` elsewhere. (The pre-profile scheme used
separate `vllm.service` / `vllm-worker.service` names; that's gone — see
ADR-0003.)

### Boot order doesn't matter (within a bring-up)

On snoopy the worker `vllm-<engine>.service` has `Restart=on-failure` with
`RestartSec=10` and retries until sparky's rendezvous is reachable; sparky's head
unit has `TimeoutStartSec=1200` — 20 minutes for snoopy to join. **Across a
reboot**, boot is fail-safe rather than automatic: a clean reboot auto-restores
serving, but a hang / hard-reset leaves a per-engine marker that makes the unit
skip auto-start (`ConditionPathExists`), so the node comes up empty and reachable
instead of re-attempting the risky load unattended — see ADR-0009.

### Open WebUI restarts automatically

`restart: always` in the compose file means Docker brings it back on boot.
No manual intervention needed after a reboot.

---

## Troubleshooting

- **Garbage/multilingual output**: `--quantization fp8` flag present on an
  already-quantized checkpoint. Remove it from the vllm serve command.
- **Nonstop thinking tokens after Nth turn**: FP8 KV cache (`--kv-cache-dtype fp8`)
  and/or prefix caching (`--enable-prefix-caching`) issue. Disable one or both.
- **NVML init fails inside container** ("Failed to initialize NVML: Unknown Error"):
  missing `--cgroupns=host`, or `systemctl daemon-reload` was run while the
  container was up. Restart the container.
- **NCCL errors**: uncomment `NCCL_DEBUG=INFO` in `roles/common/files/nccl-env.conf`,
  `make deploy`, check `make logs-head`.
- **API never comes up**: `make logs-head` and `make logs-worker` — look for
  OOM, CUDA errors, or rendezvous timeout.
- **Model not selected in Open WebUI**: click the model dropdown and select
  `step-3.5-flash`. It doesn't auto-select on first load.

---

## Adding New Models / Profiles

Profiles live at `ansible/profiles/<name>.yml` — each captures the full
`serving_topology` (engines, models, nodes, ports, `gmu`, `max_model_len`) plus
front-end toggles. The catalog of current profiles is in
**[`docs/profiles.md`](docs/profiles.md)**; the schema is in
[`docs/serving-topology.md`](docs/serving-topology.md). To stage a new model:

1. Stage weights into the inbox on sparky: `make download REPO=<hf-repo>` (runs
   `scripts/download.py` via uv — self-provisions `huggingface_hub`, no local install).
   The deploy moves them into the canonical store and mirrors to all nodes.
2. Copy an existing profile that matches your shape (`step-3.5.yml` / `minimax.yml`
   for big-shared TP; `qwen-dual.yml` / `qwen.yml` for per-node small) to
   `profiles/<name>.yml` and edit.
3. `make deploy PROFILE=<name>` — Ansible re-templates the units and restarts.

The `model` role moves inbox weights into the canonical `/opt/vllm/models` on the
head (chowned to `vllm`), then mirrors that store to every node (rsync,
`--size-only`, no `--delete`) — so any node can serve any model locally.

Memory budgeting is **per-profile**: `gpu_memory_utilization` is a deliberate
split between vLLM and what's left for OS + user work, not a safety margin. See
**[`docs/profile-tuning.md`](docs/profile-tuning.md)** for the math, the
GB10 unified-memory accounting quirk, workflow archetypes (fully-committed /
big-shared-with-headroom / small-and-dev-friendly / bare), and concrete
per-model tunings.

**Do not use:** `Qwen3.5-122B-A10B-FP8` — froze sparky during load.

---

## Known Shortcomings

- **Open WebUI config is env-authoritative, not UI-authoritative.** The
  `open-webui` role sets `ENABLE_PERSISTENT_CONFIG=false` so the connection list
  (and every other config env var) is re-asserted from the profile on **every**
  deploy — that's what lets a profile switch re-point Open WebUI at a new set of
  engines with no manual clicks. The trade-off: the **Admin Panel is effectively
  read-only for config** — settings changed there don't survive the next deploy
  (the container is recreated and config resets to env). To change a setting
  durably, set its Open WebUI environment variable in `group_vars/all.yml` (or the
  profile) instead — most admin-panel settings *are* PersistentConfig values with
  a matching env var, so config-as-code covers them. The residual gap is any admin
  setting with **no** corresponding env var: it can't be set declaratively and
  won't persist if changed in the UI. (User accounts, chats, and uploaded files
  are *data*, not config — they live in the data volume and are unaffected.)

---

## Future Work

1. Re-enable `--kv-cache-dtype fp8` and `--enable-prefix-caching` once the
   multi-turn corruption root cause is identified (see Pending above). Use the
   `benchmark/` suite to quantify the win.
2. More profiles (single-node, alternate models) now that the Ansible profile
   system is in place.
3. Voice mode: faster-whisper (STT) + Kokoro/Piper (TTS) as an Ansible role +
   compose service alongside Open WebUI.
4. Dashboard (cluster control + metrics). Groundwork is now in place: the landing
   page at `sparky.flummoxed.net` is its first iteration, and Caddy + the
   `User=deploy` model are ready. Remaining: a `User=deploy` backend that invokes
   Ansible via `ansible-runner` and scrapes vLLM `/metrics` + node/GPU exporters,
   growing out of (and eventually replacing) the static landing page.
   **Design:** [`docs/control-interface.md`](docs/control-interface.md).

---

## Historical Notes

Scripts from earlier phases are preserved in **git history** (not the working
tree). They were committed once as "Initial commit: archive of prior scripts
used", then removed when the Ansible setup landed. Browse or recover them with
`git log --all -- archive/` then `git show <commit>:archive/<path>`:
- **Per-step shell scripts + root Makefile** (pre-Ansible): the cluster was
  deployed via `install-step*.sh` driven by a root `Makefile`. Migrated to
  Ansible 2026-05-24; the templated equivalents live in `ansible/roles/`.
- **Ray-based multinode** (pre-vLLM 0.19): Ray was removed from vLLM 0.19;
  native multinode replaced it. Ray unit files + helper scripts are in that
  history for reference.
- **pip/venv install**: failed because pypi aarch64 torch is CPU-only; its
  cleanup/setup scripts are likewise in history.

---

## Using with Claude Code

A tracked, thin `CLAUDE.md` is the Claude Code entry point: it imports this README
(for full context) and documents the skills in `skills/`. So cloning + opening
Claude Code works with no setup — no symlinks, nothing to wire up.

`.claude/` (local settings) is gitignored. Note the skills in `skills/` are NOT
auto-registered as slash commands (that would require the vendor-specific
`.claude/skills/` location); instead `CLAUDE.md` tells Claude to read the relevant
`skills/<name>/SKILL.md` on demand.
