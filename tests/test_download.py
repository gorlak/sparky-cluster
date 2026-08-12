"""`scripts/download.py` must be stoppable (2026-08-11).

A 156 GiB fetch survived repeated Ctrl-C twice and had to be `pkill`ed, both times
mid-transfer. The cause is not hf_xet's: `snapshot_download` was called on the MAIN
thread, and CPython only runs a Python signal handler between bytecodes in that thread.
While it sits inside a Rust extension there is no eval loop to notice SIGINT — so the
`uv run` parent dies, the interpreter is orphaned to init, and the download continues
with no way to reach it from the terminal that started it.

These tests need no network: the downloader is a stand-in that blocks the way the real
one does.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "download.py"


# The script declares its deps inline for `uv` and imports huggingface_hub at module
# scope. The test regiment runs with no network and no such install (ADR-0011: seconds, no
# hardware), so a stub stands in — we are testing OUR signal handling, not theirs.
_STUB = textwrap.dedent("""
    import sys, types
    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = lambda **kw: None
    sys.modules.setdefault("huggingface_hub", hub)
""")


def _install_stub():
    import types
    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = lambda **kw: None
    sys.modules.setdefault("huggingface_hub", hub)


def _module():
    """Load the script without running it. It has a `uv` shebang and inline deps, so it
    is imported by path rather than as a package."""
    _install_stub()
    spec = importlib.util.spec_from_file_location("download_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["download_script"] = module
    spec.loader.exec_module(module)
    return module


def test_the_fetch_does_not_run_on_the_main_thread():
    """THE regression, asserted structurally: the main thread must stay in the interpreter
    so a signal can be delivered. If `download()` ever calls `snapshot_download` directly,
    Ctrl-C silently stops working again and nothing else here would catch it."""
    source = SCRIPT.read_text()
    body = source[source.index("def download("):source.index("def main(")]
    assert "threading.Thread" in body and "daemon=True" in body
    assert "worker.join(" in body, "the main thread must yield to the eval loop"
    assert "os._exit" in body, "sys.exit waits on threads that will not stop"


def test_both_interrupt_signals_are_handled():
    """So Ctrl-C, `pkill` and a future `systemctl stop` behave identically."""
    module = _module()
    source = SCRIPT.read_text()
    assert "signal.SIGINT" in source and "signal.SIGTERM" in source
    assert callable(module.download)


def test_a_blocked_download_still_dies_on_sigint(tmp_path):
    """End to end, in a real subprocess: a downloader that ignores everything and sleeps —
    exactly what a native extension looks like from Python — must still be killable by the
    signal Ctrl-C sends, and must exit promptly rather than after the transfer.
    """
    harness = tmp_path / "harness.py"
    harness.write_text(textwrap.dedent(f"""
        import importlib.util, sys, time, types
        hub = types.ModuleType("huggingface_hub")
        hub.snapshot_download = lambda **kw: None
        sys.modules["huggingface_hub"] = hub
        spec = importlib.util.spec_from_file_location("d", {str(SCRIPT)!r})
        d = importlib.util.module_from_spec(spec)
        sys.modules["d"] = d
        spec.loader.exec_module(d)
        # READY is printed from INSIDE the stub, which `download()` only reaches after it
        # has installed both signal handlers — so "READY" now PROVES the handlers are
        # armed. Printing it before `download()` (as this did until 2026-08-12) is a race:
        # the signal can arrive while the default disposition is still in place, and the
        # process dies with -15 instead of the handler's 130. Microseconds when the box is
        # idle; wide enough to fail during a live vLLM compile.
        def _blocked(**kw):
            print("READY", flush=True)
            time.sleep(600)          # never returns, like a 156 GiB fetch
        d.snapshot_download = _blocked
        d.download("fake/repo", {str(tmp_path)!r})
    """))
    proc = subprocess.Popen([sys.executable, str(harness)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "READY"
        proc.send_signal(signal.SIGINT)
        # Generous, but orders of magnitude below the 600s sleep: if the handler is not
        # reached, this times out rather than passing slowly.
        assert proc.wait(timeout=15) == 130
    finally:
        if proc.poll() is None:
            proc.kill()


def test_sigterm_kills_it_too(tmp_path):
    """`pkill -f scripts/download.py` was the only thing that worked on the day, and it
    must keep working — deliberately, not by falling through to the default disposition."""
    harness = tmp_path / "harness.py"
    harness.write_text(textwrap.dedent(f"""
        import importlib.util, sys, time, types
        hub = types.ModuleType("huggingface_hub")
        hub.snapshot_download = lambda **kw: None
        sys.modules["huggingface_hub"] = hub
        spec = importlib.util.spec_from_file_location("d", {str(SCRIPT)!r})
        d = importlib.util.module_from_spec(spec)
        sys.modules["d"] = d
        spec.loader.exec_module(d)
        # Printed from inside the stub — see the SIGINT test above: READY must mean "the
        # handlers are installed", not "the process started".
        def _blocked(**kw):
            print("READY", flush=True)
            time.sleep(600)
        d.snapshot_download = _blocked
        d.download("fake/repo", {str(tmp_path)!r})
    """))
    proc = subprocess.Popen([sys.executable, str(harness)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "READY"
        proc.terminate()
        assert proc.wait(timeout=15) == 130
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_failed_download_is_reported_not_swallowed(tmp_path):
    """An exception on the worker thread would otherwise vanish — the thread dies, the
    main loop sees `is_alive()` go false, and a failure reads as success."""
    module = _module()
    module.snapshot_download = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    assert module.download("fake/repo", tmp_path) is False


def test_the_tested_incantation_is_what_ships():
    """These four are exactly what was run by hand on 2026-08-11 and found tolerable on a
    shared home link. hf_xet has no rate limit — concurrency is the only lever, and its
    default ramps until latency degrades (`HF_XET_CLIENT_AC_TARGET_RTT`).

    Pinned verbatim, including `HIGH_PERFORMANCE=1`, which looks contradictory beside the
    limits and is kept because it is what was *measured*. An earlier tightening (high
    performance off, `max_workers=1`) was never measured against it; untested strictness
    is not an improvement, and this test is here to stop the next tidy-up.
    """
    _install_stub()
    module = _module()
    assert os.environ["HF_XET_HIGH_PERFORMANCE"] == "1"
    assert os.environ["HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY"] == "false"
    assert os.environ["HF_XET_FIXED_DOWNLOAD_CONCURRENCY"] == "1"
    assert os.environ["HF_XET_DATA_MAX_CONCURRENT_FILE_DOWNLOADS"] == "1"
    # assigned, not setdefault — an inherited env var must not silently restore the ramp
    assert 'setdefault("HF_XET' not in SCRIPT.read_text()


def test_snapshot_download_is_left_at_its_own_defaults(tmp_path):
    """`max_workers` is deliberately NOT passed: it was not part of the incantation that
    was measured, and the xet-level file limit already bounds files in flight.

    Asserted on the CALL rather than by grepping the file — the reasoning above mentions
    `max_workers` by name, and a test that cannot tell prose from code is a test that
    forbids explaining itself.
    """
    module = _module()
    seen = {}
    module.snapshot_download = lambda **kw: seen.update(kw) or str(tmp_path)
    assert module.download("fake/repo", tmp_path) is True
    assert set(seen) == {"repo_id", "local_dir"}, f"unexpected kwargs: {sorted(seen)}"


def test_ignore_patterns_are_passed_only_when_there_is_something_to_skip(tmp_path):
    """The other half of the guard above. A dual-layout repo MUST filter, and a
    single-layout one must not pass the kwarg at all — `ignore_patterns=None` would be a
    no-op that still widens the measured call surface for every download we do."""
    module = _module()
    seen = {}
    module.snapshot_download = lambda **kw: seen.update(kw) or str(tmp_path)
    assert module.download("fake/repo", tmp_path, module.HF_IGNORE) is True
    assert seen.get("ignore_patterns") == list(module.HF_IGNORE)


# --- accepting what the browser gives you ------------------------------------

def test_a_pasted_hub_url_becomes_a_repo_id():
    """Pasting the address bar is the natural gesture — it is how `-0731` was found —
    and hand-editing a URL into a slug is an invitation to typo the org. `deepseek-ai/X`
    and `nvidia/X` are different checkpoints."""
    module = _module()
    want = "deepseek-ai/DeepSeek-V4-Flash-0731"
    for given in (
        want,
        f"https://huggingface.co/{want}",
        f"http://huggingface.co/{want}",
        f"https://www.huggingface.co/{want}",
        f"https://hf.co/{want}",
        f"https://huggingface.co/{want}/",
        f"  https://huggingface.co/{want}  ",
        f"https://huggingface.co/{want}/tree/main",
        f"https://huggingface.co/{want}/blob/main/config.json",
        f"https://huggingface.co/{want}?library=transformers",
        f"https://huggingface.co/{want}#usage",
    ):
        assert module.normalize_repo(given) == want, given


def test_a_canonical_model_without_an_org_still_works():
    """`bert-base-uncased` and friends have no org segment."""
    module = _module()
    assert module.normalize_repo("bert-base-uncased") == "bert-base-uncased"
    assert module.normalize_repo("https://huggingface.co/bert-base-uncased") == "bert-base-uncased"


def test_a_non_hub_url_is_refused_not_guessed_at():
    """Silently reading `https://example.com/a/b` as the repo `a/b` is worse than an
    error — it would fetch a real but wrong model if those names happen to exist."""
    module = _module()
    for hostile in ("https://example.com/deepseek-ai/DeepSeek-V4-Flash-0731",
                    "https://huggingface.co.evil.test/a/b"):
        try:
            module.normalize_repo(hostile)
        except ValueError:
            continue
        raise AssertionError(f"accepted a non-Hub URL: {hostile}")


def test_something_that_is_not_a_repo_id_is_refused():
    module = _module()
    for bad in ("", "https://huggingface.co/", "a/b/c/d", "https://huggingface.co/a/b/c/d"):
        try:
            module.normalize_repo(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted: {bad!r}")


# --- dual-layout repos (2026-08-11) -----------------------------------------
#
# Mistral publishes the same weights twice in one repo — a native `consolidated-*` set and
# an HF `model-*` set. Fetching both doubles the transfer for nothing, and summing all the
# `*.safetensors` reports twice the real footprint: on 2026-08-11 that arithmetic ruled
# three candidates out of a sourcing sweep as "too big" when every one of them fit.

MISTRAL_DUAL = [                     # mistralai/Mistral-Medium-3.5-128B
    "config.json", "params.json", "tekken.json", "tokenizer.json",
    "consolidated.safetensors.index.json", "consolidated-00001-of-00013.safetensors",
    "model.safetensors.index.json", "model-00001-of-00051.safetensors",
]
NATIVE_ONLY = [                      # mistralai/Mistral-Small-4-119B-2603-NVFP4
    "params.json", "tekken.json",
    "consolidated.safetensors.index.json", "consolidated-00001-of-00013.safetensors",
]
HF_ONLY = [                          # nvidia/Mistral-Medium-3.5-128B-NVFP4
    "config.json", "hf_quant_config.json", "params.json", "tekken.json",
    "model.safetensors.index.json", "model-00001-of-00044.safetensors",
]


def test_a_dual_layout_repo_takes_the_native_set_by_default():
    """vLLM refuses an HF tokenizer for Mistral architectures, so these models are on the
    native path regardless — the HF set is the one we would never load."""
    module = _module()
    chosen, ignore = module.choose_layout(MISTRAL_DUAL)
    assert chosen == "native"
    assert any(fnmatch.fnmatch("model-00001-of-00051.safetensors", p) for p in ignore)
    assert any(fnmatch.fnmatch("model.safetensors.index.json", p) for p in ignore)
    assert not any(fnmatch.fnmatch("consolidated-00001-of-00013.safetensors", p)
                   for p in ignore), "never skip the set we are keeping"


def test_layout_hf_skips_the_native_set_instead():
    module = _module()
    chosen, ignore = module.choose_layout(MISTRAL_DUAL, prefer="hf")
    assert chosen == "hf"
    assert any(fnmatch.fnmatch("consolidated-00001-of-00013.safetensors", p) for p in ignore)
    assert not any(fnmatch.fnmatch("model-00001-of-00051.safetensors", p) for p in ignore)


def test_layout_both_skips_nothing():
    module = _module()
    assert module.choose_layout(MISTRAL_DUAL, prefer="both") == ("both", ())


def test_a_single_layout_repo_never_has_its_only_weights_skipped():
    """THE failure this must never cause. Filtering the only weight set would download a
    config-shaped directory that fails at load time, long after the transfer is gone —
    and `nvidia/…-NVFP4` (HF-only) still carries `params.json` and `tekken.json`, so a
    naive 'is this a Mistral?' test would wrongly call it dual-layout."""
    module = _module()
    for filenames, expected in ((NATIVE_ONLY, "native"), (HF_ONLY, "hf")):
        chosen, ignore = module.choose_layout(filenames)
        assert chosen == expected
        assert ignore == (), f"{expected}-only repo must skip nothing"
        for prefer in ("native", "hf", "both"):
            assert module.choose_layout(filenames, prefer=prefer)[1] == () or prefer == "both"


def test_config_files_alone_do_not_make_a_repo_dual_layout():
    """`config.json` and `params.json` coexist innocently in most Mistral repos. It is two
    full sets of TENSORS that make it dual, and judging on config would skip real weights."""
    module = _module()
    chosen, ignore = module.choose_layout(
        ["config.json", "params.json", "tekken.json", "model-00001-of-00044.safetensors"])
    assert (chosen, ignore) == ("hf", ())
