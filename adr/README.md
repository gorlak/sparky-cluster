# Architecture Decision Records

Each significant architectural or operational decision shipped to this cluster gets an ADR.
ADRs are append-only: superseded decisions get a `Superseded by` line; nothing is deleted.

| # | Title | Status | Date |
|---|---|---|---|
| [0001](0001-three-tier-identity.md) | Three-tier identity model (geoff / deploy / vllm) | Implemented | 2026-05-24 |
| [0002](0002-ansible-cluster-management.md) | Ansible for cluster management | Implemented | 2026-05-24 |
| [0003](0003-declarative-profile-system.md) | Declarative YAML profile system | Implemented | 2026-05-24 |
| [0004](0004-nvidia-vllm-container.md) | NVIDIA vLLM container runtime (not pip/venv) | Implemented | 2026-05-24 |
| [0005](0005-native-multinode-no-ray.md) | Native multinode via torch.distributed (no Ray) | Implemented | 2026-05-24 |
| [0006](0006-open-webui-env-authoritative.md) | Open WebUI env-authoritative configuration | Implemented | 2026-05-24 |
| [0007](0007-caddy-reverse-proxy.md) | Caddy as reverse proxy with wildcard DNS | Implemented | 2026-05-24 |
| [0008](0008-control-panel-architecture.md) | Control panel: FastAPI/HTMX on-host as deploy user | Implemented | 2026-05-24 |
| [0009](0009-benchmark-regiment.md) | Benchmark regiment design | Accepted | 2026-07-02 |
| [0010](0010-benchmark-sqlite-storage.md) | SQLite for benchmark trend storage | Accepted | 2026-07-02 |
