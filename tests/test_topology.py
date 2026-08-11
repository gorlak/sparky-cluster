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
    assert {"empty", "minimax-m2.7-nvfp4", "qwen3-vl-235b-a22b-instruct-nvfp4"} <= names


def test_empty_profile_has_no_engines():
    p = topology.load_profile("empty")
    assert p.is_empty
    assert p.engines == ()
    assert p.blocked is False


def test_blocked_profile_parses_as_parked():
    # `blocked: true` is the park gesture (ADR-0018): weights kept, not activatable.
    assert topology.load_profile("step-3.7-flash-nvfp4").blocked is True


def test_big_shared_tp2():
    """The fleet's only shape since 2026-08-10: one model sharded across both nodes.

    Bound to the ARCHETYPE, not to a model — the previous version named
    `minimax-m2.7-nvfp4` and asserted `served_as == "minimax-m2"`, which broke the moment
    profiles were renamed to their canonical HF names. Nothing it was actually testing
    (rank assignment, unit derivation) had anything to do with MiniMax.
    """
    prof = topology.one_of("big-shared")
    e = prof.engines[0]
    assert e.nodes == ("sparky", "snoopy")
    assert e.is_multinode
    assert e.tensor_parallel_size == 2
    assert e.api_node == "sparky"
    assert e.rank_of("sparky") == 0
    assert e.rank_of("snoopy") == 1
    # ONE template unit, instanced per engine (ADR-0018) — same name on every node.
    # Asserted as a DERIVATION from the engine name rather than a literal, so a rename
    # cannot break it while the mechanism is intact.
    assert e.unit == f"vllm@{e.name}.service"
    assert e.container == f"vllm-{e.name}"
    assert e.env_file == f"/opt/vllm/engines/{e.name}.env"
    assert e.active_marker == f"/opt/vllm/active/{e.name}"


def test_one_name_everywhere():
    """profile == engine == served_as == the lowercased canonical HF model name.

    Four names for one thing is what made `q36-mtp3` and `baseline` unrecognisable in the
    scoreboard months later. Collapsing them means a row on the scoreboard, a systemd
    unit, an API model string and a Hub page are all obviously the same model.
    """
    for prof in topology.all_profiles():
        if not prof.engines:
            continue
        assert len(prof.engines) == 1, f"{prof.name}: one-name only holds for one engine"
        e = prof.engines[0]
        assert e.name == prof.name, f"{prof.name}: engine is {e.name}"
        assert e.served_as == prof.name, f"{prof.name}: served_as is {e.served_as}"


def test_the_hf_repo_is_recorded_and_matches_the_profile_name():
    """The profile name is the model name lowercased, but the ORG is not recoverable from
    it — `Qwen3-Coder-Next-NVFP4` is RedHatAI's, not Qwen's. `hf_repo` is what makes a
    scoreboard row paste-able into huggingface.co."""
    for prof in topology.all_profiles():
        if not prof.engines or prof.blocked:
            continue
        assert prof.hf_repo, f"{prof.name}: no hf_repo"
        org, _, name = prof.hf_repo.partition("/")
        assert org and name, f"{prof.name}: hf_repo {prof.hf_repo!r} is not org/Name"
        assert name.lower() == prof.name, (
            f"{prof.name}: name disagrees with hf_repo {prof.hf_repo!r} "
            f"(expected profile name {name.lower()!r})")


def test_single_node_profile_runs_on_snoopy():
    """Single-node profiles serve on snoopy by design — sparky is the head (frontends)
    and the dev node, so the resource-richer worker takes the model.

    NO `-single` profile is live any more — the last one was archived on 2026-08-10.
    This loads the RETIRED config by path, because the projection must still be
    correct for the day someone revives one: fleet occupancy is a standing reason to
    want a node free, and a shape that silently stopped rendering would be found the
    hard way. The active twins were deleted on
    2026-08-10 once TP=2 measured faster on decode, throughput AND KV capacity for every
    model tried — see docs/profile-tuning.md. The shape must still render correctly: the
    parked profile is the re-test path, and fleet occupancy is still a real reason to
    reach for TP=1."""
    p = topology.one_of("single-node", include_retired=True)
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
    # The override exists so a container bump is adopted MODEL BY MODEL, which is what
    # made the 26.07 campaign survivable when one model turned out to be a node-killer.
    #
    # As of 2026-08-10 the fleet is single-container: `step-3.5-flash-fp8` was the last profile
    # falling through to the group_vars default (26.04) and it was retired — outclassed on
    # every measured axis (docs/models/tombstones.md). So there is no un-pinned profile
    # left to assert against. The mechanism is still load-bearing and still tested; what
    # changed is that every profile now exercises the pinned branch.
    for prof in topology.by_archetype("big-shared"):
        assert prof.vllm_image is not None, prof.name
    unpinned = [p.name for p in topology.all_profiles()
                if p.engines and p.vllm_image is None]
    assert unpinned == [], f"expected a single-container fleet, found {unpinned}"


def test_retired_profiles_are_not_in_the_allowlist():
    """`ansible/profiles/retired/` keeps the CONFIG of models we stopped running — the
    memory math, the parser names read from chat templates, the quant findings — because
    deleting the .yml threw that away into git history where nobody looks.

    It must never be loadable. Both loaders glob `profiles/*.yml` NON-recursively (here,
    and `fileglob` in the fleet role), so the directory is invisible by construction. This
    test exists because the day that changes silently is the day a retired model reappears
    in the allowlist and a deploy installs its weights again."""
    retired_dir = topology.PROFILES_DIR / "retired"
    if not retired_dir.is_dir():
        return
    archived = {p.stem for p in retired_dir.glob("*.yml")}
    assert archived, "retired/ exists but is empty — did an archive get lost?"
    live = {p.name for p in topology.all_profiles()}
    assert not (archived & live), f"retired profiles leaked into the allowlist: {archived & live}"


def test_every_retired_profile_says_it_is_retired():
    """A config with no banner is indistinguishable from a live one when copied. Someone
    reviving a profile must be told, in the file itself, that its parser names and quant
    assumptions are as old as its retirement date."""
    retired_dir = topology.PROFILES_DIR / "retired"
    if not retired_dir.is_dir():
        return
    for path in retired_dir.glob("*.yml"):
        assert "RETIRED" in path.read_text()[:600], f"{path.name} has no retirement banner"



def test_every_archetype_in_the_vocabulary_has_an_example():
    """A vocabulary entry with no profile is a trap: `one_of` raises, but only if some
    test asks for it. This asserts the whole vocabulary stays real, so an archetype that
    quietly lost its last profile is reported here rather than by whichever test happens
    to reference it next.

    `include_retired` counts, deliberately — `single-node` has no live example since
    2026-08-10, and the shape is still worth keeping renderable."""
    for name in topology.ARCHETYPES:
        found = topology.by_archetype(name, include_retired=True)
        assert found, f"archetype {name!r} has no example anywhere: {topology.ARCHETYPES[name]}"


def test_an_unknown_archetype_is_rejected_rather_than_matching_nothing():
    """Returning [] for a typo would make a test pass while checking nothing."""
    import pytest
    with pytest.raises(KeyError):
        topology.by_archetype("big-shard")          # plausible typo


def test_retired_configs_follow_the_same_naming_rule():
    """The archive is where a revival starts, so it must not preserve a naming scheme we
    have abandoned — copying one back would reintroduce the drift the canonical names fixed.

    Same rule as live profiles: the name is the lowercased HF model name, plus an optional
    `-flavour` suffix (`-single`, `-mtp3-single`) for a second way of serving the same
    weights. Archetypes are required too, since `by_archetype(include_retired=True)` is
    exactly how a shape with no live example is still tested."""
    retired = topology.PROFILES_DIR / "retired"
    if not retired.is_dir():
        return
    for path in sorted(retired.glob("*.yml")):
        prof = topology.load_profile(path)
        assert prof.archetypes, f"{prof.name}: no archetypes"
        assert prof.hf_repo, f"{prof.name}: no hf_repo — the org is not recoverable later"
        model_name = prof.hf_repo.partition("/")[2].lower()
        assert prof.name == model_name or prof.name.startswith(model_name + "-"), (
            f"{prof.name}: expected {model_name!r} or {model_name!r} + a -flavour suffix")
        assert path.stem == prof.name, f"{path.name}: filename disagrees with profile_name"


def test_omitted_nodes_port_and_tp_default_to_the_whole_fleet():
    """A profile states only what makes it DIFFERENT.

    All eight serving profiles were identical on these three fields — 24 lines of
    ceremony that also put hostnames in eight files with no business knowing them, so
    adding a third node meant editing every profile."""
    prof = topology.one_of("big-shared")
    e = prof.engines[0]
    assert e.nodes == topology.fleet_nodes()
    assert e.port == topology.API_PORT
    assert e.tensor_parallel_size == len(topology.fleet_nodes())


def test_node_order_is_head_first_because_rank_zero_is_the_api_host():
    """Not cosmetic: `nodes[0]` is rank 0, the API host and the NCCL master (ADR-0003)."""
    nodes = topology.fleet_nodes()
    assert nodes[0] == "sparky", f"head must be first, got {nodes}"
    assert "snoopy" in nodes


def test_defaults_never_override_what_a_profile_states():
    """THE TRAP in this change: the retired single-node configs say `nodes: [snoopy]` and
    `tensor_parallel_size: 1`. If the default won, an archived TP=1 config would silently
    become TP=2 across both nodes — a stored config that no longer means what it says."""
    single = topology.one_of("single-node", include_retired=True)
    e = single.engines[0]
    assert e.nodes == ("snoopy",), f"retired single-node became {e.nodes}"
    assert e.tensor_parallel_size == 1
    assert e.api_node == "snoopy"


def test_the_default_node_list_comes_from_the_inventory_not_a_constant():
    """Both readers — this parser and the fleet role's Jinja — derive the default from
    `ansible/inventory.yml`, so they cannot disagree about what an omitted `nodes` means.
    A hardcoded tuple here would be a second source of truth for hostnames."""
    import yaml as _yaml
    data = _yaml.safe_load(topology.INVENTORY.read_text())
    children = data["all"]["children"]
    expected = tuple(list(children["head"]["hosts"]) +
                     [h for g, s in children.items() if g != "head" for h in s["hosts"]])
    assert topology.fleet_nodes() == expected


def test_the_harness_finds_the_profiles_when_it_is_not_in_the_repo():
    """The harness is now INSTALLED as well as published (ADR-0021), so `__file__` can sit
    in a venv where `../ansible/profiles` is nothing. Everything read from the ansible tree
    has to move together — a fallback that fixed only PROFILES_DIR would leave the
    inventory and group_vars resolving into a directory that does not exist, and the first
    symptom would be a sweep that cannot tell which node an engine runs on.
    """
    import inspect

    from sparky import fleet, topology

    source = inspect.getsource(topology)
    assert "/opt/cluster/ansible/profiles" in source, "no published-tree fallback"
    assert topology.ANSIBLE_DIR == topology.PROFILES_DIR.parent
    assert topology.INVENTORY.parent == topology.ANSIBLE_DIR
    assert fleet.GROUP_VARS.parent.parent == topology.ANSIBLE_DIR
