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
    assert {"empty", "minimax-m2.7-awq", "step-3.5-fp8"} <= names


def test_empty_profile_has_no_engines():
    p = topology.load_profile("empty")
    assert p.is_empty
    assert p.engines == ()
    assert p.enable_control_panel is True  # infra toggles still parse


def test_big_shared_tp2():
    e = topology.load_profile("minimax-m2.7-awq").engines[0]
    assert e.nodes == ("sparky", "snoopy")
    assert e.is_multinode
    assert e.tensor_parallel_size == 2
    assert e.api_node == "sparky"
    assert e.rank_of("sparky") == 0
    assert e.rank_of("snoopy") == 1
    assert e.unit == "vllm-minimax-m2.7-awq.service"
    assert e.container == "vllm-minimax-m2.7-awq"
    assert e.served_as == "minimax-m2"


def test_dual_is_two_single_node_engines():
    p = topology.load_profile("qwen3-coder-nvfp4-dual")
    assert len(p.engines) == 2
    assert {e.nodes for e in p.engines} == {("sparky",), ("snoopy",)}
    for e in p.engines:
        assert not e.is_multinode
        assert e.tensor_parallel_size == 1
        assert e.api_node == e.nodes[0]


def test_gmu_string_parses_to_float():
    e = topology.load_profile("minimax-m2.7-awq").engines[0]
    assert e.gpu_memory_utilization == 0.75


def test_per_profile_vllm_image_override():
    # dual pins the 26.06 image; the big-shared default leaves it unset (group_vars).
    assert topology.load_profile("qwen3-coder-nvfp4-dual").vllm_image is not None
    assert topology.load_profile("minimax-m2.7-awq").vllm_image is None
