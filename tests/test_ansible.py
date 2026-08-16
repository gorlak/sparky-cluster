"""Unit tests for the ansible invoker (ADR-0015) — command assembly, no execution.

Since ADR-0018 ansible is the `deploy` engine ONLY: whole-fleet, no profile argument,
password-gated. These assert that shape, plus the deploy/suite mutex.
"""

import os

from pathlib import Path

import pytest

from sparky.serve import ansible


def test_as_deploy_prefix(monkeypatch):
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "geoff")
    assert ansible._as_deploy() == ["sudo", "-u", "deploy"]
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    assert ansible._as_deploy() == []


def test_playbook_cmd_takes_no_profile(monkeypatch):
    """`deploy` means *deploy the fleet* — a profile argument would be the old model."""
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    cmd = ansible._playbook_cmd("site.yml", ["--check", "--diff"])
    assert "site.yml" in cmd
    assert "-e" not in cmd
    assert not any("profiles/" in str(c) for c in cmd)
    assert cmd[-2:] == ["--check", "--diff"]


def test_playbook_cmd_takes_the_fleet_lock(monkeypatch):
    """A deploy reshapes the boundary while a suite walks it — they must not interleave."""
    from sparky.foundation import fleetlock

    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    cmd = ansible._playbook_cmd("site.yml", [])
    # Position, not prefix: the command is wrapped (`env ANSIBLE_LOG_PATH=...`), so what
    # matters is that flock still guards the playbook — not that it comes first.
    i = cmd.index("flock")
    assert cmd[i + 1] == str(fleetlock.FLEET_LOCK)
    assert cmd[i + 2] == "ansible-playbook", "flock must wrap the playbook, not something else"


def test_the_deploy_log_is_written_only_by_the_deploy_identity(monkeypatch):
    """`log_path` in ansible.cfg would be read by BOTH identities that run ansible.

    `sparky lint` shells out to `ansible-playbook --syntax-check` as **geoff**; a deploy
    runs as **deploy**. Whichever ran first created the log and locked the other out —
    /opt/cluster is sticky and `fs.protected_regular=2` refuses a non-owner open even when
    the mode bits allow it. That broke every deploy on 2026-08-13 until the log was scoped
    to the deploy invocation, where ownership is stable.
    """
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    cmd = [str(c) for c in ansible._playbook_cmd("site.yml", [])]
    assert any(c.startswith("ANSIBLE_LOG_PATH=") for c in cmd), "the deploy must log"
    cfg = (Path(__file__).resolve().parent.parent / "ansible/ansible.cfg").read_text()
    assert "log_path" not in cfg, \
        "log_path in ansible.cfg is shared with `sparky lint`, which runs as geoff"


def test_deploy_publishes_then_runs_site(monkeypatch):
    seen = {}
    monkeypatch.setattr(ansible, "publish", lambda: seen.__setitem__("published", True))
    monkeypatch.setattr(ansible, "_run", lambda cmd, **kw: seen.__setitem__("cmd", cmd) or 0)
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    rc = ansible.deploy(dry_run=True)
    assert seen["published"] is True
    assert "site.yml" in seen["cmd"] and "--check" in seen["cmd"]
    assert rc == 0


def test_eviction_is_opt_in(monkeypatch):
    """Plan by default: a de-allowlisted model is reported, never silently deleted."""
    seen = {}
    monkeypatch.setattr(ansible, "publish", lambda: None)
    monkeypatch.setattr(ansible, "_run", lambda cmd, **kw: seen.__setitem__("cmd", cmd) or 0)
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    ansible.deploy()
    assert "evict_weights=true" not in seen["cmd"]
    ansible.deploy(evict=True)
    assert seen["cmd"][seen["cmd"].index("-e") + 1] == "evict_weights=true"


def test_teardown_webui_adds_tags(monkeypatch):
    seen = {}
    monkeypatch.setattr(ansible, "publish", lambda: None)
    monkeypatch.setattr(ansible, "_run", lambda cmd, **kw: seen.__setitem__("cmd", cmd) or 0)
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    ansible.teardown(include_webui=True)
    assert "teardown.yml" in seen["cmd"]
    assert "--tags" in seen["cmd"] and "all,webui" in seen["cmd"]


def test_logs_target_the_template_unit(monkeypatch):
    cmds = []
    monkeypatch.setattr(ansible, "_run", lambda cmd, **kw: cmds.append(cmd) or 0)
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    ansible.logs("head")
    ansible.logs("worker")
    head_cmd, worker_cmd = cmds
    assert head_cmd == ["journalctl", "-u", "vllm@*", "-f"]
    assert worker_cmd == ["ssh", ansible.WORKER_HOST, "journalctl", "-u", "vllm@*", "-f"]


def test_there_is_no_ansible_status_path(monkeypatch):
    """Removed deliberately. Reading status is not privileged — plain `systemctl
    is-active` and the reconciler's `--status` verb both work as geoff on every node —
    so a `sudo -u deploy ansible` route bought nothing and prompted for a password at
    the worst possible moment: when a node was down and the panel merely got slow."""
    assert not hasattr(ansible, "status")


def test_the_panel_budget_exceeds_the_panels_own_worst_case(monkeypatch):
    """The panel probes every node, so a node that is DOWN makes it slow, not absent.
    A budget under that turns 'slow' into 'unreachable'."""
    assert ansible.PANEL_TIMEOUT >= 15.0


def test_reading_logs_needs_no_privilege(monkeypatch):
    """ADR-0018 retired geoff's `NOPASSWD: journalctl` — it was both unnecessary (`adm`
    membership is enough) and a root shell waiting to happen (journalctl pages through
    less; `!sh`). Nothing in the log path may reach for sudo again."""
    cmds = []
    monkeypatch.setattr(ansible, "_run", lambda cmd, **kw: cmds.append(cmd) or 0)
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "geoff")
    ansible.logs("head")
    ansible.logs("worker")
    assert all("sudo" not in c for cmd in cmds for c in cmd)


def test_a_run_holding_the_fleet_refuses_the_deploy(tmp_path, monkeypatch, capsys):
    """A deploy declines while a measurement holds the fleet, rather than blocking on the
    flock — which would sit silent for however long the run has left. The run and the
    deploy take the SAME lock, now that it has one home; that they do is structural
    (`fleetlock.hold` vs `flock(1)` on `fleetlock.FLEET_LOCK`), not something a test pins."""
    from sparky.foundation import fleetlock
    from sparky.serve import ansible

    lock = tmp_path / "fleet.lock"
    monkeypatch.setattr(fleetlock, "FLEET_LOCK", lock)
    monkeypatch.setattr(fleetlock, "_fleet_fd", None)

    assert ansible._refuse_while_a_suite_runs() is False
    fleetlock.hold()
    try:
        assert ansible._refuse_while_a_suite_runs() is True
        assert "REFUSING" in capsys.readouterr().out
    finally:
        fleetlock.release()


def test_a_run_refuses_to_start_under_a_deploy(tmp_path, monkeypatch):
    """The other direction. Starting a seven-hour suite into a deploy that is halfway
    through re-rendering the fleet measures a moving target."""
    import fcntl

    from sparky.foundation import fleetlock
    from sparky.measure.loop import runner

    lock = tmp_path / "fleet.lock"
    monkeypatch.setattr(fleetlock, "FLEET_LOCK", lock)
    monkeypatch.setattr(fleetlock, "_fleet_fd", None)
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o664)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(runner.SuiteBusy) as exc:
            runner.acquire(tmp_path / "runner.lock")
        assert "deploy is in progress" in str(exc.value)
    finally:
        os.close(holder)
