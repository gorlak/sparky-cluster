"""CLI tests — the `sparky status` exit-code contract (ADR-0011).

`status` reads the control panel (ops.panel_status, mocked here — no network) and
its exit code is the health verdict agents gate on: 0 healthy / 1 degraded /
2 panel-unreachable. See skills/operations/SKILL.md.
"""

from typer.testing import CliRunner

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


def test_status_json_emits_the_dict(monkeypatch):
    monkeypatch.setattr(ops, "panel_status", lambda: _status(ok=True))
    res = runner.invoke(cli.app, ["status", "--json"])
    assert res.exit_code == 0
    assert '"ok": true' in res.stdout and '"profile": "p"' in res.stdout
