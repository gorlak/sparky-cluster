"""Unit tests for the topology loader (ADR-0010 harness substrate).

No hardware: they load the committed profile YAMLs and assert the parsed shape.
`test_all_committed_profiles_load` doubles as a drift guard — a broken profile
schema fails here instead of at deploy time.
"""

import json

from sparky import topology


def test_load_current_topology_reads_given_path(tmp_path):
    f = tmp_path / "topo.json"
    f.write_text(json.dumps({"profile": "p", "engines": [{"name": "e", "api_url": "http://x:8000"}]}))
    t = topology.load_current_topology(f)
    assert t["profile"] == "p"
    assert t["engines"][0]["api_url"] == "http://x:8000"


def test_load_current_topology_absent_returns_none(tmp_path):
    assert topology.load_current_topology(tmp_path / "nope.json") is None


def test_all_committed_profiles_load():
    profiles = topology.all_profiles()
    names = {p.name for p in profiles}
    assert {"empty", "minimax-m2.7-nvfp4", "step-3.5-fp8"} <= names


def test_empty_profile_has_no_engines():
    p = topology.load_profile("empty")
    assert p.is_empty
    assert p.engines == ()
    assert p.blocked is False


def test_blocked_profile_parses_as_parked():
    # `blocked: true` is the park gesture (ADR-0018): weights kept, not activatable.
    assert topology.load_profile("step-3.7-nvfp4").blocked is True


def test_big_shared_tp2():
    e = topology.load_profile("minimax-m2.7-nvfp4").engines[0]
    assert e.nodes == ("sparky", "snoopy")
    assert e.is_multinode
    assert e.tensor_parallel_size == 2
    assert e.api_node == "sparky"
    assert e.rank_of("sparky") == 0
    assert e.rank_of("snoopy") == 1
    # ONE template unit, instanced per engine (ADR-0018) — same name on every node.
    assert e.unit == "vllm@minimax-m2.7-nvfp4.service"
    assert e.container == "vllm-minimax-m2.7-nvfp4"
    assert e.env_file == "/opt/vllm/engines/minimax-m2.7-nvfp4.env"
    assert e.active_marker == "/opt/vllm/active/minimax-m2.7-nvfp4"
    assert e.served_as == "minimax-m2"


def test_single_node_profile_runs_on_snoopy():
    # Single-node profiles serve on snoopy by design — sparky is the head (frontends)
    # and the dev node. The per-node "-dual" duplicate shape was retired (no value
    # without a round-robin fronting the two endpoints). See docs/profiles.md.
    p = topology.load_profile("qwen3-coder-nvfp4-single")
    assert len(p.engines) == 1
    e = p.engines[0]
    assert e.nodes == ("snoopy",)
    assert not e.is_multinode
    assert e.tensor_parallel_size == 1
    assert e.api_node == "snoopy"


def test_gmu_string_parses_to_float():
    e = topology.load_profile("minimax-m2.7-nvfp4").engines[0]
    assert e.gpu_memory_utilization == 0.80


def test_per_profile_vllm_image_override():
    # Most profiles now pin 26.07 per-profile; `step-3.5-fp8` deliberately does not,
    # so it still falls through to the group_vars default (26.04). That split is the
    # point of the override — a container bump is adopted model by model.
    assert topology.load_profile("qwen3-coder-nvfp4-single").vllm_image is not None
    assert topology.load_profile("minimax-m2.7-nvfp4").vllm_image is not None
    assert topology.load_profile("step-3.5-fp8").vllm_image is None
