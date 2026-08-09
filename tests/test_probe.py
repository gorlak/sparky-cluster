"""The probe's decision, unit-tested (ADR-0019, ADR-0011 Layer 3).

`vllm-probe` is a grant: something holding it must not be able to reach a general
`docker run`. `validate()` is pure precisely so that claim is testable here rather
than asserted in a comment — same reasoning as the reconciler's `plan()`.

The tests that matter are the NEGATIVE ones. A probe that answers the right question
is convenient; a probe that cannot be turned into a shell is the point.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

PROBE = (Path(__file__).resolve().parent.parent
         / "ansible/roles/activate/files/vllm-probe")


def load_probe(tmp_path, images: list[str] | None = ("dgx-spark/vllm:26.07-xgrammar-fix",
                                                     "nvcr.io/nvidia/vllm@sha256:abc")):
    """Import the installed program verbatim — the thing under test is the file the
    deploy copies to /usr/local/sbin, not a reimplementation of it."""
    spec = importlib.util.spec_from_loader(
        "vllm_probe",
        importlib.machinery.SourceFileLoader("vllm_probe", str(PROBE)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    allowlist = tmp_path / "allowlist"
    if images is not None:
        allowlist.write_text("# a comment\n\n" + "\n".join(images) + "\n")
    mod.IMAGE_ALLOWLIST = allowlist
    return mod


IMG = "dgx-spark/vllm:26.07-xgrammar-fix"


def test_accepts_a_deployed_image_and_a_known_probe(tmp_path):
    m = load_probe(tmp_path)
    assert m.validate([IMG, "versions"]) == (IMG, "versions", [])


def test_passes_arguments_through_as_argv(tmp_path):
    m = load_probe(tmp_path)
    _, _, args = m.validate([IMG, "archs", "Mistral3ForConditionalGeneration",
                             "Qwen3VLForConditionalGeneration"])
    assert args == ["Mistral3ForConditionalGeneration", "Qwen3VLForConditionalGeneration"]


def test_rejects_an_image_that_was_never_deployed(tmp_path):
    """The containment boundary: probing something new is a deploy, not an argument.
    Without this, the grant runs arbitrary images as root."""
    m = load_probe(tmp_path)
    with pytest.raises(SystemExit):
        m.validate(["alpine:latest", "versions"])


def test_rejects_an_unknown_probe_name(tmp_path):
    m = load_probe(tmp_path)
    with pytest.raises(SystemExit):
        m.validate([IMG, "eval"])


def test_refuses_everything_when_the_allowlist_is_missing(tmp_path):
    """Fail closed. An unreadable allowlist must not degrade to 'allow anything'."""
    m = load_probe(tmp_path, images=None)
    with pytest.raises(SystemExit):
        m.validate([IMG, "versions"])


@pytest.mark.parametrize("evil", [
    "-v/:/host",                 # a docker flag smuggled in as an argument
    "--privileged",
    "/etc/shadow",               # a path
    "a;id",                      # shell metacharacters, were a shell ever involved
    "a b",                       # whitespace — would split into two docker args
    "$(id)",
    "`id`",
    "'",                         # the quote that breaks the engine env files (ADR-0018)
    "",
    "x" * 200,                   # unbounded length
])
def test_rejects_arguments_that_are_not_bare_identifiers(tmp_path, evil):
    m = load_probe(tmp_path)
    with pytest.raises(SystemExit):
        m.validate([IMG, "pip", evil])


def test_rejects_too_many_arguments(tmp_path):
    m = load_probe(tmp_path)
    with pytest.raises(SystemExit):
        m.validate([IMG, "archs", *[f"Arch{i}" for i in range(m.MAX_ARGS + 1)]])


def test_docker_flags_grant_no_host_access(tmp_path):
    """The flags are constants, and these absences are the security property: a probe
    with a bind mount, a device, or a network is a different tool wearing this name."""
    m = load_probe(tmp_path)
    flags = " ".join(m.DOCKER_FLAGS)
    for forbidden in ("-v", "--mount", "--volume", "--gpus", "--privileged",
                      "--cgroupns", "--ipc", "--pid"):
        assert forbidden not in m.DOCKER_FLAGS, f"{forbidden} must not be in DOCKER_FLAGS"
    for required in ("--rm", "--network", "none", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges"):
        assert required in m.DOCKER_FLAGS, f"{required} missing from DOCKER_FLAGS"
    assert "--entrypoint python3" in flags


def test_no_probe_program_takes_code_from_its_arguments(tmp_path):
    """There must be no 'run this Python' mode — that would be a shell with extra
    steps. Probe programs read argv; they never eval it."""
    m = load_probe(tmp_path)
    for name, src in m.PROBES.items():
        assert "eval(" not in src, f"probe {name} evaluates its input"
        assert "exec(" not in src, f"probe {name} execs its input"
        assert "subprocess" not in src, f"probe {name} shells out"
        assert "os.system" not in src, f"probe {name} shells out"


def test_the_probe_is_installed_and_granted_by_the_role():
    """The program is worthless uninstalled, and dangerous if granted more broadly
    than intended — assert the role does exactly one of each."""
    role = PROBE.parent.parent
    tasks = (role / "tasks/main.yml").read_text()
    sudoers = (role / "templates/sudoers-activate.j2").read_text()
    assert "src: vllm-probe" in tasks
    assert 'dest: "{{ probe_bin }}"' in tasks
    assert "NOPASSWD: {{ probe_bin }}" in sudoers
    # the grant is to the activation group, not to ALL
    assert "%{{ activate_group }} ALL=(root) NOPASSWD: {{ probe_bin }}" in sudoers
