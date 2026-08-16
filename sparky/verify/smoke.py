"""The activation gate — verify a freshly-activated model actually works (ADR-0012).

Not a measurement: it asks "did serving come up right", not "how good is this model".
`activate` runs it in-process at the end of every activation, so it lives in the verify
tier, beside the sanity probes it aggregates — and out of the CLI, which was only ever its
accidental home (ADR-0027). It reaches *down* to the sanity probes and the client; nothing
in the measure stack is involved.

The three checks are independent capabilities, deliberately not chained:

- **ready** — the engine answers at all.
- **tool-shape** — it serves the one tool-call shape Open WebUI sends. A profile with no
  tool parser answers `/v1/models` and then 400s the instant the UI attaches a tool; that
  shipped once, and this is the check that would have caught it.
- **text sanity** — a benign multiturn conversation comes back coherent, not corrupted.
- **vision sanity** — a model that claims vision can actually see an image.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from sparky.foundation import topology
from sparky.foundation.api import VllmClient
from sparky.verify.text_sanity import run_multiturn
from sparky.verify.vision_sanity import probe as vision_probe

console = Console()

# The activation-gate breadcrumb. The reconciler deletes it at the start of every
# activation, so a stale result can never be read as a verdict on the live model.
SMOKE_REPORT = Path("/opt/cluster/last-smoke.json")


def run(topology_file: str | None, report_file: str | None) -> int:
    """The gate itself, as a return code — `activate` runs it in-process."""
    path = Path(topology_file) if topology_file else topology.CURRENT_TOPOLOGY
    current = topology.load_current_topology(path)
    if current is None:
        console.print(f"[yellow]No topology at {path} — nothing to probe.[/]")
        return 1
    engines = current.get("engines", [])
    profile = current.get("profile", "?")
    if not engines:
        console.print(f"[bold]{profile}[/] — nothing serving, no engines to probe")
        return 0

    table = Table(title=f"smoke: {profile}", title_justify="left")
    for col in ("engine", "api", "ready", "tool-shape", "quality", "vision"):
        table.add_column(col, overflow="fold")

    failed = False
    results = []
    for e in engines:
        tool, quality, ok = "—", "—", False
        tool_code, quality_str = None, "skipped"
        vision_cell, vision_str, vision_ok = "—", "skipped", True
        with VllmClient(e["api_url"], timeout=120.0) as client:
            ready = client.is_ready()
            if ready:
                # Vision runs on READINESS, not on the tool/quality chain: a model can
                # lack tool flags and still be asked to look at an image, and a VL model
                # that serves text perfectly is exactly the case this catches.
                v = vision_probe(client, e["served_as"])
                vision_ok = v.ok
                vision_cell = ("[dim]n/a[/]" if v.unsupported
                               else "[green]pass[/]" if v.ok else f"[red]{v.detail}[/]")
                vision_str = v.detail
                code = client.probe_tool_support(e["served_as"]).status_code
                tool_code = code
                tool = "[green]200[/]" if code == 200 else f"[red]{code}[/]"
                ok = code == 200
                # Quality runs on READINESS, not on tool support. It used to be chained
                # behind a 200 here, which meant any model without the tool flags — every
                # minimal-flag first bring-up — silently skipped its output-quality check
                # and showed "—". Tool calling and coherent prose are independent
                # capabilities; a model can lack the first and still be the thing we are
                # deciding whether to serve.
                if True:
                    result = run_multiturn(client, e["served_as"])
                    ok = ok and result.ok
                    if result.ok:
                        quality = "[green]pass[/]"
                        quality_str = "pass"
                    else:
                        reasons = sorted({r for t in result.failures for r in t.verdict.reasons})
                        quality = "[red]FAIL: " + ",".join(reasons) + "[/]"
                        quality_str = "fail: " + ",".join(reasons)
        failed = failed or not ok or not vision_ok
        results.append({"name": e["name"], "api_url": e["api_url"], "ready": ready,
                        "tool_shape": tool_code, "quality": quality_str,
                        "vision": vision_str, "ok": ok and vision_ok})
        table.add_row(
            e["name"], e["api_url"],
            "[green]yes[/]" if ready else "[red]no[/]", tool, quality, vision_cell,
        )

    console.print(table)
    if report_file:
        out = Path(report_file)
        # Unlink first: the reconciler and other members of the cluster group take
        # turns owning this file, and only the DIRECTORY is group-writable.
        out.unlink(missing_ok=True)
        out.write_text(json.dumps(
            {"profile": profile, "ran_at": datetime.now(timezone.utc).isoformat(),
             "ok": not failed, "engines": results}, indent=2) + "\n")
    return 1 if failed else 0
