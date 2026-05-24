# Archive

Scripts and unit files from earlier phases of this project. Kept for reference only — none of these are part of the active stack.

## What's here

### Script-based deploy (pre-Ansible) — retired 2026-05-24
The cluster was originally deployed with numbered shell scripts driven by a root
`Makefile`. Migrated to Ansible (`/opt/cluster/ansible`); the templated
equivalents now live in `ansible/roles/`. Retired sources:

- `Makefile.legacy` — the old root Makefile (restart/deploy/webui targets, etc.)
- `nodes/sparky/` — `vllm.service`, `open-webui/docker-compose.yml`,
  `vllm/nccl-env.conf`, and `install-step{2,4,5,6}.sh`
- `nodes/snoopy/` — `vllm-worker.service`, `vllm/nccl-env.conf`, `install-step2.sh`

Mapping to the Ansible project: the unit files → `roles/vllm/templates/vllm.service.j2`;
`nccl-env.conf` → `roles/common/files/`; the compose file → `roles/open-webui/`;
the install steps → the `common` / `model` / `vllm` / `open-webui` roles.

### Ray (pre-vLLM 0.19)
Ray was removed from vLLM in 0.19 (`26.04-py3`). The native multi-node path
(`--nnodes/--node-rank/--master-addr`) replaced it entirely. These files
reflect the Ray-based multinode attempt that preceded the current stack:

- `sparky/etc/systemd/system/ray-head.service`
- `snoopy/etc/systemd/system/ray-worker.service`
- `sparky/scripts/install-step3.sh` — ray-head setup
- `sparky/scripts/reinstall-ray-units.sh` — ray unit reinstall helper
- `sparky/scripts/diagnose-ray.sh` — ray cluster diagnostics
- `sparky/scripts/diagnose-gpu.sh` — GPU visibility diagnostics (ray context)
- `sparky/scripts/test-daemon-reload-gremlin.sh` — confirmed daemon-reload revokes GPU cgroup access

### Legacy venv install (pre-container)
The original approach tried to run vLLM from a pip venv. This failed because
the pypi aarch64 torch wheel is CPU-only (`libcudart.so.12: cannot open shared
object file`). These scripts set up and tear down that install:

- `sparky/scripts/install-step1.sh`
- `sparky/scripts/cleanup.sh`
- `snoopy/scripts/install.sh`
- `snoopy/scripts/cleanup.sh`

### Mac-side scripts
When Claude Code ran on the Mac, these synced files to the cluster. Now that
Claude Code runs directly on sparky, the Makefile's `push-snoopy` target
handles snoopy sync and these are obsolete:

- `sync.sh`
- `push-model.sh`
