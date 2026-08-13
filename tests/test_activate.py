"""Unit tests for the activation reconciler (ADR-0011 Layer 3, ADR-0018).

The reconciler is the one piece of custom code in the safety path, so its decision is
kept pure and tested here without hardware:

    (requested profile x installed env files x markers x live units)
        -> the marker set + the unit start/stop plan

Plus the two things that guard the boundary: the allowlist re-validation that refuses
an undeployed profile, and the four-token grammar the forced-command channel accepts.
"""

from __future__ import annotations

import importlib.util
import os
import json
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parent.parent
          / "ansible/roles/activate/files/vllm-activate")


@pytest.fixture(scope="module")
def rec():
    spec = importlib.util.spec_from_loader(
        "vllm_activate",
        importlib.machinery.SourceFileLoader("vllm_activate", str(SCRIPT)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- env parsing: must match what systemd does with our rendered files ------

def test_parse_env_strips_wrapping_quotes(rec):
    parsed = rec.parse_env(
        "# comment\n"
        "ENGINE_NAME=ex\n"
        "ENGINE_NODES='sparky snoopy'\n"
        'VLLM_SERVE_ARGS=\'--host 0.0.0.0 --speculative-config {"method":"mtp"}\'\n'
        "\n"
    )
    assert parsed["ENGINE_NAME"] == "ex"
    assert parsed["ENGINE_NODES"].split() == ["sparky", "snoopy"]
    # double quotes inside survive — systemd does no quote processing when it splits
    assert '{"method":"mtp"}' in parsed["VLLM_SERVE_ARGS"]


def test_parse_env_ignores_comments_and_blank_lines(rec):
    assert rec.parse_env("\n# x=y\n  \nA=1\n") == {"A": "1"}


# --- the plan ---------------------------------------------------------------

def engines(*specs):
    """{engine: env} for the reconciler, one entry per (name, profile, nodes, hash)."""
    return {
        name: {"ENGINE_PROFILE": profile, "ENGINE_NODES": " ".join(nodes),
               "ENGINE_MODEL": f"Model-{name}", "_hash": digest}
        for name, profile, nodes, digest in specs
    }


BIG = ("big", "step", ["sparky", "snoopy"], "h-big")
SOLO = ("solo", "qwen", ["snoopy"], "h-solo")


def test_activating_a_profile_starts_only_its_engines(rec):
    p = rec.plan("step", engines(BIG, SOLO), "snoopy", markers={}, active={})
    assert p.markers == {"big": "h-big"}
    assert p.start == ["big"]
    assert p.stop == []          # nothing was running
    assert p.drop_markers == []


def test_switching_profiles_stops_the_outgoing_engine(rec):
    p = rec.plan("qwen", engines(BIG, SOLO), "snoopy",
                 markers={"big": "h-big"}, active={"big": True})
    assert p.markers == {"solo": "h-solo"}
    assert p.drop_markers == ["big"]
    assert p.stop == ["big"]
    assert p.start == ["solo"]


def test_a_node_the_engine_does_not_span_gets_nothing(rec):
    """`solo` runs on snoopy only — sparky must plan no marker and no unit for it,
    which is what makes the per-node marker set the honest boot gate."""
    p = rec.plan("qwen", engines(BIG, SOLO), "sparky", markers={}, active={})
    assert p.markers == {}
    assert p.start == []


def test_empty_stops_everything_everywhere(rec):
    p = rec.plan("empty", engines(BIG, SOLO), "snoopy",
                 markers={"solo": "h-solo"}, active={"solo": True})
    assert p.markers == {}
    assert p.drop_markers == ["solo"]
    assert p.stop == ["solo"]
    assert p.start == []


def test_reactivating_the_same_definition_is_a_no_op(rec):
    p = rec.plan("step", engines(BIG), "sparky",
                 markers={"big": "h-big"}, active={"big": True})
    assert p.leave == ["big"]
    assert not p.changes


def test_a_moved_definition_restarts_and_records_the_new_hash(rec):
    """A deploy re-rendered the engine while it was serving; the marker still holds
    the old hash, so the next activate applies the change."""
    p = rec.plan("step", engines(("big", "step", ["sparky"], "h-new")), "sparky",
                 markers={"big": "h-old"}, active={"big": True})
    assert p.restart == ["big"]
    assert p.markers == {"big": "h-new"}
    assert p.pending == []


def test_preserve_leaves_a_moved_engine_running_and_keeps_the_old_hash(rec):
    """`deploy --preserve` must never drop a healthy engine — and must not pretend
    the change landed, or the next activate would see nothing to do."""
    p = rec.plan("step", engines(("big", "step", ["sparky"], "h-new")), "sparky",
                 markers={"big": "h-old"}, active={"big": True}, preserve=True)
    assert p.pending == ["big"]
    assert p.restart == []
    assert p.markers == {"big": "h-old"}


def test_preserve_still_starts_an_engine_that_should_be_serving(rec):
    p = rec.plan("step", engines(BIG), "sparky", markers={}, active={"big": False},
                 preserve=True)
    assert p.start == ["big"]
    assert p.markers == {"big": "h-big"}


def test_force_restarts_even_when_nothing_moved(rec):
    p = rec.plan("step", engines(BIG), "sparky",
                 markers={"big": "h-big"}, active={"big": True}, force=True)
    assert p.restart == ["big"]
    assert p.leave == []


def test_an_orphan_marker_is_dropped_and_its_unit_stopped(rec):
    """`deploy` removed an engine from this node. Its marker must go, or the next
    boot would try to start a unit whose env file no longer exists."""
    p = rec.plan("empty", engines(BIG), "sparky",
                 markers={"gone": "h-gone"}, active={"gone": True})
    assert p.drop_markers == ["gone"]
    assert p.stop == ["gone"]
    assert p.markers == {}


# --- the allowlist: re-validated on every node ------------------------------

def test_empty_is_always_activatable(rec, tmp_path):
    """The fail-safe target can never depend on a file being right."""
    assert rec.read_allowlist(tmp_path / "missing") == ["empty"]


def test_allowlist_skips_comments_and_blanks(rec, tmp_path):
    f = tmp_path / "allowlist"
    f.write_text("# generated\n\nstep\nqwen\nstep\n")
    assert rec.read_allowlist(f) == ["empty", "step", "qwen"]


def test_reconcile_refuses_a_profile_this_node_does_not_have(rec, tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "ENGINES_DIR", tmp_path / "engines")
    monkeypatch.setattr(rec, "ALLOWLIST", tmp_path / "allowlist")
    monkeypatch.setattr(rec, "CONF", tmp_path / "conf")
    (tmp_path / "allowlist").write_text("step\n")
    with pytest.raises(rec.ActivateError) as exc:
        rec.reconcile_node("not-deployed", node="snoopy")
    assert "not in the allowlist" in str(exc.value)


# --- the request: uncertainty means `empty` ---------------------------------

@pytest.mark.parametrize("content", ["", "   \n", "has spaces\n", "../../etc/passwd\n",
                                     "a;rm -rf /\n"])
def test_an_unusable_request_reads_as_empty(rec, tmp_path, monkeypatch, content):
    f = tmp_path / "desired-profile"
    f.write_text(content)
    monkeypatch.setattr(rec, "DESIRED_PROFILE", f)
    assert rec.read_request() == "empty"


def test_a_missing_request_reads_as_empty(rec, tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "DESIRED_PROFILE", tmp_path / "nope")
    assert rec.read_request() == "empty"


def test_a_good_request_reads_back(rec, tmp_path, monkeypatch):
    f = tmp_path / "desired-profile"
    f.write_text("step-3.5-flash-fp8\ntrailing junk\n")
    monkeypatch.setattr(rec, "DESIRED_PROFILE", f)
    assert rec.read_request() == "step-3.5-flash-fp8"


# --- the forced-command grammar ---------------------------------------------

@pytest.mark.parametrize("request_", [
    "status", "stop step-3.5-flash-fp8", "start empty", "all qwen3.6-35b force",
    "start step preserve", "stop step force preserve",
])
def test_accepted_ssh_requests(rec, request_):
    assert rec.parse_ssh_request(request_) == request_.split()


@pytest.mark.parametrize("request_", [
    "", "status extra", "bogus step", "start", "start step; rm -rf /",
    "start step $(id)", "start step force preserve extra", "start ../step",
    "start step|cat", "stop step && reboot",
])
def test_rejected_ssh_requests(rec, request_):
    with pytest.raises(rec.ActivateError):
        rec.parse_ssh_request(request_)


def test_remote_request_tokens_are_validated_before_they_are_sent(rec):
    """Defence on the way out as well as in: a malformed token never reaches ssh."""
    with pytest.raises(rec.ActivateError):
        rec.invoke_remote({"ADDR_snoopy": "10.0.200.13"}, "snoopy", ["start", "a b"])


def test_remote_invocation_needs_a_known_address(rec):
    with pytest.raises(rec.ActivateError):
        rec.invoke_remote({}, "woodstock", ["status"])


# --- marker transactions ----------------------------------------------------

def test_markers_are_written_and_dropped_as_a_set(rec, tmp_path, monkeypatch):
    active = tmp_path / "active"
    monkeypatch.setattr(rec, "ACTIVE_DIR", active)
    rec.write_markers({"a": "h-a", "b": "h-b"}, [])
    assert rec.read_markers(active) == {"a": "h-a", "b": "h-b"}
    rec.write_markers({"a": "h-a2"}, ["b"])
    assert rec.read_markers(active) == {"a": "h-a2"}


def test_a_failed_marker_write_rolls_the_whole_set_back(rec, tmp_path, monkeypatch):
    """Markers are the source of truth, so a half-written set would be worse than no
    change at all: live state would be reconciled against a fiction."""
    active = tmp_path / "active"
    monkeypatch.setattr(rec, "ACTIVE_DIR", active)
    rec.write_markers({"a": "h-a"}, [])

    real_replace = Path.replace

    def explode(self, target):
        if Path(target).name == "b":
            raise OSError("disk full")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", explode)
    with pytest.raises(OSError):
        rec.write_markers({"a": "h-a2", "b": "h-b"}, [])
    monkeypatch.setattr(Path, "replace", real_replace)
    assert rec.read_markers(active) == {"a": "h-a"}


def test_unit_and_container_naming(rec):
    assert rec.unit_of("step-3.5-flash-fp8") == "vllm@step-3.5-flash-fp8.service"


# --- driving one node: marker-first, then systemd ---------------------------

@pytest.fixture()
def node(rec, tmp_path, monkeypatch):
    """A fake node: real env files and markers on disk, systemctl recorded."""
    engines_dir, active, state = (tmp_path / "engines", tmp_path / "active",
                                  tmp_path / "state")
    for d in (engines_dir, active, state):
        d.mkdir()
    (engines_dir / "allowlist").write_text("step\nqwen\n")
    (engines_dir / "big.env").write_text(
        "ENGINE_PROFILE=step\nENGINE_NODES='sparky snoopy'\nENGINE_MODEL=Big\n"
        "ENGINE_API_NODE=sparky\nENGINE_API_ADDR=10.0.200.12\nENGINE_PORT=8000\n"
        "ENGINE_SERVED_AS=big\n")
    (engines_dir / "solo.env").write_text(
        "ENGINE_PROFILE=qwen\nENGINE_NODES='snoopy'\nENGINE_MODEL=Solo\n"
        "ENGINE_API_NODE=snoopy\nENGINE_API_ADDR=10.0.200.13\nENGINE_PORT=8000\n"
        "ENGINE_SERVED_AS=solo\n")
    monkeypatch.setattr(rec, "ENGINES_DIR", engines_dir)
    monkeypatch.setattr(rec, "ALLOWLIST", engines_dir / "allowlist")
    monkeypatch.setattr(rec, "ACTIVE_DIR", active)
    monkeypatch.setattr(rec, "STATE_DIR", state)
    monkeypatch.setattr(rec, "CONF", tmp_path / "conf")
    (tmp_path / "conf").write_text("THIS_NODE=snoopy\nNODES='sparky snoopy'\n")

    calls, live = [], set()

    def fake_systemctl(*args, check=True):
        calls.append(args)
        if args[0] == "start":
            live.add(args[-1])
        if args[0] == "stop":
            live.discard(args[-1])
        return type("P", (), {"returncode": 0, "stdout": "active", "stderr": ""})()

    monkeypatch.setattr(rec, "_systemctl", fake_systemctl)
    monkeypatch.setattr(rec, "is_active", lambda e: rec.unit_of(e) in live)
    # The plan reads the unit's ActiveState, not a bool — a fake that answers "active" for
    # everything (which the generic `fake_systemctl` above would) makes every engine look
    # like it needs stopping.
    monkeypatch.setattr(rec, "unit_state",
                        lambda e: "active" if rec.unit_of(e) in live else "inactive")
    return rec, calls, live, state


def test_reconcile_writes_markers_before_touching_systemd(node):
    rec, calls, live, _ = node
    rec.reconcile_node("qwen", node="snoopy")
    assert rec.read_markers() == {"solo": rec.read_engines()["solo"]["_hash"]}
    assert ("start", "vllm@solo.service") in calls
    assert live == {"vllm@solo.service"}


def test_reconcile_clears_the_failsafe_marker_before_starting(node):
    """Recovery out of an ADR-0009 fail-safe boot is just an activate: without this
    the unit's ConditionPathExists would make `start` a silent no-op."""
    rec, calls, live, state = node
    stale = state / "vllm-solo.running"
    stale.write_text("")
    rec.reconcile_node("qwen", node="snoopy")
    assert not stale.exists()
    assert ("start", "vllm@solo.service") in calls


def test_switching_stops_the_old_engine_and_starts_the_new_one(node):
    rec, calls, live, _ = node
    rec.reconcile_node("step", node="snoopy")
    calls.clear()
    rec.reconcile_node("qwen", node="snoopy")
    assert ("stop", "vllm@big.service") in calls
    assert ("start", "vllm@solo.service") in calls
    assert rec.read_markers() == {"solo": rec.read_engines()["solo"]["_hash"]}


def test_reconciling_to_empty_touches_nothing_but_vllm(node):
    """Reachability is never gated on a model — only vllm@ units are ever touched."""
    rec, calls, live, _ = node
    rec.reconcile_node("step", node="snoopy")
    calls.clear()
    rec.reconcile_node("empty", node="snoopy")
    assert rec.read_markers() == {}
    assert live == set()
    assert all(a[-1].startswith("vllm@") for a in calls if a[0] in ("start", "stop"))


def test_a_redeploy_of_the_same_definition_does_not_restart(node):
    rec, calls, live, _ = node
    rec.reconcile_node("qwen", node="snoopy")
    calls.clear()
    rec.reconcile_node("qwen", node="snoopy")
    assert not [a for a in calls if a[0] in ("start", "stop")]


def test_a_rerendered_engine_restarts_on_the_next_activate(node, rec):
    rec_, calls, live, _ = node
    rec_.reconcile_node("qwen", node="snoopy")
    (rec_.ENGINES_DIR / "solo.env").write_text(
        (rec_.ENGINES_DIR / "solo.env").read_text() + "VLLM_SERVE_ARGS='--new'\n")
    calls.clear()
    rec_.reconcile_node("qwen", node="snoopy")
    assert ("stop", "vllm@solo.service") in calls
    assert ("start", "vllm@solo.service") in calls


def test_preserve_leaves_it_running_and_the_next_activate_still_applies_it(node):
    rec, calls, live, _ = node
    rec.reconcile_node("qwen", node="snoopy")
    (rec.ENGINES_DIR / "solo.env").write_text(
        (rec.ENGINES_DIR / "solo.env").read_text() + "VLLM_SERVE_ARGS='--new'\n")
    calls.clear()
    p = rec.reconcile_node("qwen", node="snoopy", preserve=True)
    assert p.pending == ["solo"]
    assert not [a for a in calls if a[0] in ("start", "stop")]
    # the marker still records the OLD definition, so this is not lost
    p2 = rec.reconcile_node("qwen", node="snoopy")
    assert p2.restart == ["solo"]


def test_orchestrate_refuses_without_its_wiring(rec, tmp_path, monkeypatch):
    """Reconciling only the head would silently leave a worker serving."""
    monkeypatch.setattr(rec, "CONF", tmp_path / "missing")
    with pytest.raises(rec.ActivateError, match="never been deployed"):
        rec.orchestrate("empty")


# --- the recorded topology --------------------------------------------------

def test_the_head_records_a_worker_only_engine_it_does_not_host(node):
    """A node's env files describe only what IT can run, so on sparky a snoopy-only
    engine has no env file at all. The head still has to record it, or `sparky status`,
    smoke and bench would all conclude nothing is serving."""
    rec, _, _, _ = node
    (rec.ENGINES_DIR / "index.json").write_text(json.dumps([
        {"name": "solo", "profile": "qwen", "nodes": ["snoopy"], "api_node": "snoopy",
         "api_addr": "10.0.200.13", "port": "8000", "model": "Solo",
         "served_as": "solo", "stable_name": "sparky"},
    ]))
    (rec.ENGINES_DIR / "solo.env").unlink()  # as it would be on the head
    entries = rec.topology_entries("qwen")
    assert [e["name"] for e in entries] == ["solo"]
    assert entries[0]["api_url"] == "http://10.0.200.13:8000"
    assert entries[0]["unit"] == "vllm@solo.service"
    assert entries[0]["port"] == 8000


def test_topology_falls_back_to_local_env_files_without_an_index(node):
    """Degraded but honest — a missing index must not read as 'nothing is serving'."""
    rec, _, _, _ = node
    entries = rec.topology_entries("qwen")
    assert [e["name"] for e in entries] == ["solo"]
    assert entries[0]["api_url"] == "http://10.0.200.13:8000"


def test_topology_of_empty_is_empty(node):
    rec, _, _, _ = node
    assert rec.topology_entries("empty") == []


# --- fail-safe detection: narrow on purpose ---------------------------------
#
# The ADR-0009 recovery state is the one status that must never be ignored, so it
# must never cry wolf. It reports only when the engine is DESIRED, systemd left it
# INACTIVE, and the unclean-shutdown marker survived to say why.

def _status_of(rec, name):
    return next(e for e in rec.node_status(node="snoopy")["engines"] if e["name"] == name)


def test_deactivating_is_not_failsafe(node, monkeypatch):
    """`ExecStop` is `docker stop --time=120`, so a clean, deliberate stop sits in
    `deactivating` for up to two minutes with the marker still armed — ExecStopPost
    clears it only once the process is gone. That window used to render as
    '⚠ Fail-safe recovery' on every single profile switch."""
    rec, _, _, state = node
    rec.write_markers({"solo": rec.read_engines()["solo"]["_hash"]}, [])
    (state / "vllm-solo.running").write_text("")
    monkeypatch.setattr(rec, "_systemctl", lambda *a, **k: type(
        "P", (), {"returncode": 3, "stdout": "deactivating", "stderr": ""})())
    assert _status_of(rec, "solo")["failsafe"] is False


def test_activating_is_not_failsafe(node, monkeypatch):
    """ExecStartPre arms the marker before the main process forks, so there is a
    window of `activating` with the marker present. That is a start, not a skip."""
    rec, _, _, state = node
    rec.write_markers({"solo": rec.read_engines()["solo"]["_hash"]}, [])
    (state / "vllm-solo.running").write_text("")
    monkeypatch.setattr(rec, "_systemctl", lambda *a, **k: type(
        "P", (), {"returncode": 3, "stdout": "activating", "stderr": ""})())
    assert _status_of(rec, "solo")["failsafe"] is False


def test_inactive_and_desired_with_a_surviving_marker_IS_failsafe(node, monkeypatch):
    """The real thing: systemd skipped a desired unit at boot because the marker said
    the last shutdown was unclean."""
    rec, _, _, state = node
    rec.write_markers({"solo": rec.read_engines()["solo"]["_hash"]}, [])
    (state / "vllm-solo.running").write_text("")
    monkeypatch.setattr(rec, "_systemctl", lambda *a, **k: type(
        "P", (), {"returncode": 3, "stdout": "inactive", "stderr": ""})())
    assert _status_of(rec, "solo")["failsafe"] is True


def test_an_undesired_engine_is_never_failsafe(node, monkeypatch):
    """A stale marker on an engine nobody asked for gates nothing and means nothing —
    reporting it would be a permanent phantom alarm."""
    rec, _, _, state = node
    (state / "vllm-solo.running").write_text("")   # marker, but no desired marker
    monkeypatch.setattr(rec, "_systemctl", lambda *a, **k: type(
        "P", (), {"returncode": 3, "stdout": "inactive", "stderr": ""})())
    assert _status_of(rec, "solo")["failsafe"] is False


# --- a unit between restarts still owns its port (2026-08-11) -----------------
#
# The stop plan used to be `[e for e in others if active.get(e)]`, fed by
# `systemctl is-active --quiet`, which is true only for `active`. A failing engine sampled
# inside its 20-second RestartSec gap reads `activating`/`auto-restart`: not serving, and
# not free either. The plan recorded `"stop": []`, systemd restarted it, it took port
# 29501 back, and the next profile's head failed to bind five times and was quarantined.

def test_an_engine_between_restarts_is_stopped(rec):
    """THE regression. `activating` is a failing unit inside its RestartSec gap — it owns
    the port and is seconds from taking it again."""
    p = rec.plan("qwen", engines(BIG, SOLO), "snoopy", markers={"big": "h-big"},
                 active={"big": "activating"})
    assert p.stop == ["big"], "a unit between restarts was left holding its resources"
    assert p.drop_markers == ["big"]


def test_every_non_idle_state_is_stopped(rec):
    """`deactivating` and `failed` too. The only state that owns nothing is `inactive` —
    enumerating what to stop invites exactly the omission that caused this."""
    for state in ("active", "activating", "deactivating", "reloading", "failed"):
        p = rec.plan("qwen", engines(BIG, SOLO), "snoopy", markers={}, active={"big": state})
        assert p.stop == ["big"], f"{state} was not stopped"


def test_an_idle_engine_is_not_stopped(rec):
    """The other half: a plan that stops everything every time is noise, and noise in a
    plan is how a real stop stops being read."""
    for state in ("inactive", "unknown", "", None):
        p = rec.plan("qwen", engines(BIG, SOLO), "snoopy", markers={}, active={"big": state})
        assert p.stop == [], f"{state!r} should need no stop"


def test_a_wedged_target_is_restarted_not_started(rec):
    """If the TARGET is mid-restart-loop, `start` races systemd's own pending restart and
    can leave the old definition running. Stop-then-start is the only ordering that lands."""
    p = rec.plan("qwen", engines(SOLO), "snoopy", markers={"solo": "h-solo"},
                 active={"solo": "activating"})
    assert p.restart == ["solo"] and p.start == []


def test_bools_still_mean_what_they_used_to(rec):
    """`live_state()` returns strings now, but plan() takes bools too — the tests that
    only care whether a unit is up should not have to know systemd's vocabulary."""
    assert rec._serving(True) and not rec._serving(False)
    assert rec._needs_stop(True) and not rec._needs_stop(False)
    p = rec.plan("qwen", engines(BIG, SOLO), "snoopy", markers={}, active={"big": True})
    assert p.stop == ["big"]


def test_success_is_refused_when_another_profile_is_live(monkeypatch):
    """A gate that passes proves something is healthy — not that it is what you asked for.

    2026-08-12: an `-eagle` activation printed `…-eagle: live and gated` while the smoke
    table printed beside it named the CONTROL engine. A second activation had changed the
    selection underneath the first, and every step of `bring_up` reasons about the
    profile as a REQUEST while the smoke gate reads the LIVE topology. Nothing compared
    them.

    The cluster was fine; only the report was wrong — which is the worse of the two. A
    wrong engine that says so is a bug you fix in a minute. A wrong engine that says
    "gated" is a trap: it is exactly the state someone commits, benchmarks, or walks away
    from.
    """
    from sparky import activate as act

    monkeypatch.setattr(act, "activate", lambda p, force=False: 0)
    monkeypatch.setattr(act, "wait_for_ready", lambda *a, **k: True)
    monkeypatch.setattr(act, "live_profile", lambda: "some-other-profile")
    import sparky.cli as cli
    monkeypatch.setattr(cli, "_smoke", lambda *a, **k: 0)

    with pytest.raises(act.NotLive, match="is what is serving"):
        act.bring_up("the-one-i-asked-for")


def test_success_is_reported_when_the_live_profile_matches(monkeypatch):
    """The converse, so the guard cannot be satisfied by simply never succeeding."""
    from sparky import activate as act

    seen = []
    monkeypatch.setattr(act, "activate", lambda p, force=False: 0)
    monkeypatch.setattr(act, "wait_for_ready", lambda *a, **k: True)
    monkeypatch.setattr(act, "live_profile", lambda: "wanted")
    import sparky.cli as cli
    monkeypatch.setattr(cli, "_smoke", lambda *a, **k: 0)

    act.bring_up("wanted", on_event=seen.append)
    assert any("live and gated" in m for m in seen)


# --- activate must not fire into a running deploy (2026-08-12) -------------

def test_wait_for_deploy_returns_when_the_lock_is_free(tmp_path, monkeypatch):
    from sparky import activate as act, ansible, sweep
    monkeypatch.setattr(ansible, "FLEET_LOCK", tmp_path / "fleet.lock")
    monkeypatch.setattr(sweep, "_fleet_fd", None)
    act.wait_for_deploy(timeout=1.0, poll=0.05)      # returns, does not raise


def test_wait_for_deploy_skips_when_this_process_holds_the_lock(tmp_path, monkeypatch):
    """A campaign holds `fleet.lock` for its whole run and activates once per job.

    flock is per open-file-description, so a second acquire from the same process blocks
    against itself: waiting here would hang every sweep forever. This is the deadlock the
    guard has to dodge, and it is why the naive "just take the lock" fix is wrong.
    """
    import fcntl
    from sparky import activate as act, ansible, sweep
    lock = tmp_path / "fleet.lock"
    monkeypatch.setattr(ansible, "FLEET_LOCK", lock)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o664)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)     # we are the campaign
    monkeypatch.setattr(sweep, "_fleet_fd", fd)
    try:
        act.wait_for_deploy(timeout=1.0, poll=0.05)     # must NOT hang or raise
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_wait_for_deploy_raises_rather_than_waiting_forever(tmp_path, monkeypatch):
    """A wedged deploy must surface, not silently stall an activation for 30 minutes."""
    import fcntl
    import pytest
    from sparky import activate as act, ansible, sweep
    lock = tmp_path / "fleet.lock"
    monkeypatch.setattr(ansible, "FLEET_LOCK", lock)
    monkeypatch.setattr(sweep, "_fleet_fd", None)       # someone ELSE holds it
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o664)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(act.NotLive, match="fleet lock"):
            act.wait_for_deploy(timeout=0.2, poll=0.05)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_bring_up_consults_the_guard_before_activating():
    """The guard is only worth having if `bring_up` actually calls it."""
    import inspect
    from sparky import activate as act
    src = inspect.getsource(act.bring_up)
    assert "wait_for_deploy" in src.split("activate(profile")[0], \
        "bring_up must wait for a deploy BEFORE requesting the activation"
