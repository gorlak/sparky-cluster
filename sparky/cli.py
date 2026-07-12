"""sparky CLI (Typer) — the operator-facing entrypoint (ADR-0010).

Commands that touch the cluster live here; pure unit/render checks live in pytest.
`bench` / `smoke` / `test infra` join `topology` as ADR-0011 / ADR-0012 land.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from sparky import topology

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


if __name__ == "__main__":
    app()
