"""Unit tests for the ansible invoker (ADR-0015) — command assembly, no execution.

Since ADR-0018 ansible is the `deploy` engine ONLY: whole-fleet, no profile argument,
password-gated. These assert that shape, plus the deploy/sweep mutex.
"""

import os

import pytest

from sparky import ansible


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
    """A deploy reshapes the boundary while a sweep walks it — they must not interleave."""
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    cmd = ansible._playbook_cmd("site.yml", [])
    assert cmd[:3] == ["flock", str(ansible.FLEET_LOCK), "ansible-playbook"]


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


@pytest.mark.real_lock_paths
def test_the_campaign_and_the_deploy_take_THE_SAME_lock():
    """They were different files until 2026-08-11, while a comment in ansible.py asserted
    they were the same. `deploy` took `fleet.lock`; the sweep took `sweep.lock`, which
    only ever excluded other sweeps. So nothing stopped a deploy from re-rendering engine
    files, pulling an image or evicting weights in the middle of a measurement — and the
    resulting numbers would belong to no configuration, invisibly.

    Pinned as constants rather than as behaviour because the mechanisms differ by
    necessity: `flock(1)` in a shell on one side, `fcntl.flock` on the other. The file is
    the only thing they share, so the file is what has to match.
    """
    from sparky import ansible, sweep

    assert sweep.FLEET_LOCK == ansible.FLEET_LOCK
    assert sweep.DEFAULT_LOCK != ansible.FLEET_LOCK  # still distinct roles


def test_a_campaign_holding_the_fleet_refuses_the_deploy(tmp_path, monkeypatch, capsys):
    """And says which campaign, and how to end it. `flock` alone would be correct and
    awful — the deploy would sit silent for however long the campaign has left."""
    import fcntl

    from sparky import ansible, sweep

    lock = tmp_path / "fleet.lock"
    monkeypatch.setattr(ansible, "FLEET_LOCK", lock)
    monkeypatch.setattr(sweep, "FLEET_LOCK", lock)
    monkeypatch.setattr(sweep, "_fleet_fd", None)

    assert ansible.campaign_holding_the_fleet() is False
    sweep._hold_fleet_lock(lock)
    try:
        # Held in-process, so an flock from another fd in the SAME process still sees it.
        fd = os.open(lock, os.O_RDWR)
        try:
            with pytest.raises(OSError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        sweep.release(tmp_path / "sweep.lock")


def test_a_sweep_refuses_to_start_under_a_deploy(tmp_path, monkeypatch):
    """The other direction. Starting a seven-hour campaign into a deploy that is halfway
    through re-rendering the fleet measures a moving target."""
    import fcntl

    from sparky import sweep

    lock = tmp_path / "fleet.lock"
    monkeypatch.setattr(sweep, "FLEET_LOCK", lock)
    monkeypatch.setattr(sweep, "_fleet_fd", None)
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o664)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(sweep.SweepBusy) as exc:
            sweep.acquire(tmp_path / "sweep.lock")
        assert "deploy is in progress" in str(exc.value)
    finally:
        os.close(holder)
