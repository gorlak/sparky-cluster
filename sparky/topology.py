"""Typed reader of the cluster's serving topology (ADR-0010 shared substrate).

A profile (`ansible/profiles/<name>.yml`) declares the serving topology once as
structured data; every model-dependent service is a projection of it (see
`docs/serving-topology.md`). This module is the harness's typed view of that
declaration — the base both the test regiment (ADR-0011) and the benchmark
regiment (ADR-0012) read to know which engines exist, where they serve, and under
what unit/served name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES_DIR = REPO_ROOT / "ansible" / "profiles"
# Written at the end of every deploy (runtime, not in the repo tree).
CURRENT_TOPOLOGY = Path("/opt/cluster/current-topology.json")


@dataclass(frozen=True)
class Engine:
    """One vLLM engine as declared in a profile's `serving_topology`."""

    name: str
    kind: str
    nodes: tuple[str, ...]
    port: int
    model: str
    served_as: str
    tensor_parallel_size: int
    gpu_memory_utilization: float
    max_model_len: int
    head_extra_args: tuple[str, ...] = ()
    worker_extra_args: tuple[str, ...] = ()

    @property
    def api_node(self) -> str:
        """`nodes[0]` is rank 0 — the node that hosts the API (ADR-0003)."""
        return self.nodes[0]

    @property
    def is_multinode(self) -> bool:
        return len(self.nodes) > 1

    @property
    def unit(self) -> str:
        """systemd unit — one name, identical on every node it spans (ADR-0003)."""
        return f"vllm-{self.name}.service"

    @property
    def container(self) -> str:
        return f"vllm-{self.name}"

    def rank_of(self, node: str) -> int:
        """torch.distributed rank = the node's index in `nodes`."""
        return self.nodes.index(node)


@dataclass(frozen=True)
class Profile:
    """A profile: its serving topology plus the front-end toggles."""

    name: str
    engines: tuple[Engine, ...]
    vllm_image: str | None = None
    enable_open_webui: bool = False
    enable_reverse_proxy: bool = False
    enable_control_panel: bool = False
    enable_metrics: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.engines

    def engine(self, name: str) -> Engine:
        for e in self.engines:
            if e.name == name:
                return e
        raise KeyError(f"no engine {name!r} in profile {self.name!r}")


def _engine_from_dict(d: dict) -> Engine:
    nodes = tuple(d["nodes"])
    return Engine(
        name=d["name"],
        kind=d.get("kind", "vllm"),
        nodes=nodes,
        port=int(d["port"]),
        model=d["model"],
        served_as=d["served_as"],
        tensor_parallel_size=int(d.get("tensor_parallel_size", len(nodes))),
        gpu_memory_utilization=float(d["gpu_memory_utilization"]),
        max_model_len=int(d["max_model_len"]),
        head_extra_args=tuple(d.get("head_extra_args") or ()),
        worker_extra_args=tuple(d.get("worker_extra_args") or ()),
    )


def load_profile(name_or_path: str | Path) -> Profile:
    """Load `<name>` from `ansible/profiles/`, or a direct path to a profile YAML.

    Profile names carry version dots (`minimax-m2.7-awq`), so a bare name is only
    treated as a path when it has a `.yml`/`.yaml` suffix or already exists.
    """
    path = Path(name_or_path)
    if path.suffix not in (".yml", ".yaml") and not path.exists():
        path = PROFILES_DIR / f"{name_or_path}.yml"
    data = yaml.safe_load(path.read_text()) or {}
    engines = tuple(_engine_from_dict(e) for e in (data.get("serving_topology") or []))
    return Profile(
        name=data.get("profile_name", path.stem),
        engines=engines,
        vllm_image=data.get("vllm_image"),
        enable_open_webui=bool(data.get("enable_open_webui", False)),
        enable_reverse_proxy=bool(data.get("enable_reverse_proxy", False)),
        enable_control_panel=bool(data.get("enable_control_panel", False)),
        enable_metrics=bool(data.get("enable_metrics", False)),
    )


def all_profiles() -> list[Profile]:
    """Every profile committed under `ansible/profiles/`, name-sorted."""
    return [load_profile(p) for p in sorted(PROFILES_DIR.glob("*.yml"))]


def load_current_topology(path: Path = CURRENT_TOPOLOGY) -> dict | None:
    """The live deployed topology (written each deploy), or None if absent."""
    if not path.exists():
        return None
    return json.loads(path.read_text())
