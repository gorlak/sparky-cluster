"""sparky CLI (Typer) — the single operator entrypoint (ADR-0015).

Cluster lifecycle (deploy / check / teardown / status / logs), the harness
(topology / smoke / bench / report), and dev tasks (test / lint / download) — all
here. sparky is the outer layer; ansible is the engine it drives (sparky/ansible.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sparky import ansible as ops
from sparky import report, topology
from sparky.api import VllmClient
from sparky.bench import run_all
from sparky.multiturn import run_multiturn
from sparky.store import Store

app = typer.Typer(
    help="Sparky Cluster — operator entrypoint (ADR-0015).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.callback()
def _root() -> None:
    """Sparky Cluster — operator entrypoint (ADR-0015)."""


@app.command("topology")
def show_topology(
    profile: str = typer.Argument(
        "step-3.5-fp8", help="Profile name (or a path to a profile YAML)."
    ),
) -> None:
    """Show a profile's serving topology — engines, nodes, ports, served names."""
    p = topology.load_profile(profile)
    if p.is_empty:
        console.print(f"[bold]{p.name}[/] — empty profile (no engines serving)")
        raise typer.Exit()

    title = p.name + (f"   image: {p.vllm_image}" if p.vllm_image else "")
    table = Table(title=title, title_justify="left")
    for col in ("engine", "served as", "nodes (rank0=API)", "TP", "port", "model", "gmu", "max_len"):
        table.add_column(col, overflow="fold")
    for e in p.engines:
        nodes = "+".join(e.nodes) + (f"  API:{e.api_node}" if e.is_multinode else "")
        table.add_row(
            e.name, e.served_as, nodes, str(e.tensor_parallel_size),
            str(e.port), e.model, f"{e.gpu_memory_utilization:g}", str(e.max_model_len),
        )
    console.print(table)


@app.command("smoke")
def smoke(
    topology_file: str = typer.Option(
        None, "--topology",
        help="Topology JSON to probe (default: the live current-topology.json). The deploy "
        "gate passes the in-flight topology so it's validated before being recorded.",
    ),
) -> None:
    """Post-deploy gate: probe each live engine (from current-topology.json) for
    readiness, the tool-call shape Open WebUI sends, and multiturn output quality.

    Fails (exit 1) if any engine is down, 400s the tool-call shape (up but can't
    serve the UI it's wired to), or trips a corruption heuristic across the
    conversation (ADR-0012). ~2 min per engine — that's the deploy-gate budget.
    """
    path = Path(topology_file) if topology_file else topology.CURRENT_TOPOLOGY
    current = topology.load_current_topology(path)
    if current is None:
        console.print(f"[yellow]No topology at {path} — nothing to probe.[/]")
        raise typer.Exit(1)
    engines = current.get("engines", [])
    profile = current.get("profile", "?")
    if not engines:
        console.print(f"[bold]{profile}[/] — empty profile, no engines to probe")
        raise typer.Exit()

    table = Table(title=f"smoke: {profile}", title_justify="left")
    for col in ("engine", "api", "ready", "tool-shape", "quality"):
        table.add_column(col, overflow="fold")

    failed = False
    for e in engines:
        tool, quality, ok = "—", "—", False
        with VllmClient(e["api_url"], timeout=120.0) as client:
            ready = client.is_ready()
            if ready:
                code = client.probe_tool_support(e["served_as"]).status_code
                tool = "[green]200[/]" if code == 200 else f"[red]{code}[/]"
                ok = code == 200
                if ok:
                    result = run_multiturn(client, e["served_as"])
                    ok = result.ok
                    if result.ok:
                        quality = "[green]pass[/]"
                    else:
                        reasons = sorted({r for t in result.failures for r in t.verdict.reasons})
                        quality = "[red]FAIL: " + ",".join(reasons) + "[/]"
        failed = failed or not ok
        table.add_row(
            e["name"], e["api_url"],
            "[green]yes[/]" if ready else "[red]no[/]", tool, quality,
        )

    console.print(table)
    if failed:
        raise typer.Exit(1)


@app.command("bench")
def bench(
    label: str = typer.Argument(..., help="Label for this run (e.g. '26.04', 'nvfp4', 'prefix-on')."),
) -> None:
    """Run the vllm bench serve scenarios against the live engine(s); record to the trend store.

    Several minutes per engine (latency + throughput + prefix_cache). Reads the
    engine's container/served-name from current-topology.json (ADR-0012).
    """
    current = topology.load_current_topology()
    if current is None:
        console.print("[yellow]No current-topology.json — nothing is deployed.[/]")
        raise typer.Exit(1)
    engines = [e for e in current.get("engines", []) if e.get("api_url")]
    if not engines:
        console.print("[yellow]No API engines to benchmark.[/]")
        raise typer.Exit(1)
    profile = current.get("profile", "?")
    with Store() as store:
        for e in engines:
            console.print(f"[bold]benchmarking {e['name']} as '{label}'[/] — 3 scenarios, several minutes…")
            for run in run_all(label, e, store, profile):
                console.print(f"  {run.scenario}: output {run.output_toks_s} tok/s, ttft p99 {run.ttft_p99_ms} ms")
    console.print(f"[green]recorded '{label}'[/] — compare with:  sparky report {label} <other>")


@app.command("report")
def report_cmd(
    label_a: str = typer.Argument(..., help="Baseline label."),
    label_b: str = typer.Argument(..., help="Comparison label."),
) -> None:
    """Compare two benchmark labels from the trend store (direction-aware A/B)."""
    with Store() as store:
        comparison = report.compare(store, label_a, label_b)
    report.render(console, label_a, label_b, comparison)


# --- cluster lifecycle (ADR-0015: sparky drives ansible) --------------------

@app.command()
def deploy(profile: str = typer.Argument(ops.DEFAULT_PROFILE, help="Profile to apply.")) -> None:
    """Publish the repo and apply a profile (ansible site.yml). Restarts only changed units."""
    raise typer.Exit(ops.deploy(profile))


@app.command()
def check(profile: str = typer.Argument(ops.DEFAULT_PROFILE, help="Profile to dry-run.")) -> None:
    """Dry-run a profile deploy (--check --diff) — shows what would change, makes nothing."""
    raise typer.Exit(ops.deploy(profile, dry_run=True))


@app.command()
def teardown(
    profile: str = typer.Argument(ops.DEFAULT_PROFILE),
    webui: bool = typer.Option(False, "--webui", help="also stop Open WebUI"),
) -> None:
    """Stop + disable vLLM engines on both nodes (frees the GPUs)."""
    raise typer.Exit(ops.teardown(profile, include_webui=webui))


@app.command()
def status() -> None:
    """systemd state of the vLLM units on both nodes."""
    raise typer.Exit(ops.status())


@app.command()
def logs(node: str = typer.Argument("head", help="head | worker")) -> None:
    """Follow the vLLM journal on a node."""
    raise typer.Exit(ops.logs(node))


@app.command()
def lint() -> None:
    """Ansible syntax-check across every profile + teardown (ADR-0011 Layer 1)."""
    raise typer.Exit(ops.lint())


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def test(ctx: typer.Context) -> None:
    """Run the harness unit tests (pytest). Extra args pass through (e.g. -k name, -x)."""
    import pytest

    os.chdir(ops.REPO_ROOT)
    raise typer.Exit(int(pytest.main(list(ctx.args))))


@app.command()
def download(
    repo: str = typer.Argument(..., help="HF repo id, e.g. stepfun-ai/Step-3.5-Flash-FP8."),
    dest: str = typer.Argument(None, help="Optional dir name in the inbox."),
) -> None:
    """Stage a HuggingFace model into the inbox (scripts/download.py via uv)."""
    import subprocess

    script = ops.REPO_ROOT / "scripts" / "download.py"
    cmd = ["uv", "run", "--script", str(script), repo] + ([dest] if dest else [])
    raise typer.Exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    app()
