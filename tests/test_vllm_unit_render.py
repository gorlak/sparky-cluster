"""Render tests for the vLLM serving surface (ADR-0011 Layer 2, ADR-0018).

Since ADR-0018 that surface is two files: ONE systemd template unit
(`vllm@.service.j2`) whose logic never varies, and one env file per engine per node
(`engine.env.j2`) carrying everything that does. These render both over sample
`serving_topology` inputs and assert on the output — no hardware.

Covers the logic that has bitten us most: rank / head-vs-worker computation, the two
`ConditionPathExists` boot gates, multinode vs single-node arg assembly, the
engine-spec-hash reconnect coupling that keeps head + workers restarting as a matched
pair, and the round-trip of serve flags through a single-quoted env value that
systemd re-splits on whitespace.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path

import jinja2
import yaml

from sparky.fleet import load_fleet
from sparky.topology import PROFILES_DIR

ROLE = Path(__file__).resolve().parent.parent / "ansible/roles/vllm/templates"
UNIT_SRC = (ROLE / "vllm@.service.j2").read_text()
ENV_SRC = (ROLE / "engine.env.j2").read_text()

# The group_vars values Ansible supplies — arbitrary but well-formed.
_VARS = dict(
    ansible_managed="Ansible managed: do not edit",
    vllm_state_dir="/opt/vllm/state",
    vllm_engines_dir="/opt/vllm/engines",
    vllm_active_dir="/opt/vllm/active",
    nccl_conf_path="/opt/vllm/nccl-env.conf",
    vllm_image="nvcr.io/nvidia/vllm:26.04-py3",
    vllm_models_dir="/opt/vllm/models",
    vllm_shm_size="16g",
    vllm_container_memory="110g",
    vllm_systemd_memory_max="115g",
    vllm_restart_sec=20,
    vllm_start_timeout_sec=1200,
    stable_model_name="sparky",
)


def _env() -> jinja2.Environment:
    env = jinja2.Environment(keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=False)
    # Ansible-provided filters the templates rely on (plain Jinja2 lacks them).
    env.filters["to_json"] = lambda o, **kw: json.dumps(o, **kw)
    env.filters["hash"] = lambda s, algo="sha1": hashlib.new(algo, str(s).encode()).hexdigest()
    return env


def render_unit() -> str:
    """The one template unit — identical on every node, for every engine."""
    return _env().from_string(UNIT_SRC).render(**_VARS)


def render_env(engine: dict, node: str) -> str:
    """The env file for `engine` as seen on `node` (its inventory_hostname)."""
    hostvars = {n: {"vllm_host_ip": f"10.0.200.{12 + i}"} for i, n in enumerate(engine["nodes"])}
    return _env().from_string(ENV_SRC).render(
        engine=engine,
        inventory_hostname=node,
        hostvars=hostvars,
        vllm_host_ip=hostvars[node]["vllm_host_ip"],
        **_VARS,
    )


def env_vars(text: str) -> dict[str, str]:
    """Parse a rendered env file the way systemd (and the reconciler) do."""
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
        _profile="test-profile",
        _image="nvcr.io/nvidia/vllm:26.04-py3",
        _blocked=False,
    )
    base.update(overrides)
    return base


# --- the template unit: one file, all the logic -----------------------------

def test_unit_carries_both_boot_gates():  # ADR-0018 extends ADR-0009
    out = render_unit()
    # desired (reconciler-written, per node) AND cleanly-stopped-last-time
    assert "ConditionPathExists=/opt/vllm/active/%i" in out
    assert "ConditionPathExists=!/opt/vllm/state/vllm-%i.running" in out


def test_unit_manages_the_failsafe_marker_lifecycle():  # ADR-0009
    out = render_unit()
    assert "ExecStartPre=/usr/bin/touch /opt/vllm/state/vllm-%i.running" in out
    assert "ExecStopPost=/usr/bin/rm -f /opt/vllm/state/vllm-%i.running" in out
    assert "StartLimitIntervalSec=900" in out
    assert "StartLimitBurst=5" in out


def test_unit_reads_the_engine_env_file():
    out = render_unit()
    assert "EnvironmentFile=/opt/vllm/engines/%i.env" in out
    assert "EnvironmentFile=/opt/vllm/nccl-env.conf" in out


def test_unit_splits_serve_args_but_not_other_values():
    """`$VLLM_SERVE_ARGS` must stay UNBRACED — that is what makes systemd split it
    into argv words. Everything else must be braced, or a value with a space would
    silently become two arguments."""
    out = render_unit()
    assert "vllm serve /models/${ENGINE_MODEL} $VLLM_SERVE_ARGS" in out
    assert "${VLLM_IMAGE}" in out
    assert "$VLLM_IMAGE " not in out.replace("${VLLM_IMAGE}", "")


def test_unit_is_engine_agnostic():
    """No engine, model, profile or node name may appear in the template unit —
    everything variable lives in the env files."""
    out = render_unit()
    for leaked in ("step-3.5", "minimax", "sparky.flummoxed", "Step-3.5-Flash"):
        assert leaked not in out


def test_unit_enables_boot_without_the_reconciler():
    out = render_unit()
    assert "WantedBy=multi-user.target" in out
    assert "vllm-activate" not in out  # boot must never depend on the reconciler


# --- the env file: everything that varies -----------------------------------

def test_head_is_rank0_with_api():
    v = env_vars(render_env(engine(), "sparky"))  # nodes[0]
    assert v["ENGINE_NODE_RANK"] == "0"
    assert v["ENGINE_API_NODE"] == "sparky"
    args = v["VLLM_SERVE_ARGS"]
    assert "--host 0.0.0.0" in args
    assert "--node-rank 0" in args
    assert "--headless" not in args


def test_worker_is_headless_at_higher_rank():
    v = env_vars(render_env(engine(), "snoopy"))  # nodes[1]
    assert v["ENGINE_NODE_RANK"] == "1"
    args = v["VLLM_SERVE_ARGS"]
    assert "--headless" in args
    assert "--node-rank 1" in args
    assert "--served-model-name" not in args  # a worker exposes no API
    assert "--host 0.0.0.0" not in args


def test_api_advertises_both_the_stable_and_the_real_name():
    """Chat targets the stable name so conversations survive an activation;
    bench/eval target the real one."""
    v = env_vars(render_env(engine(), "sparky"))
    assert "--served-model-name sparky test" in v["VLLM_SERVE_ARGS"]
    assert v["ENGINE_STABLE_NAME"] == "sparky"
    assert v["ENGINE_SERVED_AS"] == "test"


def test_multinode_rendezvous_args():
    args = env_vars(render_env(engine(), "sparky"))["VLLM_SERVE_ARGS"]
    assert "--nnodes 2" in args
    assert "--master-addr 10.0.200.12" in args  # nodes[0]'s vllm_host_ip


def test_single_node_omits_multinode_args():
    v = env_vars(render_env(engine(nodes=["snoopy"], tensor_parallel_size=1), "snoopy"))
    args = v["VLLM_SERVE_ARGS"]
    assert v["ENGINE_NODE_RANK"] == "0"  # the lone node is rank 0 / API
    assert "--tensor-parallel-size 1" in args
    assert "--nnodes" not in args
    assert "--master-addr" not in args
    assert "--headless" not in args


def test_env_carries_the_reconciler_identity_fields():
    v = env_vars(render_env(engine(), "snoopy"))
    # The reconciler parses these directly to decide what to activate, so a worker
    # can re-validate a request without asking the head anything.
    assert v["ENGINE_PROFILE"] == "test-profile"
    assert v["ENGINE_NODES"].split() == ["sparky", "snoopy"]
    assert v["ENGINE_MODEL"] == "Test-Model"
    assert v["ENGINE_PORT"] == "8000"
    assert v["VLLM_HOST_IP"] == "10.0.200.13"


def test_per_profile_image_override_lands_in_the_env_file():
    v = env_vars(render_env(engine(_image="dgx-spark/vllm:26.06-fastapi-fix"), "sparky"))
    assert v["VLLM_IMAGE"] == "dgx-spark/vllm:26.06-fastapi-fix"


# --- the matched-pair restart coupling --------------------------------------

def test_worker_env_changes_when_head_args_change():  # ADR-0011 / ADR-0012 coupling
    """A head-side change must move the WORKER's env file too. The reconciler
    restarts a unit when its env-file hash no longer matches the marker, so this is
    what keeps head + workers restarting as a matched pair — a worker that didn't
    restart holds a stale TCPStore connection and never reconnects."""
    base = render_env(engine(), "snoopy")
    changed = render_env(engine(head_extra_args=["--enable-chunked-prefill", "--x"]), "snoopy")
    assert base != changed
    assert env_vars(base)["ENGINE_SPEC_HASH"] != env_vars(changed)["ENGINE_SPEC_HASH"]


def test_image_bump_moves_the_spec_hash():
    a = env_vars(render_env(engine(), "snoopy"))["ENGINE_SPEC_HASH"]
    b = env_vars(render_env(engine(_image="other:tag"), "snoopy"))["ENGINE_SPEC_HASH"]
    assert a != b


def test_head_env_unchanged_when_only_worker_args_change():
    base = render_env(engine(), "sparky")
    changed = render_env(engine(worker_extra_args=["--enable-chunked-prefill", "--x"]), "sparky")
    # The head's ARGS must not depend on worker_extra_args — but the shared spec hash
    # does, by design, so the pair still restarts together.
    assert env_vars(base)["VLLM_SERVE_ARGS"] == env_vars(changed)["VLLM_SERVE_ARGS"]


# --- the real fleet ---------------------------------------------------------

def _as_dict(profile, e) -> dict:
    return dict(
        name=e.name, kind=e.kind, nodes=list(e.nodes), port=e.port, model=e.model,
        served_as=e.served_as, tensor_parallel_size=e.tensor_parallel_size,
        gpu_memory_utilization=str(e.gpu_memory_utilization),
        max_model_len=e.max_model_len,
        head_extra_args=list(e.head_extra_args), worker_extra_args=list(e.worker_extra_args),
        _profile=profile.name, _image=profile.vllm_image or _VARS["vllm_image"],
        _blocked=profile.blocked,
    )


def test_every_real_profile_engine_renders_on_every_node():
    for pl in load_fleet().placements:
        for node in pl.engine.nodes:
            v = env_vars(render_env(_as_dict(pl.profile, pl.engine), node))
            assert v["ENGINE_MODEL"] == pl.engine.model
            assert v["ENGINE_PROFILE"] == pl.profile.name
            assert v["VLLM_SERVE_ARGS"]


def to_argv(env_value: str) -> list[str]:
    """Model the SECOND stage of the round trip: systemd expanding an unbraced `$VAR`
    in ExecStart. That split is POSIX-style — it performs quote REMOVAL, not just
    whitespace splitting — so `shlex.split(posix=True)` reproduces it, including the
    `\"` unescaping. Verified against a live systemd user unit.

    The first stage (the EnvironmentFile parse) is `env_vars()` above, and it passes
    quotes through untouched. Getting these two backwards is what shipped
    `--speculative-config {"method":"mtp"}` to vLLM as `{method:mtp}`.
    """
    return shlex.split(env_value, posix=True)


def test_serve_args_survive_the_full_round_trip_to_argv():
    """End to end: profile flags -> env file -> systemd unquoting -> argv. Every flag
    a profile wrote must come back byte-identical, quotes and all."""
    for pl in load_fleet().placements:
        for node in pl.engine.nodes:
            raw = render_env(_as_dict(pl.profile, pl.engine), node)
            argv = to_argv(env_vars(raw)["VLLM_SERVE_ARGS"])
            expected = (pl.engine.head_extra_args if node == pl.engine.api_node
                        else pl.engine.worker_extra_args)
            # a profile flag may itself be several words (`--tool-call-parser qwen3_xml`)
            for flag in expected:
                words = flag.split()
                assert any(argv[i:i + len(words)] == words for i in range(len(argv))), (
                    f"{pl.engine.name} on {node}: {flag!r} did not survive to argv.\n"
                    f"  argv = {argv}")


def test_json_flags_reach_argv_as_valid_json():
    """The regression that took the cluster down: a JSON argument must still parse as
    JSON after systemd has had its way with the quotes."""
    for pl in load_fleet().placements:
        for node in pl.engine.nodes:
            argv = to_argv(env_vars(render_env(_as_dict(pl.profile, pl.engine), node))["VLLM_SERVE_ARGS"])
            for word in argv:
                if word.startswith("{"):
                    json.loads(word)  # raises if the quotes were eaten


def test_a_json_flag_is_escaped_in_the_env_file():
    """Directly: the rendered file must contain \" — the bare form is silently eaten."""
    spec = '--speculative-config {"method":"mtp","num_speculative_tokens":3}'
    raw = render_env(engine(nodes=["snoopy"], tensor_parallel_size=1,
                            head_extra_args=[spec]), "snoopy")
    line = next(ln for ln in raw.splitlines() if ln.startswith("VLLM_SERVE_ARGS="))
    assert '\\"method\\"' in line, f"double quotes not escaped in the env file: {line}"
    assert to_argv(env_vars(raw)["VLLM_SERVE_ARGS"])[-1] == \
        '{"method":"mtp","num_speculative_tokens":3}'


def test_profiles_still_parse_as_yaml():
    """A drift guard: the fleet loader is the only reader, so a profile that stops
    being well-formed YAML fails here rather than mid-deploy."""
    for path in sorted(PROFILES_DIR.glob("*.yml")):
        assert isinstance(yaml.safe_load(path.read_text()), dict)


def test_no_profile_needs_shell_quoting():
    """The one authoring constraint ADR-0018 adds, checked against the real fleet."""
    for pl in load_fleet().placements:
        for arg in tuple(pl.engine.head_extra_args) + tuple(pl.engine.worker_extra_args):
            assert shlex.quote(arg).replace("'", "") or True  # no crash on odd input
            assert "'" not in arg and "\n" not in arg
