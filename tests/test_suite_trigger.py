"""The suite trigger (ADR-0021, ADR-0011 Layer 3) — no systemd, no deploy.

`vllm-suite` is a *grant*: it runs as root, and anything that can reach the control
panel can invoke it. So the tests that matter are the refusals — what it will not do with
input it is handed — and they are written against `validate()`, which is pure for exactly
this reason.

Its sibling `tests/test_probe.py` makes the same argument about ADR-0019's probe.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

TRIGGER = (Path(__file__).resolve().parent.parent
           / "ansible/roles/activate/files/vllm-suite")


def _load():
    """Load the trigger as a module. It has no .py suffix — it is an installed program,
    not a library — so it needs an explicit loader."""
    spec = importlib.util.spec_from_loader(
        "vllm_suite",
        importlib.machinery.SourceFileLoader("vllm_suite", str(TRIGGER)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trigger = _load()


@pytest.fixture
def installed(tmp_path):
    (tmp_path / "nightly.yml").write_text("jobs: []\n")
    (tmp_path / "tp-compare.yml").write_text("jobs: []\n")
    return tmp_path


# --- the refusals -----------------------------------------------------------

def test_a_name_that_is_a_path_is_refused(installed):
    """The allowlist would be decoration if a caller could name `../../etc/anything`.
    Checking the SHAPE first makes traversal unexpressible rather than filtered."""
    for hostile in ("../nightly", "/etc/passwd", "./nightly", "a/b", "nightly/../x"):
        with pytest.raises(SystemExit):
            trigger.validate(["start", hostile], directory=installed)


def test_a_name_with_shell_metacharacters_is_refused(installed):
    """Nothing here reaches a shell — the trigger execs a list — but a name that could
    only be interesting to a shell is a caller probing for one, and it is not a suite."""
    for hostile in ("nightly; rm -rf /", "nightly && x", "$(id)", "nightly\nstop", "-rf"):
        with pytest.raises(SystemExit):
            trigger.validate(["start", hostile], directory=installed)


def test_a_name_not_in_the_installed_set_is_refused(installed):
    """Being in the repo is not being deployed. That distinction is the allowlist."""
    with pytest.raises(SystemExit):
        trigger.validate(["start", "not-deployed"], directory=installed)


def test_an_empty_allowlist_refuses_everything(installed, tmp_path):
    """A missing deploy must not fail open."""
    empty = tmp_path / "nothing-here"
    empty.mkdir()
    with pytest.raises(SystemExit):
        trigger.validate(["start", "nightly"], directory=empty)


def test_an_unknown_verb_is_refused(installed):
    for argv in (["run", "nightly"], ["restart"], ["--help"], []):
        with pytest.raises(SystemExit):
            trigger.validate(argv, directory=installed)


def test_extra_arguments_are_refused(installed):
    """One name, nothing else. Every path the program touches is composed from its own
    constants, so there is nothing a second argument could legitimately be."""
    for argv in (["start", "nightly", "extra"], ["stop", "nightly"], ["status", "x"]):
        with pytest.raises(SystemExit):
            trigger.validate(argv, directory=installed)


# --- what it does accept ----------------------------------------------------

def test_an_installed_suite_is_accepted(installed):
    assert trigger.validate(["start", "nightly"], directory=installed) == ("start", "nightly")
    assert trigger.validate(["start", "tp-compare"], directory=installed) == ("start", "tp-compare")


def test_stop_and_status_take_no_name(installed):
    assert trigger.validate(["stop"], directory=installed) == ("stop", None)
    assert trigger.validate(["status"], directory=installed) == ("status", None)


def test_stop_works_with_nothing_installed(tmp_path):
    """Stopping must not depend on the allowlist. Being unable to stop a run because the
    thing that started it was since undeployed is the exact moment you need it most."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert trigger.validate(["stop"], directory=empty) == ("stop", None)


# --- the constants the two halves share -------------------------------------

def test_the_client_and_the_trigger_agree_on_the_paths():
    """`sparky run` composes the log path itself so that reading a log needs no privilege.
    If the two drifted, a run would start and its log would be somewhere nobody looks."""
    from sparky.measure.loop import suitectl

    assert suitectl.UNIT == trigger.UNIT
    assert suitectl.LOG_DIR == trigger.LOG_DIR
    assert suitectl.INSTALLED_DIR == trigger.SUITE_DIR
    assert suitectl.log_path("x") == trigger.LOG_DIR / "x.log"


def test_the_unit_runs_the_foreground_runner_not_the_launcher():
    """`run` is the launcher that reaches this trigger; `suite` is what actually runs. If
    the unit executed `run`, starting a suite would start a launcher that starts a
    launcher."""
    source = TRIGGER.read_text()
    assert '"suite"' in source
    assert '"run", name' not in source
