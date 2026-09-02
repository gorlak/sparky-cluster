"""Unit tests for the fleet — the allowlist and what it implies (ADR-0018).

`deploy` is convergent, so these are the checks that decide what gets *installed* and
what gets *deleted*. They run against synthetic profile sets (so eviction can be
exercised without touching the real one) plus the committed profiles as a drift guard.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from sparky.serve.fleet import EMPTY, FleetError, load_fleet
from sparky.foundation.topology import PROFILES_DIR

HEAD = "sparky"


def write(dir_, name, body):
    (dir_ / f"{name}.yml").write_text(textwrap.dedent(body))


def profile_yaml(name, *, model="M", nodes=("sparky", "snoopy"), engine=None,
                 blocked=False, port=8000, args=(), worker_args=()):
    engine = engine or name
    return f"""\
        profile_name: {name}
        {'blocked: true' if blocked else ''}
        serving_topology:
          - name: {engine}
            kind: vllm
            nodes: [{', '.join(nodes)}]
            port: {port}
            model: {model}
            served_as: {engine}
            tensor_parallel_size: {len(nodes)}
            memory_fraction: "0.8"
            context_length: 32768
            head_extra_args: [{', '.join(json.dumps(a) for a in args)}]
            worker_extra_args: [{', '.join(json.dumps(a) for a in worker_args)}]
        """


@pytest.fixture()
def dir_(tmp_path):
    (tmp_path / "empty.yml").write_text("profile_name: empty\nserving_topology: []\n")
    return tmp_path


# --- the allowlist is the profiles directory --------------------------------

def test_a_profile_file_is_an_allowlist_entry(dir_):
    write(dir_, "step", profile_yaml("step"))
    fleet = load_fleet(dir_)
    assert fleet.allowlist == [EMPTY, "step"]


def test_blocked_keeps_the_weights_but_leaves_the_allowlist(dir_):
    """*Block to park it; delete the file to evict it.* A parked candidate — waiting
    on an upstream fix — must keep its weights so re-testing costs no download."""
    write(dir_, "step", profile_yaml("step", model="Step"))
    write(dir_, "next", profile_yaml("next", model="Next", blocked=True))
    fleet = load_fleet(dir_)
    assert fleet.allowlist == [EMPTY, "step"]           # not activatable
    assert "Next" in fleet.models                        # but still kept
    assert fleet.evictions_on(HEAD, ["Step", "Next"], head=HEAD) == []


def test_deleting_the_yml_evicts_its_weights(dir_):
    write(dir_, "step", profile_yaml("step", model="Step"))
    write(dir_, "old", profile_yaml("old", model="Old"))
    assert load_fleet(dir_).evictions_on(HEAD, ["Step", "Old"], head=HEAD) == []
    (dir_ / "old.yml").unlink()
    assert load_fleet(dir_).evictions_on(HEAD, ["Step", "Old"], head=HEAD) == ["Old"]


def test_eviction_ignores_models_that_were_never_there(dir_):
    write(dir_, "step", profile_yaml("step", model="Step"))
    assert load_fleet(dir_).evictions_on(HEAD, [], head=HEAD) == []


# --- per-node placement -----------------------------------------------------

def test_a_worker_holds_only_the_models_it_runs(dir_):
    """Per-node disk tracks what the node actually runs, not the whole fleet."""
    write(dir_, "big", profile_yaml("big", model="Big"))                       # TP=2
    write(dir_, "solo", profile_yaml("solo", model="Solo", nodes=("snoopy",)))
    write(dir_, "head-only", profile_yaml("head-only", model="HeadOnly", nodes=("sparky",)))
    fleet = load_fleet(dir_)
    assert fleet.models_on("snoopy", head=HEAD) == ["Big", "Solo"]
    assert "HeadOnly" not in fleet.models_on("snoopy", head=HEAD)


def test_the_head_holds_everything_because_it_is_the_mirror_source(dir_):
    """The head is the canonical store: `download` stages into its inbox and every
    other node rsyncs from it, so evicting a worker-only model here would leave no
    way to repair that worker's copy short of re-downloading."""
    write(dir_, "solo", profile_yaml("solo", model="Solo", nodes=("snoopy",)))
    fleet = load_fleet(dir_)
    assert fleet.models_on(HEAD, head=HEAD) == ["Solo"]
    assert fleet.evictions_on(HEAD, ["Solo"], head=HEAD) == []


def test_a_worker_evicts_a_model_it_stopped_running(dir_):
    write(dir_, "solo", profile_yaml("solo", model="Solo", nodes=("snoopy",)))
    fleet = load_fleet(dir_)
    # weights left over from when this model was TP=2 across both nodes
    assert fleet.evictions_on("sparky", ["Solo"], head=None) == ["Solo"]


def test_engines_on_a_node(dir_):
    write(dir_, "big", profile_yaml("big", engine="big-e"))
    write(dir_, "solo", profile_yaml("solo", engine="solo-e", nodes=("snoopy",)))
    fleet = load_fleet(dir_)
    assert [e.name for e in fleet.engines_on("sparky")] == ["big-e"]
    assert sorted(e.name for e in fleet.engines_on("snoopy")) == ["big-e", "solo-e"]


# --- the invariants deploy asserts ------------------------------------------

def test_duplicate_engine_names_are_rejected_fleet_wide(dir_):
    """An engine name is its systemd instance AND its env file path, so a collision
    across two profiles would have them silently overwrite each other."""
    write(dir_, "a", profile_yaml("a", engine="shared"))
    write(dir_, "b", profile_yaml("b", engine="shared"))
    with pytest.raises(FleetError, match="unique FLEET-wide"):
        load_fleet(dir_).validate()


def test_an_off_port_engine_is_rejected(dir_):
    write(dir_, "a", profile_yaml("a", port=8001))
    with pytest.raises(FleetError, match="port 8000"):
        load_fleet(dir_).validate()


def test_a_single_quoted_flag_is_rejected(dir_):
    """It would terminate the env file's single-quoted value early."""
    write(dir_, "a", profile_yaml("a", args=["--speculative-config '{\"m\":1}'"]))
    with pytest.raises(FleetError, match="cannot survive the engine env file"):
        load_fleet(dir_).validate()


def test_spaces_and_double_quotes_in_a_flag_are_fine(dir_):
    """Spaces become separate argv words — which is exactly what a `--flag value`
    entry wants — and systemd does no quote processing, so JSON passes through."""
    # `--speculative-config` is placed on BOTH ranks: it configures the model, and since
    # 2026-08-12 lint refuses it head-only (the ranks desync and TP=2 deadlocks).
    # `--tool-call-parser` stays head-only, which is correct: it belongs to the API server.
    write(dir_, "a",
          profile_yaml("a",
                       args=["--tool-call-parser step3p5",
                             '--speculative-config {"method":"mtp"}'],
                       worker_args=['--speculative-config {"method":"mtp"}']))
    load_fleet(dir_).validate()


def test_validate_reports_every_problem_at_once(dir_):
    write(dir_, "a", profile_yaml("a", engine="shared", port=8001))
    write(dir_, "b", profile_yaml("b", engine="shared"))
    with pytest.raises(FleetError) as exc:
        load_fleet(dir_).validate()
    assert "unique FLEET-wide" in str(exc.value) and "port 8000" in str(exc.value)


# --- the committed fleet ----------------------------------------------------

def test_the_real_fleet_is_deployable():
    """Drift guard: a profile that `deploy` would refuse fails here, not on the node."""
    load_fleet().validate()


def test_the_real_allowlist_excludes_parked_profiles():
    fleet = load_fleet()
    parked = [p.name for p in fleet.profiles if p.blocked]
    # Deliberately does NOT name a model. The parenthetical here used to say
    # "(step-3.7-flash-nvfp4)", which stopped being true on 2026-08-11 when DEF-0006
    # cleared and that profile unparked — the assertion still passed, so the message
    # quietly became a lie about the fleet. Which profiles are parked is exactly the sort
    # of thing that churns; that at least one exists is the invariant worth asserting.
    assert parked, "expected at least one parked candidate in the committed profiles"
    for name in parked:
        assert name not in fleet.allowlist
        # …but its weights are still kept, which is the whole point of parking.
        assert set(e.model for e in fleet.profile(name).engines) <= set(fleet.models)


def test_empty_is_in_the_real_allowlist():
    assert EMPTY in load_fleet().allowlist


def test_every_committed_profile_is_loaded():
    assert len(load_fleet().profiles) == len(list(PROFILES_DIR.glob("*.yml")))


# --- ADR-0013: a profile may only select an image the images role guarantees ---

def test_a_profile_selecting_an_unmanaged_image_is_rejected(dir_, monkeypatch):
    """Documented in group_vars, unenforced until now. An unmanaged image deploys
    "successfully" and then fails at ENGINE START — and with digest pins copied into
    profiles by hand, one wrong character does it."""
    from sparky.serve import fleet as fleet_mod
    monkeypatch.setattr(fleet_mod, "managed_images",
                        lambda *a, **k: {"nvcr.io/nvidia/vllm@sha256:good"})
    write(dir_, "typo", profile_yaml("typo") +
          "image: nvcr.io/nvidia/vllm@sha256:DEADBEEF\n")
    with pytest.raises(FleetError, match="not in container_images"):
        load_fleet(dir_).validate()


def test_a_profile_selecting_a_managed_image_passes(dir_, monkeypatch):
    from sparky.serve import fleet as fleet_mod
    monkeypatch.setattr(fleet_mod, "managed_images",
                        lambda *a, **k: {"nvcr.io/nvidia/vllm@sha256:good"})
    write(dir_, "ok", profile_yaml("ok") +
          "image: nvcr.io/nvidia/vllm@sha256:good\n")
    load_fleet(dir_).validate()


def test_the_real_fleets_images_are_all_managed():
    """Drift guard against the live group_vars — catches a hand-typed digest."""
    from sparky.serve.fleet import managed_images
    managed = managed_images()
    assert managed, "container_images did not parse"
    for p in load_fleet().profiles:
        if p.image:
            assert p.image in managed, f"{p.name}: {p.image}"


# --- flags that must reach every rank (2026-08-12) ----------------------------

def test_a_model_flag_on_one_rank_only_is_refused(dir_):
    """Why this is a lint failure rather than a comment.

    `--speculative-config` was written into `head_extra_args` alone. Every rank builds its
    own VllmConfig from its own argv, so rank 0 ran EAGLE's extra draft profiling pass and
    rank 1 did not — the ranks disagreed about how many collectives to run and TP=2
    deadlocked at startup. Rank 1 blocked ~12 minutes in
    `_dummy_sampler_run -> tensor_model_parallel_all_gather` then died with
    `DistBackendError … possible application crash on rank 0`; rank 0 looped
    `No available shared memory broadcast block found` indefinitely. An hour of stalled
    cluster, and nothing in either log named the flag.

    It was the THIRD head-only-flag desync in two days (`--tokenizer-mode`, then
    `--config-format` without `--load-format`, then this), which is what turned a
    convention that was written in a profile comment into something enforced.
    """
    write(dir_, "a", profile_yaml("a", args=['--speculative-config {"method":"eagle"}']))
    with pytest.raises(FleetError, match="not the worker"):
        load_fleet(dir_).validate()


def test_the_mirrored_desync_is_refused_too(dir_):
    """Worker-only is the same bug seen from the other side, and just as deadly."""
    write(dir_, "a", profile_yaml("a", worker_args=["--tokenizer-mode mistral"]))
    with pytest.raises(FleetError, match="not the head"):
        load_fleet(dir_).validate()


def test_api_surface_flags_stay_head_only():
    """The converse, and why this is a NAMED LIST rather than 'the two lists must match'.

    `--tool-call-parser`, `--reasoning-parser` and `--enable-auto-tool-choice` belong to
    the API server, which runs on the head alone. Every serving profile has them head-only;
    a blanket equality rule would fail all of them.
    """
    from sparky.serve import fleet
    for flag in ("--tool-call-parser", "--reasoning-parser", "--enable-auto-tool-choice"):
        assert flag not in fleet.BOTH_RANK_FLAGS, f"{flag} is head-only by design"
