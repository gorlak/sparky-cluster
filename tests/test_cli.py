"""CLI tests — the exit-code contracts agents gate on (ADR-0011).

`status` reads the control panel (ops.panel_status, mocked here — no network): 0
healthy / 1 degraded / 2 panel-unreachable. `activate` is the other half — the
unprivileged operation an agent drives (ADR-0018), so its refusals matter as much as
its successes. See skills/operations/SKILL.md.
"""

import pytest
from typer.testing import CliRunner

from sparky import activate as act
from sparky import ansible as ops
from sparky import cli

runner = CliRunner()


def _status(ok, has_topology=True):
    return {"has_topology": has_topology, "ok": ok, "profile": "p",
            "deployed_at": "2026-07-25T00:00:00Z", "failsafe": not ok,
            "engines": [], "services": [{"name": "Caddy", "state": "running"}]}


def test_status_exit0_when_healthy(monkeypatch):
    monkeypatch.setattr(ops, "panel_status", lambda: _status(ok=True))
    assert runner.invoke(cli.app, ["status"]).exit_code == 0


def test_status_exit1_when_degraded(monkeypatch):
    monkeypatch.setattr(ops, "panel_status", lambda: _status(ok=False))
    assert runner.invoke(cli.app, ["status"]).exit_code == 1


def test_status_json_exit2_when_panel_unreachable(monkeypatch):
    monkeypatch.setattr(ops, "panel_status", lambda: None)
    res = runner.invoke(cli.app, ["status", "--json"])
    assert res.exit_code == 2
    assert "unreachable" in res.stdout


def test_status_runs_no_subprocess_when_the_panel_is_unreachable(monkeypatch):
    """The old fallback shelled `sudo -u deploy ansible`, which prompts for a password
    — surprising a human and hanging an agent — and it fired exactly when a node was
    down. Asserted on behaviour, not wording: nothing may be executed at all."""
    monkeypatch.setattr(ops, "panel_status", lambda: None)
    monkeypatch.setattr(ops, "_run", lambda *a, **k: pytest.fail("status executed a command"))
    for args in (["status"], ["status", "--json"]):
        res = runner.invoke(cli.app, args)
        assert res.exit_code == 2, args


def _unreachable(monkeypatch, fleet=None):
    monkeypatch.setattr(ops, "panel_status", lambda: None)
    monkeypatch.setattr(act, "fleet_state", lambda *a, **k: fleet)
    return runner.invoke(cli.app, ["status"]).stdout


def test_status_points_at_the_unprivileged_probes_instead(monkeypatch):
    """A dead end helps nobody: name the commands that DO work without the panel."""
    out = _unreachable(monkeypatch)
    assert "--status" in out
    assert "deploy" in out          # how to repair the panel


def test_status_answers_the_question_you_actually_have_first(monkeypatch):
    """The panel is a status surface; it being down says nothing about whether the
    cluster is serving. Lead with the one line that answers that."""
    out = _unreachable(monkeypatch, fleet={
        "model_endpoint": "http://api.example.net",
        "nodes": [{"node": "snoopy"}, {"node": "sparky"}]})
    assert "http://api.example.net/health" in out
    assert out.index("serving") < out.index("Why is the panel down")


def test_status_names_every_node_deploy_recorded(monkeypatch):
    """Derived from what `deploy` wrote, not hardcoded — so it stays right as the
    Peanuts roster grows past two."""
    out = _unreachable(monkeypatch, fleet={"model_endpoint": "http://x",
                                           "nodes": [{"node": "sparky"}, {"node": "snoopy"},
                                                     {"node": "woodstock"}]})
    for node in ("snoopy", "woodstock"):
        assert f"ssh {node} " in out
    # this host needs no ssh, and comes first — it still answers when the network is
    # what's broken
    body = out[out.index("Per-node"):]
    assert body.index("/usr/local/sbin/vllm-activate --status\n") < body.index("ssh ")


def test_status_json_emits_the_dict(monkeypatch):
    monkeypatch.setattr(ops, "panel_status", lambda: _status(ok=True))
    res = runner.invoke(cli.app, ["status", "--json"])
    assert res.exit_code == 0
    assert '"ok": true' in res.stdout and '"profile": "p"' in res.stdout


# --- activate: the unprivileged operation ----------------------------------

def _no_reconcile(monkeypatch, rc=0):
    calls = {}
    monkeypatch.setattr(act, "write_request", lambda p, **kw: calls.__setitem__("wrote", p))
    monkeypatch.setattr(act, "reconcile",
                        lambda **kw: calls.update(kw) or type("P", (), {"returncode": rc})())
    return calls


def test_activate_refuses_a_profile_this_cluster_has_not_deployed(monkeypatch):
    """The allowlist is the policy and only `deploy` writes it — so an agent asking
    for something undeployed gets a clear refusal, not a half-applied activation."""
    monkeypatch.setattr(act, "read_allowlist", lambda *a, **k: ["empty", "step"])
    calls = _no_reconcile(monkeypatch)
    res = runner.invoke(cli.app, ["activate", "not-deployed"])
    assert res.exit_code == 2
    assert "not activatable" in res.stdout
    assert "wrote" not in calls  # the request is never even recorded


def test_activate_refuses_when_the_cluster_was_never_deployed(monkeypatch):
    monkeypatch.setattr(act, "read_allowlist", lambda *a, **k: [])
    _no_reconcile(monkeypatch)
    res = runner.invoke(cli.app, ["activate", "step"])
    assert res.exit_code == 2
    assert "deploy" in res.stdout


def test_activate_empty_writes_the_request_and_skips_the_wait(monkeypatch):
    """`empty` has nothing to become ready, so waiting on it would just hang."""
    monkeypatch.setattr(act, "read_allowlist", lambda *a, **k: ["empty", "step"])
    calls = _no_reconcile(monkeypatch)
    monkeypatch.setattr(act, "wait_for_ready", lambda *a, **k: pytest.fail("should not wait"))
    assert runner.invoke(cli.app, ["activate", "empty"]).exit_code == 0
    assert calls["wrote"] == "empty"


def test_activate_propagates_a_reconciler_failure(monkeypatch):
    monkeypatch.setattr(act, "read_allowlist", lambda *a, **k: ["empty", "step"])
    _no_reconcile(monkeypatch, rc=1)
    assert runner.invoke(cli.app, ["activate", "step"]).exit_code == 1


def test_activate_with_no_argument_lists_what_is_activatable(monkeypatch):
    monkeypatch.setattr(act, "read_allowlist", lambda *a, **k: ["empty", "step"])
    monkeypatch.setattr(act, "live_profile", lambda: "step")
    monkeypatch.setattr(act, "requested", lambda *a, **k: "step")
    res = runner.invoke(cli.app, ["activate"])
    assert res.exit_code == 0
    assert "step" in res.stdout and "empty" in res.stdout


def test_teardown_is_just_activate_empty(monkeypatch):
    """No sudo, no ansible — stopping serving is a selection, not a provisioning act."""
    seen = {}

    def fake_activate(profile, **kw):
        seen["profile"] = profile
        return 0

    monkeypatch.setattr(act, "activate", fake_activate)
    monkeypatch.setattr(ops, "teardown", lambda **kw: pytest.fail("should not run ansible"))
    assert runner.invoke(cli.app, ["teardown"]).exit_code == 0
    assert seen["profile"] == "empty"


def test_teardown_break_glass_goes_through_ansible(monkeypatch):
    seen = {}
    monkeypatch.setattr(ops, "teardown", lambda **kw: seen.update(kw) or 0)
    assert runner.invoke(cli.app, ["teardown", "--break-glass"]).exit_code == 0
    assert seen == {"include_webui": False}


def test_the_request_file_keeps_its_inode_and_permissions(tmp_path):
    """The request is shared by geoff, the panel service and the agent, and it is the
    INODE's group ownership that lets all three write it. A write-and-rename would
    hand ownership to whoever activated last and lock the others out."""
    f = tmp_path / "desired-profile"
    f.write_text("step\n")
    before = f.stat().st_ino
    act.write_request("qwen", f)
    assert f.read_text() == "qwen\n"
    assert f.stat().st_ino == before


def test_a_stale_session_gets_a_diagnosis_not_a_traceback(monkeypatch, tmp_path):
    """Group membership is granted at LOGIN, so the deploy that first creates the
    `activate` group cannot retrofit it onto shells already open. That makes this the
    single most likely first-run failure of the whole ADR-0018 boundary — and it used
    to surface as a raw PermissionError traceback, which reads like a broken cluster."""
    monkeypatch.setattr(act, "read_allowlist", lambda *a, **k: ["empty", "step"])
    monkeypatch.setattr(act, "group_diagnosis", lambda *a, **k: "THIS SESSION predates it")

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(act, "write_request", denied)
    monkeypatch.setattr(act, "reconcile", lambda **k: pytest.fail("must not reconcile"))
    res = runner.invoke(cli.app, ["activate", "step"])
    assert res.exit_code == 2
    assert "THIS SESSION predates it" in res.stdout
