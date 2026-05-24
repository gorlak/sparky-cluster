# DGX Spark Cluster — vLLM + Open WebUI

---

## Current State (2026-05-24)

**This project lives on sparky (the control node) at `~/Projects/DGX-Spark-Setup/`.**

The cluster is running and serving **`stepfun-ai/Step-3.5-Flash-FP8`** via
vLLM 0.19 native multinode (TP=2, no Ray) across sparky + snoopy. Open WebUI
is up at http://sparky (port 80).

**The cluster is managed by Ansible.** The source of truth is this git repo at
`ansible/`; `make deploy` publishes it to `/opt/cluster/ansible` (the runtime copy
the `deploy` user reads) and applies it. The current deployment is the `step-flash`
profile. Day-to-day ops: `cd ~/Projects/DGX-Spark-Setup/ansible && make <target>`
— see Cluster Operations. The old per-step shell scripts + root Makefile are
preserved in git history (the initial "archive of prior scripts" commit), not in
the working tree.

### Active services

| Service | Node | State |
|---|---|---|
| `vllm.service` | sparky | enabled, running — head (rank 0), API on :8000 |
| `vllm-worker.service` | snoopy | enabled, running — headless worker (rank 1) |
| `caddy` | sparky | Docker Compose, restart: always — reverse proxy on :80 |
| `open-webui` | sparky | Docker Compose, restart: always — internal :8080, behind Caddy |

### Web access

Caddy fronts `:80` and routes by hostname:
- `http://sparky.flummoxed.net/` — landing page (links to services)
- `http://chat.sparky.flummoxed.net/` — Open WebUI (login required)

This needs a **wildcard DNS record** `*.sparky.flummoxed.net → sparky's IP` (one
record; every future service is then just a new route in the Caddyfile, no DNS
change). Open WebUI runs at the root of its own hostname because it doesn't
support being served under a sub-path. The landing page is templated from
`landing_services` in `group_vars/all.yml` — add a service there to add a link.

Open WebUI has **auth enabled**. The admin account is created by the first
sign-up; open sign-up is then closed, so the admin adds users in **Admin Panel →
Users**. Auth knobs are the `webui_*` vars in `group_vars/all.yml`. (Note: a
from-scratch install must briefly re-enable sign-up to create that first admin —
see the comment on `webui_enable_signup`.)

### Known working configuration

- Image: `nvcr.io/nvidia/vllm:26.04-py3` (SM12.1 CUTLASS fix; required for GB10)
- Model: `Step-3.5-Flash-FP8` at `/opt/vllm/models/Step-3.5-Flash-FP8` on both nodes
- TP=2 multinode via `--nnodes 2 / --node-rank 0|1 / --master-addr 10.0.200.12`
- `--gpu-memory-utilization 0.90` — weights are ~97.5 GiB/node, leaves ~11 GiB
- `--max-model-len 32768`
- `VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS=1` — accurate CUDA graph memory accounting

### Pending investigation

`--kv-cache-dtype fp8` and `--enable-prefix-caching` were disabled to achieve
stable multi-turn operation. The failure mode was: Nth inference in a
conversation would produce garbage/nonstop thinking tokens. Hypothesis:
FP8 KV cache + prefix caching interact badly in vLLM 0.19 on this model.

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
#38126). 26.03 causes ~40 CUDA traps during warmup on GB10.

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
DGX-Spark-Setup/                # git repo (source of truth)
├── README.md                  # canonical project docs (vendor-neutral)
├── CLAUDE.md                  # thin Claude Code entry point — imports README + skills
├── .gitignore                 # excludes model-cache/, .claude/, caches
├── skills/                    # agent skills (model-scout, model-evaluation)
├── ansible/                   # THE Ansible project (git-tracked)
│   ├── ansible.cfg            # runs as deploy; become via sudo (NOPASSWD)
│   ├── inventory.yml          # sparky (head, local) + snoopy (worker, ssh)
│   ├── bootstrap-deploy.sh    # one-time: create deploy user + /opt/cluster
│   ├── Makefile               # publish (repo->/opt/cluster) + ansible-playbook
│   ├── site.yml               # deploy a profile (common→worker→head→webui)
│   ├── teardown.yml           # stop vLLM (head then worker); webui via --tags
│   ├── group_vars/            # all.yml (constants) + head.yml / worker.yml
│   ├── profiles/
│   │   └── step-flash.yml     # CURRENT config: model, TP=2, serve flags, webui
│   └── roles/
│       ├── common/            # vllm user, /opt/vllm dirs, NCCL conf (files/)
│       ├── model/             # idempotent install from staging (skips if present)
│       ├── vllm/              # ONE template -> both unit files; restart-on-change
│       ├── open-webui/        # compose template + `docker compose up -d`
│       └── caddy/             # reverse proxy on :80 — landing page + service routes
├── benchmark/                 # vllm bench serve wrapper + compare tool
├── models/                    # model status notes
└── model-cache/               # model weights staging (GITIGNORED — 100s of GB)
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
`step-flash`. Publishing needs the `cluster` group (log in once after bootstrap).

| Target | What it does |
|---|---|
| `make deploy` | Bring the cluster to a profile's state (`PROFILE=step-flash`). Restarts services only if a unit changed. |
| `make check` | Dry run (`--check --diff`) — shows what would change, makes none |
| `make teardown` | Stop + disable vLLM on both nodes (head first, then worker; frees GPU) |
| `make teardown-all` | Also stops Open WebUI |
| `make status` | vLLM service status on both nodes |
| `make ping` | Connectivity + privilege-escalation check on both nodes |
| `make logs-head` | Follow sparky's vllm journal |
| `make logs-worker` | Follow snoopy's vllm-worker journal |

Switch profiles by passing `PROFILE=<name>` (e.g. `make deploy PROFILE=single-node`).
Ansible diffs current vs desired state: it tears down/reconfigures/brings up as needed.

---

## Deploy Sequence (from scratch / disaster recovery)

```bash
# 0. Get the repo (rebuild/new machine): clone it, or it's already here.
git clone <remote> ~/Projects/DGX-Spark-Setup   # on a fresh control node

# 1. (Once per cluster) create the deploy identity + /opt/cluster. Run as geoff
#    on sparky; prompts for sudo on both nodes.
bash ~/Projects/DGX-Spark-Setup/ansible/bootstrap-deploy.sh
#    Then log out/in (or `newgrp cluster`) to pick up the cluster group.

# 2. Stage model weights where the model role can find them.
#    Control node staging: /opt/cluster/model-cache/<MODEL>
hf download stepfun-ai/Step-3.5-Flash-FP8 \
    --local-dir /opt/cluster/model-cache/Step-3.5-Flash-FP8
#    (Worker still needs the weights at /opt/vllm/models too — cross-node
#     distribution isn't automated yet; rsync to snoopy if doing a fresh build.)

# 3. Deploy a profile — publishes the repo to /opt/cluster, then handles both
#    nodes end to end (common, model install if staged, worker, head + API wait,
#    Open WebUI).
cd ~/Projects/DGX-Spark-Setup/ansible && make deploy PROFILE=step-flash
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

### Boot order doesn't matter

`vllm-worker.service` on snoopy has `Restart=on-failure` with `RestartSec=10`
and retries until sparky's rendezvous is reachable. sparky's `vllm.service`
has `TimeoutStartSec=1200` — 20 minutes for snoopy to join.

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

A profile (`profiles/<name>.yml`) captures everything that varies per deployment:
`model_name`, `served_model_name`, `tensor_parallel_size`, `num_nodes`,
`max_model_len`, `gpu_memory_utilization`, the head/worker serve-flag lists, and
`enable_open_webui`. To add a model:

1. Stage weights at `/opt/cluster/model-cache/<MODEL>` on sparky (and ensure
   they reach snoopy's `/opt/vllm/models` too — cross-node sync not yet automated).
2. Copy `profiles/step-flash.yml` to `profiles/<name>.yml` and edit the values.
3. `make deploy PROFILE=<name>` — Ansible re-templates the units and restarts.

The `model` role moves staged weights into `/opt/vllm/models` (idempotent) and
chowns them to `vllm`.

Memory budget per node at TP=2: ~97.5 GiB for weights + ~11 GiB headroom.
A new model must fit within ~108.9 GiB (0.90 × 121 GiB) per shard.

**Do not use:** `Qwen3.5-122B-A10B-FP8` — froze sparky during load.

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
