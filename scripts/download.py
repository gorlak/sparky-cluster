#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["huggingface_hub[hf_xet]>=0.34"]
# ///
"""Stage a HuggingFace model into the cluster's inbox — the right way, no local `hf` needed.

`uv` provisions huggingface_hub (+ hf_transfer for fast large-file transfer) from the
inline deps above, so this does not depend on anything installed on your machine.

Run it:
    make download REPO=<hf-repo> [DEST=<dir-name>]      # the intended front end
    ./scripts/download.py <hf-repo-id> [dest-name]      # or directly (uv shebang)

Why a script: `hf download` WITHOUT --local-dir dumps a symlink cache tree; WITH
--local-dir it writes flat real files. `snapshot_download(local_dir=...)` is the library
equivalent — flat real files into the deploy-writable inbox (no sudo). The next
`make deploy` moves them into /opt/vllm/models and mirrors to every node.
"""
import os
import sys
from pathlib import Path

# Xet high-performance transfer for the 100+ GiB checkpoints — the modern hf backend
# (via the hf_xet extra); replaces the deprecated hf_transfer. Set before the import.
os.environ.setdefault("HF_XET_HIGH_PERFORMANCE", "1")

from huggingface_hub import snapshot_download  # noqa: E402

# Inbox = model_cache_dir in ansible/group_vars/all.yml. Override with $INBOX.
INBOX = Path(os.environ.get("INBOX", "/opt/cluster/model-cache"))


def human(nbytes: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if nbytes < 1024 or unit == "TiB":
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TiB"


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("usage: make download REPO=<hf-repo> [DEST=<name>]", file=sys.stderr)
        print("   or: ./scripts/download.py <hf-repo-id> [dest-name]", file=sys.stderr)
        print("  e.g. make download REPO=stepfun-ai/Step-3.7-Flash-NVFP4", file=sys.stderr)
        return 2

    repo = args[0]
    dest = args[1] if len(args) > 1 and args[1] else repo.split("/")[-1]
    target = INBOX / dest

    if not INBOX.is_dir():
        print(f"error: inbox '{INBOX}' does not exist.", file=sys.stderr)
        print(f"  create it once:  sudo install -d -o deploy -g cluster -m 2775 '{INBOX}'",
              file=sys.stderr)
        return 1

    print(f"→ {repo}\n  → {target}\n")
    snapshot_download(repo_id=repo, local_dir=str(target))

    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"\n✓ staged: {target}  ({human(size)})")
    print("\nnext:")
    print(f"  • set  model: {dest}  in a profile  (ansible/profiles/<name>.yml)")
    print("  • cd ansible && make deploy PROFILE=<name>")
    print("    → the model role moves it into /opt/vllm/models and mirrors to every node")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
