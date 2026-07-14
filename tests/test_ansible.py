"""Unit tests for the ansible invoker (ADR-0015) — command assembly, no execution."""

from sparky import ansible


def test_as_deploy_prefix(monkeypatch):
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "geoff")
    assert ansible._as_deploy() == ["sudo", "-u", "deploy"]
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    assert ansible._as_deploy() == []


def test_playbook_cmd_deploy(monkeypatch):
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")  # no sudo prefix
    cmd = ansible._playbook_cmd("site.yml", "minimax-m2.7-awq", ["--check", "--diff"])
    assert cmd[:2] == ["ansible-playbook", "site.yml"]
    assert cmd[cmd.index("-e") + 1] == "@profiles/minimax-m2.7-awq.yml"
    assert cmd[-2:] == ["--check", "--diff"]


def test_playbook_cmd_no_profile(monkeypatch):
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    assert ansible._playbook_cmd("teardown.yml", None, []) == ["ansible-playbook", "teardown.yml"]


def test_deploy_publishes_then_runs_site(monkeypatch):
    seen = {}
    monkeypatch.setattr(ansible, "publish", lambda: seen.__setitem__("published", True))
    monkeypatch.setattr(ansible, "_run", lambda cmd, **kw: seen.__setitem__("cmd", cmd) or 0)
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    rc = ansible.deploy("minimax-m2.7-awq", dry_run=True)
    assert seen["published"] is True
    assert "site.yml" in seen["cmd"] and "--check" in seen["cmd"]
    assert rc == 0


def test_teardown_webui_adds_tags(monkeypatch):
    seen = {}
    monkeypatch.setattr(ansible, "publish", lambda: None)
    monkeypatch.setattr(ansible, "_run", lambda cmd, **kw: seen.__setitem__("cmd", cmd) or 0)
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    ansible.teardown("empty", include_webui=True)
    assert seen["cmd"][:2] == ["ansible-playbook", "teardown.yml"]
    assert "--tags" in seen["cmd"] and "all,webui" in seen["cmd"]


def test_status_and_logs_shape(monkeypatch):
    cmds = []
    monkeypatch.setattr(ansible, "_run", lambda cmd, **kw: cmds.append(cmd) or 0)
    monkeypatch.setattr(ansible.getpass, "getuser", lambda: "deploy")
    ansible.status()
    ansible.logs("head")
    ansible.logs("worker")
    status_cmd, head_cmd, worker_cmd = cmds
    assert status_cmd[:3] == ["ansible", "all", "-m"]
    assert head_cmd[:2] == ["sudo", "journalctl"]
    assert "ssh" in worker_cmd and ansible.WORKER_SSH in worker_cmd
