#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["huggingface_hub[hf_xet]>=0.34"]
# ///
"""Stage a HuggingFace model into the cluster's inbox — the right way, no local `hf` needed.

`uv` provisions huggingface_hub (+ hf_transfer for fast large-file transfer) from the
inline deps above, so this does not depend on anything installed on your machine.

Run it:
    ./sparky.sh download <hf-repo>      # the intended front end
    ./scripts/download.py <hf-repo-id> [dest-name]      # or directly (uv shebang)

Why a script: `hf download` WITHOUT --local-dir dumps a symlink cache tree; WITH
--local-dir it writes flat real files. `snapshot_download(local_dir=...)` is the library
equivalent — flat real files into the deploy-writable inbox (no sudo). The next
`./sparky.sh deploy` moves them into /opt/vllm/models and mirrors to every node.
"""
import fnmatch
import os
import signal
import sys
import threading
from pathlib import Path

# THE TESTED INCANTATION. These four are exactly what was run by hand on 2026-08-11 and
# found tolerable on a shared home link — not a configuration reasoned out from first
# principles, which is why it is written down verbatim rather than improved.
#
# hf_xet has **no rate limit**: every `HF_XET_*` knob in the binary was enumerated and none
# is bytes/sec. Concurrency is the only lever, and the shipped default ramps **until
# round-trip latency degrades** (`HF_XET_CLIENT_AC_TARGET_RTT`) — correct in a datacentre,
# wrong in a house. Turning the ramp off and pinning it to one connection and one file is
# what stopped a video call dropping frames.
#
# `HIGH_PERFORMANCE` stays ON deliberately, and the pairing only looks contradictory: it
# was set during the run that behaved, and the three limits below are what actually bound
# the transfer. Do not "tidy" this without measuring — an earlier attempt to tighten it
# further (adding `max_workers=1` and turning high-performance off) was never measured
# against this, and untested strictness is not an improvement.
#
# The kernel-side answer to sharing a link is fair queueing (`cake` on ingress), which is
# host config and belongs in the `common` role. This script's job is not to be greedy.
os.environ["HF_XET_HIGH_PERFORMANCE"] = "1"
os.environ["HF_XET_CLIENT_ENABLE_ADAPTIVE_CONCURRENCY"] = "false"
os.environ["HF_XET_FIXED_DOWNLOAD_CONCURRENCY"] = "1"
os.environ["HF_XET_DATA_MAX_CONCURRENT_FILE_DOWNLOADS"] = "1"

from huggingface_hub import snapshot_download  # noqa: E402

# Inbox = model_cache_dir in ansible/group_vars/all.yml. Override with $INBOX.
INBOX = Path(os.environ.get("INBOX", "/opt/cluster/model-cache"))


HF_HOSTS = ("huggingface.co", "www.huggingface.co", "hf.co")

# --- dual-layout repos (every Mistral) ---------------------------------------
#
# Mistral publishes the SAME weights twice in one repo: a native `consolidated-*` set and
# an HF `model-*` set. Downloading both costs exactly double for nothing —
# `mistralai/Mistral-Medium-3.5-128B` is 248.9 GiB on the wire for 124.4 GiB of model.
#
# Worse than the bandwidth, it corrupts the fit maths. Summing every `*.safetensors` in
# such a repo reports 2× the real footprint, and on 2026-08-11 that arithmetic ruled three
# candidates out of a sourcing sweep as "too big" when all three fit at TP=2 with room. The
# sizing rule that follows: **size ONE layout, and read `.safetensors.parameters` (the
# dtype histogram) for the precision** — never trust a byte total or a repo's `fp8` tag.
#
# WHICH LAYOUT: native, by default. vLLM validates the tokenizer TYPE for Mistral
# architectures and refuses an HF one outright ("The tokenizer must be an instance of
# MistralTokenizer"), so these models are on the native path regardless — and the proven
# GB10 recipe is the three-flag trio `--tokenizer-mode/--config-format/--load-format
# mistral`, all of which want the native side. The HF set is the one we would never load.
#
# The reverse case is real too and is why `--layout` exists: `nvidia/…-NVFP4` ships ONLY
# the HF layout (no `consolidated.safetensors.index.json` at all), so nothing is skipped
# there and `--load-format mistral` has no index to read.
NATIVE_PREFIX = "consolidated"
NATIVE_IGNORE = ("consolidated*",)
HF_IGNORE = ("model-*.safetensors", "model.safetensors", "model.safetensors.index.json")


def _layouts(filenames) -> tuple[bool, bool]:
    """(has_native, has_hf) — judged on WEIGHT files, not on config.

    `config.json` and `params.json` often coexist innocently; it is two full sets of
    tensors that make a repo dual-layout.
    """
    native = any(n.startswith(NATIVE_PREFIX) and n.endswith(".safetensors")
                 for n in filenames)
    hf = any(n == "model.safetensors"
             or (n.startswith("model-") and n.endswith(".safetensors"))
             for n in filenames)
    return native, hf


def choose_layout(filenames, prefer: str = "native") -> tuple[str, tuple[str, ...]]:
    """Pure: (chosen_layout, ignore_patterns). The whole decision, so it is testable.

    Skips nothing unless BOTH layouts are actually present — a single-layout repo must
    never have its only weights filtered away, which would be a silent, expensive failure
    discovered at load time.
    """
    if prefer == "both":
        return "both", ()
    native, hf = _layouts(filenames)
    if not (native and hf):
        return ("native" if native else "hf" if hf else "unknown"), ()
    if prefer == "hf":
        return "hf", NATIVE_IGNORE
    return "native", HF_IGNORE


def normalize_repo(arg: str) -> str:
    """Accept a repo id OR the URL you copied out of the browser.

    Pasting the address bar is the natural gesture — it is how the operator found
    `DeepSeek-V4-Flash-0731` in the first place — and making them hand-edit it into a slug
    is an invitation to typo an org name. `deepseek-ai/X` and `nvidia/X` are different
    checkpoints, and the failure is a 404 at best.

    Handles the shapes a browser actually produces: `/tree/<rev>`, `/blob/...`, a query
    string, a fragment, a trailing slash. Anything left over is rejected rather than
    guessed at, and a non-Hub host is refused outright — silently treating
    `https://example.com/a/b` as the repo `a/b` would be worse than an error.
    """
    text = arg.strip()
    if "://" in text:
        from urllib.parse import urlparse
        url = urlparse(text)
        if url.netloc.lower() not in HF_HOSTS:
            raise ValueError(f"not a huggingface.co URL: {arg}")
        text = url.path
    text = text.strip("/")
    parts = [p for p in text.split("/") if p]
    # Drop the page suffixes the Hub puts after the repo: /tree/main, /blob/main/x, …
    for marker in ("tree", "blob", "resolve", "raw", "commit"):
        if marker in parts:
            parts = parts[:parts.index(marker)]
            break
    # A repo id is `org/name`, or a bare `name` for the canonical models that have no org.
    if not 1 <= len(parts) <= 2:
        raise ValueError(f"cannot read a repo id from: {arg}")
    return "/".join(parts)


def human(nbytes: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if nbytes < 1024 or unit == "TiB":
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TiB"


def repo_files(repo: str):
    """[(name, size)] from the Hub, or [] if it cannot be asked.

    Best-effort by design: a layout optimisation must never be the reason a download
    refuses to start. If this fails we fetch everything, which is correct, just bigger.
    """
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo, files_metadata=True)
        return [(s.rfilename, getattr(s, "size", None) or 0) for s in info.siblings]
    except Exception:  # noqa: BLE001 - see docstring
        return []


def download(repo: str, target: Path, ignore_patterns=()) -> bool:
    """Fetch on a WORKER thread so the main thread can still take a signal.

    `snapshot_download` spends its life inside hf_xet's Rust extension. CPython only runs
    a Python signal handler between bytecodes **in the main thread**, so with the download
    on that thread there is no eval loop to notice SIGINT — Ctrl-C kills the `uv run`
    parent, the interpreter is orphaned to init, and the transfer carries on unreachable.
    That is not hypothetical: it survived repeated Ctrl-C twice on 2026-08-11 and had to be
    `pkill`ed, both times mid-156 GiB.

    `join(timeout)` returns to the interpreter five times a second, which is all the
    handler needs. `os._exit` rather than `sys.exit`: SystemExit unwinds and then *waits*
    on non-daemon threads, and hf_xet's do not stop being asked nicely.

    Resuming is safe and is why a hard exit is acceptable — the writes are incremental and
    `snapshot_download` skips what is already complete.
    """
    outcome: dict = {}

    def stop(signum, _frame):
        # Runs on the main thread the moment `join` yields. Everything hf_xet spawns is a
        # THREAD of this process, so exiting the process is the whole cleanup.
        print(f"\n… stopping (signal {signum})", file=sys.stderr, flush=True)
        os._exit(130)

    signal.signal(signal.SIGINT, stop)
    # SIGTERM too, so `pkill`, `systemctl stop` and Ctrl-C all behave identically. Without
    # this the default disposition kills us anyway, but silently and with no message.
    signal.signal(signal.SIGTERM, stop)

    def run():
        try:
            # `ignore_patterns` is passed ONLY when there is something to skip, so the
            # ordinary call stays byte-for-byte the measured incantation — no kwarg at
            # its own default, which is what the defaults test guards.
            kwargs = {"repo_id": repo, "local_dir": str(target)}
            if ignore_patterns:
                kwargs["ignore_patterns"] = list(ignore_patterns)
            outcome["path"] = snapshot_download(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="download", daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(0.2)

    if "error" in outcome:
        print(f"\nerror: {outcome['error']}", file=sys.stderr)
        return False
    return "path" in outcome


def main() -> int:
    argv = sys.argv[1:]
    prefer, args, i = "native", [], 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--layout="):
            prefer = a.split("=", 1)[1]
        elif a == "--layout":
            i += 1
            prefer = argv[i] if i < len(argv) else ""
        else:
            args.append(a)
        i += 1
    if prefer not in ("native", "hf", "both"):
        print(f"error: --layout must be native|hf|both, not {prefer!r}", file=sys.stderr)
        return 2

    if not args or args[0] in ("-h", "--help"):
        print("usage: ./sparky.sh download <hf-repo> [dest-name] [--layout native|hf|both]",
              file=sys.stderr)
        print("   or: ./scripts/download.py <hf-repo-id> [dest-name]", file=sys.stderr)
        print("  e.g. ./sparky.sh download stepfun-ai/Step-3.7-Flash-NVFP4", file=sys.stderr)
        print("   or: ./sparky.sh download https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731",
              file=sys.stderr)
        print("\n  --layout applies only to repos carrying BOTH weight layouts (every", file=sys.stderr)
        print("  Mistral): `consolidated-*` native AND `model-*` HF, the same weights twice.",
              file=sys.stderr)
        print("  Default `native` — vLLM refuses an HF tokenizer for Mistral architectures,",
              file=sys.stderr)
        print("  so the HF set is the one we would never load. Single-layout repos are", file=sys.stderr)
        print("  untouched: nothing is ever skipped unless both sets are present.", file=sys.stderr)
        return 2

    try:
        repo = normalize_repo(args[0])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    dest = args[1] if len(args) > 1 and args[1] else repo.split("/")[-1]
    target = INBOX / dest

    if not INBOX.is_dir():
        print(f"error: inbox '{INBOX}' does not exist.", file=sys.stderr)
        print(f"  create it once:  sudo install -d -o deploy -g cluster -m 2775 '{INBOX}'",
              file=sys.stderr)
        return 1

    print(f"→ {repo}\n  → {target}")
    print(f"  pid {os.getpid()} · Ctrl-C to stop (resumes from what is on disk)")
    print("  one connection, one file at a time — so the link stays usable")

    listing = repo_files(repo)
    chosen, ignore = choose_layout([n for n, _ in listing], prefer)
    if ignore:
        # fnmatch, because these are the same globs handed to snapshot_download —
        # approximating them with string surgery is how the report drifts from the fetch.
        skipped = sum(sz for n, sz in listing
                      if any(fnmatch.fnmatch(n, p) for p in ignore))
        kept = sum(sz for n, sz in listing) - skipped
        print(f"  dual-layout repo → taking the {chosen} weights, skipping the other set")
        print(f"  {human(kept)} instead of {human(kept + skipped)} — {human(skipped)} not fetched")
    elif chosen in ("native", "hf"):
        print(f"  single-layout repo ({chosen}) — fetching everything")
    print()
    if not download(repo, target, ignore):
        staged = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        print(f"\n✗ stopped — {human(staged)} staged. Re-run the same command to resume.",
              file=sys.stderr)
        return 130

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"\n✓ staged: {target}  ({human(size)})")
    print("\nnext:")
    print(f"  • set  model: {dest}  in a profile  (ansible/profiles/<name>.yml)")
    print("  • cd ansible && ./sparky.sh deploy <name>")
    print("    → the model role moves it into /opt/vllm/models and mirrors to every node")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
