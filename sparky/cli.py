"""sparky CLI (Typer) — the operator-facing entrypoint (ADR-0010).

Commands that touch the cluster live here; pure unit/render checks live in pytest.
`bench` / `smoke` / `test infra` join `topology` as ADR-0011 / ADR-0012 land.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from sparky import topology
from sparky.api import VllmClient

app = typer.Typer(
    help="Sparky Cluster test/bench harness (ADR-0010).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.callback()
def _root() -> None:
    """Sparky Cluster test/bench harness (ADR-0010)."""
    # Present so Typer keeps subcommand names even with a single command today.


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
def smoke() -> None:
    """Probe each live engine (from current-topology.json): readiness + tool-call shape.

    Fails (exit 1) if any engine is down or 400s the tool-call shape Open WebUI
    sends — i.e. is up but can't serve the UI it's wired to (ADR-0012).
    """
    current = topology.load_current_topology()
    if current is None:
        console.print("[yellow]No current-topology.json — nothing is deployed.[/]")
        raise typer.Exit(1)
    engines = current.get("engines", [])
    profile = current.get("profile", "?")
    if not engines:
        console.print(f"[bold]{profile}[/] — empty profile, no engines to probe")
        raise typer.Exit()

    table = Table(title=f"smoke: {profile}", title_justify="left")
    for col in ("engine", "api", "ready", "tool-shape"):
        table.add_column(col, overflow="fold")
    failed = False
    for e in engines:
        with VllmClient(e["api_url"], timeout=30.0) as client:
            ready = client.is_ready()
            if ready:
                code = client.probe_tool_support(e["served_as"]).status_code
                tool = "[green]200[/]" if code == 200 else f"[red]{code}[/]"
                ok = code == 200
            else:
                tool, ok = "—", False
        failed = failed or not ok
        table.add_row(
            e["name"], e["api_url"],
            "[green]yes[/]" if ready else "[red]no[/]", tool,
        )
    console.print(table)
    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
