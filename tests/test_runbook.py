"""Runbooks (ADR-0020, ADR-0011 Layer 3) — no hardware.

The interesting tests are the refusals. A runbook is instanced by anything that can reach
the panel identity, so what it may NOT express is the whole security argument.
"""

from __future__ import annotations

import pytest

from sparky import runbook


def test_a_runbook_is_addressed_by_name_never_by_path():
    """A path argument would let any YAML on the box run, and the allowlist would be
    decoration. It also removes traversal as a concern rather than sanitising for it."""
    for hostile in ("../../etc/passwd", "/etc/passwd", "./secret", "a/b"):
        with pytest.raises(runbook.BadRunbook):
            runbook.path_for(hostile)


def test_only_the_installed_set_may_be_instanced(tmp_path, monkeypatch):
    """ADR-0021. The repo is where a runbook is authored; the installed set is what may
    RUN. A list that answers differently depending on which caller asks is not an
    allowlist — and the CLI and the panel are two callers of the same trigger, so the
    repo copy must not leak into what can be started."""
    monkeypatch.setattr(runbook, "INSTALLED_DIR", tmp_path)
    (tmp_path / "deployed.yml").write_text("jobs: []\n")
    assert runbook.available() == ["deployed"]
    assert set(runbook.authored()) - {"deployed"}, "the repo has runbooks that are not here"
    for name in runbook.authored():
        assert name not in runbook.available() or name == "deployed"


def test_an_unknown_name_says_which_kind_of_missing_it_is(tmp_path):
    """A typo and a missing deploy look identical from outside, and the fix differs."""
    (tmp_path / "installed-one.yml").write_text("jobs: []\n")
    with pytest.raises(runbook.UnknownRunbook) as exc:
        runbook.path_for("definitely-not-a-runbook", directory=tmp_path)
    assert "installed-one" in str(exc.value)


def test_an_authored_but_undeployed_runbook_says_so(tmp_path):
    """The one confusing case ADR-0021 introduces: it is in the repo, you can read it, and
    it still will not start. Saying 'no such runbook' there would send you hunting for a
    typo that is not present."""
    authored = runbook.authored()
    assert authored, "expected at least one runbook in the repo"
    with pytest.raises(runbook.UnknownRunbook) as exc:
        runbook.path_for(authored[0], directory=tmp_path)
    assert "has not been deployed" in str(exc.value)


def test_the_repo_runbooks_validate():
    """The repo copies, because validation is the gate a DEPLOY passes through — it must
    fail on the file about to be installed, not the one already installed."""
    for name in runbook.authored():
        assert runbook.validate(name) == [], f"{name} does not validate"


# --- the safety argument ----------------------------------------------------

def test_a_step_may_only_invoke_an_operate_scope_command():
    """THE decision of ADR-0020. Anything that instances a runbook runs it as `activator`,
    which holds two single-command sudoers entries — so the set of invocable commands is
    the security boundary, and it is DERIVED from sparky's own scope declarations rather
    than duplicated here, so the two cannot drift."""
    spec = {"jobs": [{"profile": "p", "regiments": [{"cmd": "deploy", "args": []}]}]}
    problems = runbook.validate("hostile", spec)
    assert problems and "not an Operate-scope command" in problems[0]


def test_the_privileged_commands_are_excluded_by_construction():
    allowed = runbook.operate_commands()
    assert "deploy" not in allowed and "admin-password" not in allowed
    assert {"bench", "eval", "activate"} <= allowed


def test_args_must_be_argv_not_a_shell_string():
    """A string would be a shell command. That single difference is what separates a
    runbook from a remote shell running as an identity with sudoers entries."""
    spec = {"jobs": [{"profile": "p",
                      "regiments": [{"cmd": "bench", "args": "x; rm -rf /"}]}]}
    problems = runbook.validate("hostile", spec)
    assert problems and "must be a LIST" in problems[0]


def test_every_fault_is_reported_not_just_the_first():
    """Same reason `Fleet.validate` collects: fixing one fault at a time across a deploy
    cycle is how a five-minute change becomes an afternoon."""
    spec = {"jobs": [{"profile": "", "regiments": [{"cmd": "deploy", "args": []}]},
                     {"profile": "q", "regiments": [{"cmd": "nope", "args": []}]}]}
    assert len(runbook.validate("messy", spec)) >= 3


def test_a_runbook_with_no_jobs_is_refused():
    assert runbook.validate("empty", {"jobs": []})


# --- `covers: allowlist` ----------------------------------------------------

def test_covers_allowlist_catches_a_profile_the_runbook_forgot():
    """ADR-0020 keeps job lists literal — what you read is what runs. The cost is drift:
    add a profile, forget the standing campaign, and the gap surfaces weeks later as a
    missing scoreboard row. Declaring what a list is SUPPOSED to cover turns that into a
    lint failure, without making the list itself expand at runtime."""
    spec = {"covers": "allowlist", "defaults": {"regiments": ["tools"]},
            "jobs": [{"profile": "qwen3.6-35b-a3b-nvfp4"}]}
    problems = runbook.validate("all", spec)
    assert problems and "omits" in problems[0]


def test_covers_allowlist_catches_a_profile_that_cannot_be_activated():
    """A parked (`blocked: true`) profile keeps its weights precisely so it cannot be
    activated. Listing one would quarantine on the first job every single run."""
    spec = {"covers": "allowlist", "defaults": {"regiments": ["tools"]},
            "jobs": [{"profile": "step-3.7-flash-nvfp4"}]}
    problems = runbook.validate("all", spec)
    assert problems and "not activatable" in " ".join(problems)


def test_an_unknown_covers_declaration_is_refused():
    """Silently ignoring it would be the worst outcome — a runbook that looks guarded."""
    spec = {"covers": "everything", "jobs": [{"profile": "p", "regiments": ["tools"]}]}
    assert any("unknown `covers:" in p for p in runbook.validate("x", spec))


def test_the_all_runbook_covers_the_allowlist():
    """The point of the declaration, exercised against the real files: `all` and
    `ansible/profiles/` must agree, or one of them is a lie."""
    assert "all" in runbook.authored()
    assert runbook.validate("all") == []


def test_the_declared_name_must_match_the_filename():
    """The FILE basename is what addresses a runbook — the trigger's allowlist check, the
    log path, `sparky run <name>`. A `name:` that says something else is a label nothing
    honours, and the first person to trust it goes looking for a log that is not there."""
    spec = {"name": "something-else", "jobs": [{"profile": "p", "regiments": ["tools"]}]}
    problems = runbook.validate("real-name", spec)
    assert problems and "the file is real-name.yml" in problems[0]


def test_the_menu_is_ordered_deliberately_not_alphabetically(tmp_path, monkeypatch):
    """The list is a menu. Alphabetical put whatever happens to start with 'a' in front of
    whatever you actually reach for, and left `nemotron-family` — a one-off pair
    comparison — sitting among the standing campaigns.

    Declared per file rather than in a central list, which would be one more thing to
    forget when a runbook is added and would live nowhere in particular.
    """
    monkeypatch.setattr(runbook, "INSTALLED_DIR", tmp_path)
    (tmp_path / "zzz-first.yml").write_text("order: 1\njobs: [{profile: a}]\n")
    (tmp_path / "aaa-last.yml").write_text("order: 99\njobs: [{profile: a}]\n")
    (tmp_path / "mmm-default.yml").write_text("jobs: [{profile: a}]\n")
    assert [r["name"] for r in runbook.describe()] == [
        "zzz-first", "mmm-default", "aaa-last"]


def test_an_unordered_runbook_does_not_break_the_menu(tmp_path, monkeypatch):
    """A missing or nonsense `order:` must not throw — the menu has to render even for a
    file `lint` would reject, or one bad runbook hides every good one."""
    monkeypatch.setattr(runbook, "INSTALLED_DIR", tmp_path)
    (tmp_path / "junk.yml").write_text("order: soon\njobs: [{profile: a}]\n")
    assert runbook.describe()[0]["order"] == runbook.DEFAULT_ORDER


def test_nemotron_family_sits_last():
    """The ask, pinned against the real files: it is a one-off pair comparison, not part
    of the standing rotation."""
    names = [r["name"] for r in runbook.describe(runbook.REPO_DIR)]
    assert names[-1] == "nemotron-family"
    assert names[0] == "all"
