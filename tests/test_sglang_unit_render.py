"""Render tests for the SGLang serving surface (ADR-0011 Layer 2, ADR-0030).

The SGLang sibling of test_vllm_unit_render.py. It renders the `sglang@.service.j2` unit
and the `engine.env.j2` projection over sample `serving_topology` inputs and asserts on the
output — no hardware. The point is to prove the second engine kind reproduces the ADR-0009
fail-safe shape (the two boot gates, the marker lifecycle) with kind-scoped names, and that
the serve-args builder speaks SGLang's flag vocabulary while carrying the same
identity/reconnect fields the reconciler reads.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jinja2
import yaml

from sparky.foundation.topology import PROFILES_DIR, SERVING_KINDS

ROLE = Path(__file__).resolve().parent.parent / "ansible/roles/sglang/templates"
UNIT_SRC = (ROLE / "sglang@.service.j2").read_text()
ENV_SRC = (ROLE / "engine.env.j2").read_text()

_VARS = dict(
    ansible_managed="Ansible managed: do not edit",
    vllm_state_dir="/opt/vllm/state",
    vllm_engines_dir="/opt/vllm/engines",
    vllm_active_dir="/opt/vllm/active",
    nccl_conf_path="/opt/vllm/nccl-env.conf",
    vllm_models_dir="/opt/vllm/models",
    vllm_shm_size="32g",
    vllm_container_memory="110g",
    vllm_systemd_memory_max="115g",
    vllm_restart_sec=20,
    vllm_start_timeout_sec=1200,
    stable_model_name="sparky",
    sglang_dist_port=29500,
)


def _env() -> jinja2.Environment:
    env = jinja2.Environment(keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=False)
    env.filters["to_json"] = lambda o, **kw: json.dumps(o, **kw)
    env.filters["hash"] = lambda s, algo="sha1": hashlib.new(algo, str(s).encode()).hexdigest()
    return env


def render_unit() -> str:
    return _env().from_string(UNIT_SRC).render(**_VARS)


def render_env(engine: dict, node: str) -> str:
    hostvars = {n: {"vllm_host_ip": f"10.0.200.{12 + i}"} for i, n in enumerate(engine["nodes"])}
    return _env().from_string(ENV_SRC).render(
        engine=engine,
        inventory_hostname=node,
        hostvars=hostvars,
        vllm_host_ip=hostvars[node]["vllm_host_ip"],
        **_VARS,
    )


def env_vars(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def engine(**overrides) -> dict:
    """A big-shared TP=2 SGLang engine spec; override any field for a scenario."""
    base = dict(
        name="flash",
        kind="sglang",
        nodes=["sparky", "snoopy"],
        port=8000,
        model="Qwen3.8-Flash-Next-NVFP4",
        served_as="qwen3.8-flash-next",
        tensor_parallel_size=2,
        memory_fraction="0.80",
        context_length=262144,
        head_extra_args=["--tool-call-parser qwen3_coder"],
        worker_extra_args=[],
        _profile="qwen3.8-flash-next",
        _image="lmsysorg/sglang:qwen38flashnext",
        _blocked=False,
    )
    base.update(overrides)
    return base


# --- the template unit: the fail-safe shape, kind-scoped --------------------

def test_unit_carries_both_boot_gates_kind_scoped():  # ADR-0009 generalised (ADR-0030)
    out = render_unit()
    assert "ConditionPathExists=/opt/vllm/active/%i" in out          # desired: kind-agnostic
    assert "ConditionPathExists=!/opt/vllm/state/sglang-%i.running" in out  # cleanly-stopped


def test_unit_manages_the_sglang_failsafe_marker_lifecycle():
    out = render_unit()
    assert "ExecStartPre=/usr/bin/touch /opt/vllm/state/sglang-%i.running" in out
    assert "ExecStopPost=/usr/bin/rm -f /opt/vllm/state/sglang-%i.running" in out
    assert "StartLimitIntervalSec=900" in out
    assert "StartLimitBurst=5" in out


def test_unit_drops_caches_before_load():
    """GB10 large-model recipe: a warm host page cache starves the GPU allocator mid-load.
    The drop runs as root in the unit start (the unprivileged activate path cannot), and is
    non-destructive."""
    out = render_unit()
    assert "sync; echo 3 > /proc/sys/vm/drop_caches" in out


def test_unit_launches_sglang_and_splits_serve_args():
    out = render_unit()
    assert "python3 -m sglang.launch_server --model-path /models/${ENGINE_MODEL} $SGLANG_SERVE_ARGS" in out
    assert "${SGLANG_IMAGE}" in out
    # $SGLANG_SERVE_ARGS must stay UNBRACED (systemd splits it into argv words).
    assert "$SGLANG_SERVE_ARGS" in out


def test_unit_names_its_own_container_and_marker():
    """The container and marker are kind-scoped so vllm@ and sglang@ never collide on a
    shared name; a `vllm-` container in an sglang unit would be a silent aliasing bug."""
    out = render_unit()
    assert "--name sglang-%i" in out
    assert "docker rm -f sglang-%i" in out
    assert "vllm-%i" not in out


def test_unit_is_engine_agnostic():
    out = render_unit()
    for leaked in ("qwen3.8", "flash-next", "sparky.flummoxed", "Qwen"):
        assert leaked not in out


def test_unit_boots_without_the_reconciler():
    out = render_unit()
    assert "WantedBy=multi-user.target" in out
    assert "vllm-activate" not in out


# --- the env file: identity the reconciler reads, plus SGLang flags ---------

def test_env_declares_the_sglang_kind():
    v = env_vars(render_env(engine(), "sparky"))
    assert v["ENGINE_KIND"] == "sglang"


def test_head_is_rank0_with_api():
    v = env_vars(render_env(engine(), "sparky"))
    assert v["ENGINE_NODE_RANK"] == "0"
    assert v["ENGINE_API_NODE"] == "sparky"
    args = v["SGLANG_SERVE_ARGS"]
    assert "--host 0.0.0.0" in args
    assert "--node-rank 0" in args


def test_worker_exposes_no_api():
    v = env_vars(render_env(engine(), "snoopy"))
    assert v["ENGINE_NODE_RANK"] == "1"
    args = v["SGLANG_SERVE_ARGS"]
    assert "--node-rank 1" in args
    assert "--served-model-name" not in args   # only rank 0 binds the API
    assert "--host 0.0.0.0" not in args


def test_api_advertises_the_stable_name_only():
    """SGLang's --served-model-name takes ONE value, so an engine advertises the stable
    name (chat + the static Caddy upstream depend on it). The real name is not in the
    serve args, but is still recorded for the reconciler/harness."""
    v = env_vars(render_env(engine(), "sparky"))
    assert "--served-model-name sparky" in v["SGLANG_SERVE_ARGS"]
    assert "qwen3.8-flash-next" not in v["SGLANG_SERVE_ARGS"]  # the real name is not served
    assert v["ENGINE_SERVED_AS"] == "qwen3.8-flash-next"       # …but it is recorded


def test_schema_fields_map_to_sglang_flags():
    args = env_vars(render_env(engine(), "sparky"))["SGLANG_SERVE_ARGS"]
    assert "--tp-size 2" in args                       # tensor_parallel_size
    assert "--context-length 262144" in args           # context_length
    assert "--mem-fraction-static 0.80" in args        # memory_fraction


def test_multinode_rendezvous_over_the_cx7_rail():
    args = env_vars(render_env(engine(), "sparky"))["SGLANG_SERVE_ARGS"]
    assert "--nnodes 2" in args
    assert "--dist-init-addr 10.0.200.12:29500" in args   # nodes[0]'s CX7 IP + dist port


def test_single_node_omits_multinode_args():
    v = env_vars(render_env(engine(nodes=["snoopy"], tensor_parallel_size=1), "snoopy"))
    args = v["SGLANG_SERVE_ARGS"]
    assert v["ENGINE_NODE_RANK"] == "0"
    assert "--tp-size 1" in args
    assert "--nnodes" not in args
    assert "--dist-init-addr" not in args


def test_env_carries_the_reconciler_identity_fields():
    v = env_vars(render_env(engine(), "snoopy"))
    assert v["ENGINE_PROFILE"] == "qwen3.8-flash-next"
    assert v["ENGINE_NODES"].split() == ["sparky", "snoopy"]
    assert v["ENGINE_MODEL"] == "Qwen3.8-Flash-Next-NVFP4"
    assert v["SGLANG_IMAGE"] == "lmsysorg/sglang:qwen38flashnext"


def test_worker_env_changes_when_head_args_change():  # matched-pair restart coupling
    base = render_env(engine(), "snoopy")
    changed = render_env(engine(head_extra_args=["--tool-call-parser qwen3_coder", "--x"]), "snoopy")
    assert env_vars(base)["ENGINE_SPEC_HASH"] != env_vars(changed)["ENGINE_SPEC_HASH"]


def test_image_bump_moves_the_spec_hash():
    a = env_vars(render_env(engine(), "snoopy"))["ENGINE_SPEC_HASH"]
    b = env_vars(render_env(engine(_image="other:tag"), "snoopy"))["ENGINE_SPEC_HASH"]
    assert a != b


# --- the kind vocabulary is defined once ------------------------------------

def test_engine_kinds_group_var_matches_serving_kinds():
    """The fleet role filters on `engine_kinds` (group_vars) and the harness on
    `topology.SERVING_KINDS`; a typo'd kind must be dropped in BOTH readers, so the two
    lists cannot be allowed to drift."""
    gv = yaml.safe_load((Path(__file__).resolve().parent.parent
                         / "ansible" / "group_vars" / "all.yml").read_text())
    assert tuple(gv["engine_kinds"]) == SERVING_KINDS
    assert "sglang" in SERVING_KINDS   # the ADR-0030 addition is actually wired
