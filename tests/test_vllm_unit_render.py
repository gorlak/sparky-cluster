"""Render tests for `roles/vllm/templates/vllm.service.j2` (ADR-0011 Layer 2).

Renders the unit template over sample `serving_topology` inputs and asserts on the
output — no hardware. Covers the logic that has bitten us most: rank / head-vs-
worker computation, the ADR-0009 fail-safe marker directives, multinode vs
single-node arg assembly, and the engine-spec-hash reconnect coupling that keeps
head + workers restarting as a matched pair (ADR-0011 / ADR-0012).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jinja2
import yaml

from sparky.topology import PROFILES_DIR

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "ansible/roles/vllm/templates/vllm.service.j2"
)
_TEMPLATE_SRC = TEMPLATE.read_text()

# The group_vars values Ansible supplies — arbitrary but well-formed.
_VARS = dict(
    ansible_managed="Ansible managed: do not edit",
    vllm_state_dir="/opt/vllm/state",
    nccl_conf_path="/opt/vllm/nccl-env.conf",
    vllm_image="nvcr.io/nvidia/vllm:26.04-py3",
    vllm_models_dir="/opt/vllm/models",
    vllm_shm_size="16g",
    vllm_container_memory="110g",
    vllm_systemd_memory_max="115g",
)


def _env() -> jinja2.Environment:
    env = jinja2.Environment(keep_trailing_newline=True)
    # Ansible-provided filters the template relies on (plain Jinja2 lacks them).
    env.filters["to_json"] = lambda o, **kw: json.dumps(o, **kw)
    env.filters["hash"] = lambda s, algo="sha1": hashlib.new(algo, str(s).encode()).hexdigest()
    return env


def render(engine: dict, node: str) -> str:
    """Render the unit for `engine` as seen on `node` (its inventory_hostname)."""
    hostvars = {n: {"vllm_host_ip": f"10.0.200.{12 + i}"} for i, n in enumerate(engine["nodes"])}
    return _env().from_string(_TEMPLATE_SRC).render(
        engine=engine,
        inventory_hostname=node,
        hostvars=hostvars,
        vllm_host_ip=hostvars[node]["vllm_host_ip"],
        **_VARS,
    )


def engine(**overrides) -> dict:
    """A big-shared TP=2 engine spec; override any field for a scenario."""
    base = dict(
        name="ex",
        kind="vllm",
        nodes=["sparky", "snoopy"],
        port=8000,
        model="Test-Model",
        served_as="test",
        tensor_parallel_size=2,
        gpu_memory_utilization="0.8",
        max_model_len=32768,
        head_extra_args=["--enable-chunked-prefill"],
        worker_extra_args=["--enable-chunked-prefill"],
    )
    base.update(overrides)
    return base


def test_fail_safe_marker_directives():  # ADR-0009
    out = render(engine(), "sparky")
    assert "ConditionPathExists=!/opt/vllm/state/vllm-ex.running" in out
    assert "ExecStartPre=/usr/bin/touch /opt/vllm/state/vllm-ex.running" in out
    assert "ExecStopPost=/usr/bin/rm -f /opt/vllm/state/vllm-ex.running" in out
    assert "StartLimitIntervalSec=900" in out
    assert "StartLimitBurst=5" in out


def test_head_is_rank0_with_api():
    out = render(engine(), "sparky")  # nodes[0]
    assert "head (rank 0, API on :8000)" in out
    assert "--host 0.0.0.0" in out
    assert "--served-model-name test" in out
    assert "--node-rank 0" in out
    assert "--headless" not in out
    assert "RestartSec=60" in out  # head backs off slower


def test_worker_is_headless_at_higher_rank():
    out = render(engine(), "snoopy")  # nodes[1]
    assert "headless worker (rank 1)" in out
    assert "--headless" in out
    assert "--node-rank 1" in out
    assert "--served-model-name" not in out  # worker exposes no API
    assert "--host 0.0.0.0" not in out
    assert "RestartSec=10" in out  # worker retries the rendezvous fast


def test_multinode_rendezvous_args():
    out = render(engine(), "sparky")
    assert "--nnodes 2" in out
    assert "--master-addr 10.0.200.12" in out  # nodes[0]'s vllm_host_ip


def test_single_node_omits_multinode_args():
    out = render(engine(nodes=["snoopy"], tensor_parallel_size=1), "snoopy")
    assert "head (rank 0" in out  # the lone node is rank 0 / API
    assert "--tensor-parallel-size 1" in out
    assert "--nnodes" not in out
    assert "--master-addr" not in out
    assert "--headless" not in out


def test_worker_rerenders_when_head_args_change():  # ADR-0011 / ADR-0012 coupling
    base = render(engine(), "snoopy")
    head_changed = render(engine(head_extra_args=["--enable-chunked-prefill", "--x"]), "snoopy")
    # engine-spec-hash embeds the whole spec → a HEAD-side change re-renders the WORKER,
    # so systemd restarts the worker as a matched pair (no stale rendezvous).
    assert base != head_changed


def test_head_unchanged_when_only_worker_args_change():
    base = render(engine(), "sparky")
    worker_changed = render(engine(worker_extra_args=["--enable-chunked-prefill", "--x"]), "sparky")
    # the head unit must not depend on worker_extra_args — no spurious head restart.
    assert base == worker_changed


def test_every_real_profile_engine_renders_on_every_node():
    for pf in sorted(PROFILES_DIR.glob("*.yml")):
        data = yaml.safe_load(pf.read_text()) or {}
        for eng in data.get("serving_topology") or []:
            for node in eng["nodes"]:
                out = render(eng, node)
                assert "ExecStart=/usr/bin/docker run" in out
                assert f"vllm serve /models/{eng['model']}" in out
