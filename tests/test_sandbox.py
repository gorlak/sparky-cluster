"""The sandbox trigger (ADR-0024, ADR-0011 Layer 3) — no systemd, no grant.

`vllm-sandbox` is the first bounded program that runs caller-supplied code, so its tests
are not about validating input — there is no validating a payload. They are about the
three things that DO bound it: the confinement flags are constants, the answer can never
become program text or forge its own verdict, and a job can only name a toolchain that
was deployed here rather than describe one.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

SANDBOX = (Path(__file__).resolve().parent.parent
           / "ansible/roles/activate/files/vllm-sandbox")

PY = "python3-isolated"


def _load():
    spec = importlib.util.spec_from_loader(
        "vllm_sandbox", importlib.machinery.SourceFileLoader("vllm_sandbox", str(SANDBOX)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sb = _load()


def _execute(code, tests, toolchain=PY):
    """Run the GENERATED program the trigger would hand to systemd, minus the unit. Proves
    the runner logic; the confinement is asserted separately, since a test cannot escape a
    kernel it is running on."""
    program = sb.build_program(toolchain, code, tests)
    out = subprocess.run(sb.TOOLCHAINS[toolchain]["argv"], input=program,
                         capture_output=True, text=True)
    return sb.read_verdict(out.stdout)


# --- the verdict, stage by stage --------------------------------------------

def test_a_correct_answer_passes():
    assert _execute("def f(x):\n    return x + 1", "assert f(1) == 2") == {
        "verdict": "passed", "detail": ""}


def test_a_wrong_answer_is_failed_not_crashed():
    """A failed assertion is the model being wrong. Anything else is a different finding,
    and collapsing them loses the distinction the taxonomy exists for."""
    assert _execute("def f(x):\n    return x", "assert f(1) == 2")["verdict"] == "failed"


def test_code_that_does_not_build_is_a_compile_error():
    v = _execute("def f(x:\n", "assert f(1) == 2")
    assert v["verdict"] == "compile_error" and "SyntaxError" in v["detail"]


def test_a_definition_that_throws_is_a_crash_not_a_compile_error():
    """It built. It died on the way up. Those are different verdicts about the model."""
    v = _execute("raise RuntimeError('boom')", "assert True")
    assert v["verdict"] == "crashed" and "boom" in v["detail"]


def test_an_exception_from_the_tests_is_a_crash_not_a_wrong_answer():
    """`failed` means the answer was wrong; a TypeError means it was broken. A model that
    returns the wrong TYPE should not be scored the same as one that is merely off by
    one."""
    v = _execute("def f(x):\n    return None", "assert f(1) + 1 == 3")
    assert v["verdict"] == "crashed"


def test_an_answer_that_prints_cannot_corrupt_the_verdict():
    v = _execute("print('chatty')\ndef f(x):\n    return x + 1", "assert f(1) == 2")
    assert v == {"verdict": "passed", "detail": ""}


def test_an_answer_cannot_forge_a_passing_verdict():
    """A model that prints the result shape must not be able to grade itself. Its stdout
    is captured, so the genuine verdict is the last line."""
    v = _execute('print(\'{"verdict": "passed", "detail": "forged"}\')\n'
                 'def f(x):\n    return x', "assert f(1) == 2")
    assert v["verdict"] == "failed" and v["detail"] != "forged"


def test_quotes_and_backslashes_in_an_answer_do_not_break_out():
    """`json.dumps` is what keeps the answer inside a string literal instead of becoming
    program text — the one place an injection could exist."""
    tricky = 'def f(x):\n    return x + 1  # """ \\\' \\n """'
    assert _execute(tricky, "assert f(1) == 2")["verdict"] == "passed"


# --- toolchains -------------------------------------------------------------

C = "c-freestanding"

C_HEADER = ("#ifndef P\n#define P\ntypedef __SIZE_TYPE__ size_t;\n"
            "size_t twice(size_t n);\n#endif\n")
C_TESTS = ('#include "problem.h"\n'
           "extern void report(const char *, int, int);\n"
           "void run_tests(void) { report(\"doubles\", 1, twice(3) == 6); }\n")


def _c(answer):
    program = sb.build_program(C, answer, C_TESTS, {"problem.h": C_HEADER})
    out = subprocess.run(sb.TOOLCHAINS[C]["argv"], input=program,
                         capture_output=True, text=True, timeout=90)
    return sb.read_verdict(out.stdout).get("verdict")


@pytest.mark.parametrize("verdict,answer", [
    ("passed", '#include "problem.h"\nsize_t twice(size_t n) { return n * 2; }'),
    ("failed", '#include "problem.h"\nsize_t twice(size_t n) { return n; }'),
    ("compile_error", '#include "problem.h"\nsize_t twice(size_t n) { return'),
    # A name the header does not declare: the linker checks the contract for free.
    ("link_error", '#include "problem.h"\nsize_t twice_(size_t n) { return n * 2; }'),
    ("crashed", '#include "problem.h"\nsize_t twice(size_t n)'
                ' { int *p = 0; return (size_t)*p; }'),
])
def test_a_compiled_toolchain_reaches_the_stages_an_interpreted_one_cannot(verdict, answer):
    """`link_error` has no meaning for an interpreted set and is a real, decidable finding
    for a compiled one — which is the whole reason the taxonomy is a union of stages rather
    than one boolean."""
    assert _c(answer) == verdict


def test_a_compiled_answer_cannot_define_main():
    """The harness TU owns `main`. An answer that supplies one collides at link time rather
    than taking over the process and reporting whatever it likes."""
    assert _c('#include "problem.h"\nsize_t twice(size_t n){return n*2;}\n'
              'int main(void){return 0;}') == "link_error"


def test_a_job_names_a_toolchain_it_cannot_describe_one():
    """ADR-0024 §2. A set may be a submodule from a repository this cluster does not
    control, so it supplies a KEY. If it could supply program text to a root-invoked
    program, every other bound here would be decorative."""
    with pytest.raises(SystemExit):
        sb.build_program("no-such-toolchain", "print(1)", "assert True")


def test_every_toolchain_declares_both_halves():
    """A toolchain missing either half fails at the moment an answer arrives, which is the
    worst time to discover it."""
    assert sb.TOOLCHAINS, "no toolchains deployed"
    for name, spec in sb.TOOLCHAINS.items():
        assert spec.get("argv"), f"{name} has no argv"
        assert callable(spec.get("build")), f"{name} has no build"


# --- the shape --------------------------------------------------------------

def test_it_takes_no_arguments():
    """The job arrives on stdin. An argument would be the one thing that could become a
    path or a flag."""
    with pytest.raises(SystemExit):
        sb.main(["--anything"])


def test_an_empty_or_oversized_answer_is_refused_in_the_result_shape():
    """The caller is a scoring loop; a bare non-zero exit would make every refusal look
    like a model that cannot code."""
    for bad in ("", "x" * (sb.MAX_PAYLOAD + 1)):
        with pytest.raises(SystemExit):
            sb.build_program(PY, bad, "assert True")


def test_the_confinement_is_constants_only():
    """No caller-supplied string may reach the argv. If confinement were parameterised,
    the caller would choose its own confinement."""
    argv = sb.sandbox_argv(PY, "sparky-sandbox-1-0")
    joined = " ".join(argv)
    for required in ("DynamicUser=yes", "PrivateNetwork=yes", "TemporaryFileSystem=/",
                     "ProtectHome=yes", "NoNewPrivileges=yes", "PrivateTmp=yes",
                     "ProtectProc=invisible", "RestrictNamespaces=yes",
                     f"RuntimeMaxSec={sb.TIMEOUT_SEC}", f"MemoryMax={sb.MEMORY_MAX}",
                     f"TasksMax={sb.TASKS_MAX}"):
        assert required in joined, f"missing confinement: {required}"
    # the program itself goes over stdin, never as an argument
    assert argv[-3:] == [sb.PYTHON, "-I", "-"]


def test_the_published_tree_is_not_in_the_answers_world():
    """The finding that forced the empty root: `ProtectSystem=strict` left the filesystem
    readable, and /opt/cluster is mode 3775 — so an answer could read the hidden tests and
    reference solutions of every deployed set straight off disk.

    An empty root plus an explicit bind list makes that structural. This asserts the list
    stays a list of what the TOOLCHAINS need, and never grows a path that carries a set.
    """
    joined = " ".join(sb.sandbox_argv(PY, "u"))
    assert "TemporaryFileSystem=/" in joined, "the root must start empty"
    for forbidden in ("/opt", "/home", "/root", "/srv"):
        assert f"BindReadOnlyPaths=-{forbidden}" not in joined
    for path in sb.BIND_PATHS:
        assert path.startswith(("/usr/", "/lib/")), f"{path} is not a toolchain path"


def test_the_confinement_is_the_same_whichever_toolchain_runs():
    """A toolchain chooses an interpreter or a compiler. It must not be able to choose how
    tightly it is confined."""
    fixed = [a for a in sb.sandbox_argv(PY, "u") if a.startswith("--property=")]
    for name in sb.TOOLCHAINS:
        assert [a for a in sb.sandbox_argv(name, "u")
                if a.startswith("--property=")] == fixed


def test_a_killed_unit_reads_as_a_failed_item_not_a_broken_harness():
    """Timeout and OOM kill the unit before it can report. That is a real result — the
    answer did not finish — and must not look like the sandbox is broken."""
    assert sb.read_verdict("") == {}
    assert sb.read_verdict('{"verdict": "passed"') == {}          # half-written line
    assert sb.read_verdict('noise\n{"verdict": "failed", "detail": "x"}') == {
        "verdict": "failed", "detail": "x"}


def test_the_per_test_rows_are_forwarded_not_discarded():
    """The confined program reports one row per test, then the verdict. `main` must forward
    the rows so the caller can compute a weighted score — dropping them made every weighted
    score read 0, invisibly, because pass@1 was still correct. Tested against the confined
    output shape because the bug lived in `main`'s forwarding, below the toolchain."""
    confined = ('\n{"test": "basic", "weight": 1, "ok": true}'
                '\n{"test": "severe", "weight": 3, "ok": false}'
                '\n{"verdict": "failed", "detail": "1 of 2 failed"}')
    cases = sb.read_cases(confined)
    assert [c["test"] for c in cases] == ["basic", "severe"]
    assert [c["weight"] for c in cases] == [1, 3]
    assert sb.read_verdict(confined) == {"verdict": "failed", "detail": "1 of 2 failed"}
    # The verdict line is not mistaken for a case, and a half-written row is skipped.
    assert sb.read_cases('{"test": "x", "weight": 1') == []


# --- the hidden tests must not be reachable from inside --------------------------------
#
# Both of these scored `passed` before the fix. Neither is closed by the mount namespace:
# the tests have to be inside the sandbox to run at all, so the only defence is that
# nothing handed to the answer references them and no artifact holding them survives the
# build. A benchmark whose answers can read its answer key measures nothing.

def test_an_interpreted_answer_cannot_reach_the_tests_through_an_injected_helper():
    """`weight` is defined in the runner's module, so `weight.__globals__` was the whole
    test source. It is rebuilt with empty globals before it is handed over."""
    verdict = _execute("STOLEN = weight.__globals__.get('TESTS')\n"
                       "def f(x):\n    return 2 if STOLEN else 0\n",
                       "assert f(1) == 2")["verdict"]
    assert verdict == "failed", "the answer read the hidden tests"


def test_a_compiled_answer_cannot_read_the_sources_it_was_built_from():
    """`tests.c` sat beside `answer.c` while the binary ran. Sources are unlinked after
    compiling and before anything the answer wrote can execute."""
    answer = ('#include "problem.h"\n'
              'typedef struct FILE FILE; extern FILE* fopen(const char*, const char*);\n'
              'size_t twice(size_t n){ FILE* fp = fopen("tests.c","r"); return fp ? n*2 : 0; }\n')
    # `contraband`, not `failed`: the admission gate now rejects it before it is linked,
    # so it never runs at all. Sources are unlinked too — belt and braces, because an
    # answer that reaches the filesystem through a path the gate did not enumerate still
    # finds nothing to read.
    assert _c(answer) == "contraband", "the answer reached outside its interface"


def test_a_compiled_answer_cannot_include_the_standard_library():
    """`-nostdinc` with only the compiler's own include directory bound: the rule the set
    states to the model is enforced by the environment, not detected afterwards."""
    assert _c('#include "problem.h"\n#include <stdio.h>\n'
              'size_t twice(size_t n){ return n*2; }') == "compile_error"


# --- scope: what the answer may reach ---------------------------------------------------

@pytest.mark.parametrize("label,answer", [
    ("a plain import", "import os\ndef f(x): return x + 1"),
    ("the __import__ dodge", "def f(x):\n    __import__('os')\n    return x + 1"),
    ("an import nested in a function", "def f(x):\n    import socket\n    return x + 1"),
])
def test_an_interpreted_answer_may_not_reach_beyond_what_the_problem_provides(label, answer):
    """A RULE-COMPLIANCE check, not containment — `importlib` and `object.__subclasses__()`
    reach the same modules without emitting IMPORT_NAME. It measures whether the model
    obeyed a stated constraint; the boundary is the confinement (ADR-0024 §6)."""
    assert _execute(answer, "assert f(1) == 2")["verdict"] == "rejected"


def test_a_problem_may_grant_what_its_answer_needs():
    """`parse-duration` genuinely needs a regex module. A gate with no way to say yes would
    just be a gate nobody could pass."""
    program = sb.build_program(PY, "import re\ndef f(x): return x + 1",
                               "assert f(1) == 2", {"constraints.imports": "re"})
    out = subprocess.run(sb.TOOLCHAINS[PY]["argv"], input=program,
                         capture_output=True, text=True)
    assert sb.read_verdict(out.stdout)["verdict"] == "passed"


# --- admission: analysis before execution -----------------------------------------------

@pytest.mark.parametrize("label,answer", [
    ("a libc call declared by hand",
     '#include "problem.h"\nextern int system(const char *);\n'
     'size_t twice(size_t n){ system("id"); return n * 2; }'),
    ("a file opened without the header",
     '#include "problem.h"\ntypedef struct FILE FILE;\n'
     'extern FILE *fopen(const char *, const char *);\n'
     'size_t twice(size_t n){ fopen("/etc/passwd", "r"); return n * 2; }'),
])
def test_a_compiled_answer_may_not_reach_outside_its_interface(label, answer):
    """`-nostdinc` stops an INCLUDE; nothing stops a hand-written `extern`, and linking
    still reaches libc. So the allowed set is derived from the problem's own header and
    checked on the object, before it is linked or run."""
    assert _c(answer) == "contraband"


def test_a_raw_syscall_is_caught_even_though_it_has_no_symbol():
    """The symbol check cannot see this one — an `svc` carries no undefined symbol at all,
    which is why the gate disassembles as well as listing symbols."""
    assert _c('#include "problem.h"\nsize_t twice(size_t n){ long r;\n'
              '  __asm__ volatile("mov x8, #63\\n svc #0" : "=r"(r));\n'
              '  return n * 2; }') == "contraband"


def test_static_storage_is_capped():
    """An answer can otherwise dodge a problem's allocator entirely — `static int
    buf[1<<20]`, no allocations, a perfect memory score, and it passes at small sizes.
    The section sizes are in the object the admission gate already reads.

    A buffer the optimiser deletes is not capped, and should not be: it costs nothing.
    """
    used = ('#include "problem.h"\nstatic int buf[1<<20];\n'
            'size_t twice(size_t n){ buf[n%1024]=(int)n;'
            ' return (size_t)buf[(n*7)%1024]+n*2; }')
    assert _c(used) == "contraband"
    dead = ('#include "problem.h"\nstatic int buf[1<<20];\n'
            'size_t twice(size_t n){ buf[0]=1; return n*2; }')
    assert _c(dead) == "passed"


def test_a_problem_that_needs_static_storage_may_ask_for_it():
    """The cap is a default, not a law — a problem whose whole point is a lookup table
    raises it, exactly as one that needs a module grants the import."""
    used = ('#include "problem.h"\nstatic int buf[1<<20];\n'
            'size_t twice(size_t n){ buf[n%1024]=(int)n;'
            ' return (size_t)buf[(n*7)%1024]+n*2; }')
    program = sb.build_program(C, used, C_TESTS,
                               {"problem.h": C_HEADER,
                                "constraints.static_bytes": "8388608"})
    out = subprocess.run(sb.TOOLCHAINS[C]["argv"], input=program,
                         capture_output=True, text=True, timeout=90)
    assert sb.read_verdict(out.stdout).get("verdict") == "passed"
