"""Every command declares its SCOPE — enforced, not decorative.

`sparky` mixes three things that differ in the one way that matters operationally:
whether they need a password, whether an agent may run them, and whether they need a
live cluster. That boundary is ADR-0018's whole subject, and it used to exist only in
prose — `--help` listed "converge the whole fleet" next to "compare two benchmark labels"
in one flat alphabetical wall, so the reader had to already know the answer.

Rich help panels made it visible. This test makes it *true*: a command with no scope
fails the suite rather than quietly joining a default group, which is the only reason the
grouping will still be right in six months.
"""

from __future__ import annotations

from sparky import cli

# The scope is a claim about privilege, so the vocabulary is closed. Adding a fourth
# should be a deliberate act with a reason, not a typo that silently passes.
SCOPES = {
    "Operate — no privilege, agent-drivable, needs a live cluster",
    "Provision — password-gated (sudo -u deploy), control node only",
    "Develop — repo only, no cluster, no privilege",
}


def _commands():
    return list(cli.app.registered_commands)


def test_every_command_declares_a_scope():
    missing = [c.name or c.callback.__name__ for c in _commands()
               if not getattr(c, "rich_help_panel", None)]
    assert not missing, (
        f"commands with no scope: {missing}. Add rich_help_panel=<one of {sorted(SCOPES)}> "
        f"— a reader choosing a command needs to know whether it will ask for a password.")


def test_no_command_invents_a_new_scope():
    unknown = {getattr(c, "rich_help_panel", None) for c in _commands()} - SCOPES
    assert not unknown, f"unknown scope(s): {unknown}"


def test_the_privileged_scope_stays_small():
    """The provision scope is the password-gated surface. It was three commands and is now
    two (`check` folded into `deploy --check`). Growth here means the boundary ADR-0018
    drew is moving, which should be a decision rather than a drift."""
    provisioning = [c.name or c.callback.__name__ for c in _commands()
                    if getattr(c, "rich_help_panel", "").startswith("Provision")]
    assert len(provisioning) <= 3, f"provisioning surface grew: {provisioning}"


def test_check_is_a_flag_on_deploy_not_a_command():
    """It was a separate command until 2026-08-10, which made it a third thing to
    classify: it reads like development but is `deploy` in every way that matters — same
    code path, same sudo prefix, same publish to /opt/cluster."""
    names = {c.name or c.callback.__name__ for c in _commands()}
    assert "check" not in names
    import inspect
    deploy = next(c for c in _commands() if (c.name or c.callback.__name__) == "deploy")
    assert "check" in inspect.signature(deploy.callback).parameters
