"""sparky CLI (Typer) — the single operator entrypoint (ADR-0015).

Two operations, two privilege levels (ADR-0018):

  **deploy** — converge the whole FLEET to the allowlist. Privileged, human,
  password-gated, occasional. It sets the boundary of what may run and is
  selection-neutral: it never changes what's serving.

  **activate** — make one already-deployed model the live one. The only operation
  that changes what's serving, and it needs **no root**.

Plus the harness (topology / smoke / bench / report) and dev tasks (test / lint /
download). sparky is the outer layer; ansible is the engine `deploy` drives
(sparky/ansible.py), and the reconciler is what `activate` triggers
(sparky/activate.py).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sparky import activate as act
from sparky import ansible as ops
from sparky import report, topology
from sparky.vision import probe as vision_probe
from sparky.api import VllmClient
from sparky.bench import run_all
from sparky.fleet import load_fleet
from sparky.multiturn import run_multiturn
from sparky.store import Store

app = typer.Typer(
    help="Sparky Cluster — operator entrypoint (ADR-0015).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


PROBE_BIN = "/usr/local/sbin/vllm-probe"


def _live_image() -> str | None:
    """The image to probe when none is named: the live profile's, else the one the most
    profiles pin. Falling back matters — you probe most often with nothing activated,
    which is exactly when you are deciding what to bring up."""
    live = act.live_profile()
    if live:
        try:
            img = topology.load_profile(live).vllm_image
            if img:
                return img
        except Exception:
            pass
    from collections import Counter
    try:
        images = Counter(p.vllm_image for p in topology.all_profiles() if p.vllm_image)
    except Exception:
        return None
    return images.most_common(1)[0][0] if images else None


@app.callback()
def _root() -> None:
    """Sparky Cluster — operator entrypoint (ADR-0015)."""


@app.command("topology")
def show_topology(
    profile: str = typer.Argument(
        "empty", help="Profile name (or a path to a profile YAML)."
    ),
) -> None:
    """Show a profile's serving topology — engines, nodes, ports, served names."""
    p = topology.load_profile(profile)
    if p.is_empty:
        console.print(f"[bold]{p.name}[/] — empty profile (no engines serving)")
        raise typer.Exit()

    title = p.name + (f"   image: {p.vllm_image}" if p.vllm_image else "")
    if p.blocked:
        title += "   [yellow](blocked — weights kept, not activatable)[/]"
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


# The activation-gate breadcrumb. The reconciler deletes it at the start of every
# activation, so a stale result can never be read as a verdict on the live model.
SMOKE_REPORT = Path("/opt/cluster/last-smoke.json")


def _panel_unreachable(json_out: bool) -> int:
    """Report a missing panel usefully, and execute nothing.

    Ordered by what you actually want to know, which is NOT "why is the panel down":
    the panel is a status surface, so its being down says nothing about whether the
    cluster is serving. That question comes first, and it is answerable in one line
    with no panel, no sudo and no ansible. Node names come from what `deploy` recorded,
    so this stays right as the Peanuts roster grows.
    """
    fleet = act.fleet_state() or {}
    endpoint = fleet.get("model_endpoint")
    local = socket.gethostname().split(".")[0]
    nodes = [n["node"] for n in fleet.get("nodes", [])] or [local, ops.WORKER_HOST]
    # This node first: it needs no ssh, so it is the one that still answers when the
    # network is the thing that's broken.
    nodes = sorted(dict.fromkeys(nodes), key=lambda n: n != local)
    probes = [f"{act.ACTIVATE_BIN} --status" if n == local
              else f"ssh {n} {act.ACTIVATE_BIN} --status" for n in nodes]

    if json_out:
        console.print_json(data={
            "error": "control panel unreachable", "waited_seconds": ops.PANEL_TIMEOUT,
            "hint": "the panel is a status surface — this says nothing about whether "
                    "the cluster is serving",
            "is_it_serving": f"curl -s {endpoint}/health" if endpoint else None,
            "per_node_state": probes,
            "diagnose_panel": ["systemctl is-active control-panel",
                               "journalctl -u control-panel -n 50 --no-pager"],
            "repair": "./sparky.sh deploy"})
        return 2

    console.print(f"[red]control panel unreachable[/] at {ops.CONTROL_PANEL_URL} "
                  f"(waited {ops.PANEL_TIMEOUT:g}s)")
    console.print("[dim]The panel only reports status — this says nothing about whether "
                  "the cluster is serving.[/]")
    if endpoint:
        console.print("\n[bold]Is it still serving?[/]")
        console.print(f"  curl -s {endpoint}/health")
    console.print("\n[bold]Per-node engine state[/] [dim](no sudo — reading status "
                  "was never privileged)[/]")
    for probe in probes:
        console.print(f"  {probe}")
    console.print("\n[bold]Why is the panel down?[/]")
    console.print("  systemctl is-active control-panel")
    console.print("  journalctl -u control-panel -n 50 --no-pager")
    console.print("\n[bold]Repair[/]")
    console.print("  ./sparky.sh deploy")
    return 2


def _smoke(topology_file: str | None, report_file: str | None) -> int:
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


@app.command("smoke")
def smoke(
    topology_file: str = typer.Option(
        None, "--topology", help="Topology JSON to probe (default: the live current-topology.json)."),
    report_file: str = typer.Option(
        str(SMOKE_REPORT), "--report",
        help="Write the gate result (per-engine ready/tool-shape/quality + overall ok) to this "
             "JSON path — a durable breadcrumb, written pass OR fail."),
) -> None:
    """Activation gate: probe each live engine for readiness, the tool-call shape Open
    WebUI sends, and multiturn output quality.

    Fails (exit 1) if any engine is down, 400s the tool-call shape (up but can't
    serve the UI it's wired to), or trips a corruption heuristic across the
    conversation (ADR-0012). ~2 min per engine. `sparky activate` runs it for you
    once the engines answer; run it standalone to re-check without re-activating.
    """
    raise typer.Exit(_smoke(topology_file, report_file))


@app.command("bench")
def bench(
    label: str = typer.Argument(..., help="Label for this run (e.g. '26.04', 'nvfp4', 'prefix-on')."),
) -> None:
    """Run the vllm bench serve scenarios against the live engine(s); record to the trend store.

    Several minutes per engine (latency + throughput + prefix_cache). Reads the
    engine's container/served-name from current-topology.json (ADR-0012).

    **Asks for your sudo password, and only measures head-local engines.** Both are
    accidents of `vllm bench serve` living inside the container: reaching it means
    `sudo docker exec` (and ADR-0018 retired the passwordless `docker` grant, because a
    `docker` grant is root), and the container is only reachable on its own node — so
    this refuses every single-node profile, including whatever is usually serving.

    Deliberately not patched here. ADR-0016 rebuilds the regiment HTTP-native against
    the stable endpoint, where neither problem exists; see ADR-0018's errata for the
    reasoning. Until then: interactive only.
    """
    # Fail with the reason rather than a confusing subprocess error 20 minutes in.
    if (subprocess.run(["sudo", "-n", "docker", "version"],
                       capture_output=True).returncode != 0 and not os.isatty(0)):
        console.print(
            "[red]bench needs `sudo docker` and there's no terminal to prompt on.[/]\n"
            "  `vllm bench serve` lives inside the container, and geoff's passwordless "
            "docker grant was retired by ADR-0018 (a docker grant is root).\n"
            "  Run it from a terminal. The unattended path is ADR-0016's HTTP-native "
            "rebuild of this regiment, not a way around the boundary.")
        raise typer.Exit(2)
    current = topology.load_current_topology()
    if current is None:
        console.print("[yellow]No current-topology.json — nothing has been activated.[/]")
        raise typer.Exit(1)
    engines = [e for e in current.get("engines", []) if e.get("api_url")]
    if not engines:
        console.print("[yellow]No API engines to benchmark.[/]")
        raise typer.Exit(1)
    profile = current.get("profile", "?")
    # bench shells `docker exec` on THIS (head) node, so it can only reach an engine
    # whose container is local. Skip worker-node engines with a warning instead of
    # crashing on `docker exec <missing-container>`. Node-aware benching of worker
    # engines (ssh to the node) is an ADR-0016 follow-up; per-node profiles are the
    # experimental Tier-2 shape, and the head engine is representative for the A/B.
    local = socket.gethostname().split(".")[0]
    benchable = [e for e in engines if (e.get("api_node") or (e.get("nodes") or [local])[0]) == local]
    for e in engines:
        if e not in benchable:
            node = e.get("api_node") or (e.get("nodes") or ["?"])[0]
            console.print(f"[yellow]skipping {e['name']}: its container is on '{node}', not the "
                          f"local head, and `docker exec` can't reach it. Retired by ADR-0016's "
                          f"HTTP-native rebuild — until then this engine can't be benched.[/]")
    if not benchable:
        console.print("[yellow]No head-local engines to benchmark.[/]")
        raise typer.Exit(1)
    with Store() as store:
        for e in benchable:
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


# --- deploy: converge the fleet (privileged, human, password-gated) ---------

@app.command()
def deploy(
    evict: bool = typer.Option(
        False, "--evict",
        help="Actually delete weights no profile keeps any more. Without it they are "
             "only reported — no silent loss.",
    ),
    tags: str = typer.Option(None, "--tags", help="Limit to these ansible tags."),
) -> None:
    """Converge the whole FLEET to the allowlist (ansible site.yml).

    No profile argument: `deploy` means *deploy the fleet*. It installs every
    allowlisted profile's engines, images and weights, and the activation grants —
    setting the boundary of what may run. It is selection-neutral: whatever is
    serving keeps serving (falling to `empty` only if its profile left the
    allowlist), and it never auto-promotes a model. Use `activate` to choose.
    """
    raise typer.Exit(ops.deploy(evict=evict, tags=tags))


@app.command()
def check(
    evict: bool = typer.Option(False, "--evict", help="Plan with eviction enabled."),
) -> None:
    """Dry-run the fleet deploy (--check --diff) — shows what would change, makes nothing."""
    raise typer.Exit(ops.deploy(dry_run=True, evict=evict))


# --- activate: choose the live model (unprivileged, human OR agent) ---------

@app.command()
def activate(
    profile: str = typer.Argument(
        None, help="Profile to make live, or `empty`. Omit to show what's activatable."),
    force: bool = typer.Option(
        False, "--force",
        help="Restart the target engines even if they already serve this definition."),
    wait: bool = typer.Option(
        True, "--wait/--no-wait", help="Block until every engine answers."),
    smoke_gate: bool = typer.Option(
        True, "--smoke/--no-smoke", help="Run the smoke gate once the engines are ready."),
) -> None:
    """Make an already-deployed profile the live one. Needs no root.

    Writes the activation request (a group-writable file — no sudo), then triggers
    the fixed reconciler through its single-command sudoers entry. The reconciler
    re-validates the request against the installed env files on every node, writes
    the per-node desired markers as a transaction, and drives systemd to match.
    Any node's failure drives the fleet to `empty` rather than guessing.
    """
    allowed = act.read_allowlist()
    if profile is None:
        live, want = act.live_profile(), act.requested()
        console.print(f"[bold]live:[/] {live or '—'}"
                      + (f"   [dim](requested: {want})[/]" if want and want != live else ""))
        if not allowed:
            console.print(f"[yellow]No allowlist at {act.ALLOWLIST_FILE}[/] — "
                          f"run `./sparky.sh deploy` first.")
            raise typer.Exit(2)
        console.print("[bold]activatable:[/] " + "  ".join(allowed))
        raise typer.Exit()

    rc = act.activate(profile, force=force)
    if rc != 0:
        raise typer.Exit(rc)
    if profile == act.EMPTY or not wait:
        raise typer.Exit(0)

    console.print("[bold]waiting for the engines to answer[/] — a big model loads for minutes…")
    if not act.wait_for_ready():
        console.print("[red]timed out waiting for readiness[/] — check `./sparky.sh logs head`.")
        raise typer.Exit(1)
    if not smoke_gate:
        raise typer.Exit(0)
    console.print("[bold]smoke gate[/]")
    raise typer.Exit(_smoke(None, str(SMOKE_REPORT)))


@app.command()
def probe(
    what: str = typer.Argument(..., help="versions | archs | pip | attr | quant"),
    args: list[str] = typer.Argument(None, help="Probe arguments (architectures, packages, …)."),
    image: str = typer.Option(
        None, "--image", "-i",
        help="Image to introspect. Defaults to the image the live profile runs."),
) -> None:
    """Ask a DEPLOYED container image a read-only question — no root, no docker grant.

    The bounded probe of ADR-0019: which architectures a vLLM build supports, what it
    ships, whether an upstream fix has landed. This is the cheap half of model
    evaluation — do it BEFORE writing a profile, not after an activation fails.

        sparky probe versions
        sparky probe archs Mistral3ForConditionalGeneration
        sparky probe pip xgrammar transformers
        sparky probe attr vllm.model_executor.models.step3_vl Step3VLProcessor._get_num_multimodal_tokens
    """
    if image is None:
        image = _live_image()
        if image is None:
            console.print("[red]No image given and none inferable[/] — pass --image. "
                          "Deployed images are listed by a bare `sparky probe`.")
            raise typer.Exit(2)
    cmd = [*act._sudo(), PROBE_BIN, image, what, *(args or [])]
    console.print(f"[dim]+ {' '.join(cmd)}[/]")
    proc = subprocess.run(cmd, text=True, capture_output=True)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
    raise typer.Exit(proc.returncode)


@app.command()
def fleet() -> None:
    """Show the allowlist: what a deploy installed, where its weights live, and which
    profiles are parked."""
    state = act.fleet_state()
    repo = load_fleet()
    live = act.live_profile()
    table = Table(title="fleet — the allowlist (ansible/profiles/*.yml)", title_justify="left")
    for col in ("profile", "state", "engines", "nodes", "model"):
        table.add_column(col, overflow="fold")
    installed = set((state or {}).get("allowlist", []))
    for p in repo.profiles:
        if p.blocked:
            state_s = "[yellow]parked[/]"
        elif p.name == live:
            state_s = "[green]LIVE[/]"
        elif state is None or p.name in installed:
            state_s = "deployed" if state else "[dim]not deployed[/]"
        else:
            state_s = "[yellow]needs deploy[/]"
        table.add_row(
            p.name, state_s,
            ", ".join(e.name for e in p.engines) or "—",
            ", ".join(sorted({n for e in p.engines for n in e.nodes})) or "—",
            ", ".join(sorted({e.model for e in p.engines})) or "—",
        )
    console.print(table)
    if state:
        for node in state.get("nodes", []):
            console.print(f"  [bold]{node['node']}[/]: "
                          f"engines {', '.join(node['engines']) or '—'} · "
                          f"weights {', '.join(node['models']) or '—'}")
        console.print(f"  endpoint {state.get('model_endpoint')} "
                      f"(stable name '{state.get('stable_model_name')}') · "
                      f"deployed {state.get('deployed_at')}")
    else:
        console.print(f"  [yellow]no {act.FLEET_STATE} — this cluster has not been deployed yet.[/]")


@app.command()
def teardown(
    webui: bool = typer.Option(False, "--webui", help="also stop Open WebUI + Caddy"),
    break_glass: bool = typer.Option(
        False, "--break-glass",
        help="Stop engines as `deploy` over ansible instead of activating `empty`. "
             "For when the reconciler itself is broken; needs your sudo password."),
) -> None:
    """Stop serving. Normally this is just `activate empty` — unprivileged."""
    if webui or break_glass:
        raise typer.Exit(ops.teardown(include_webui=webui))
    raise typer.Exit(act.activate(act.EMPTY))


@app.command("admin-password")
def admin_password() -> None:
    """Set the /admin basic_auth password (ADR-0018 turns the panel's auth on).

    Hashes with the Caddy image already on this node and writes the hash to the
    runtime secret file — never into git. Run once; re-run to rotate.
    """
    import getpass as _getpass

    hash_file = Path("/opt/cluster/admin-basic-auth.hash")
    # No complexity rules: whatever you type is what gets hashed. Leading, trailing and
    # internal whitespace all survive the trip (`IFS= read -r` in the container preserves
    # them verbatim), so there is nothing to guard against — only the typo, which is what
    # the confirmation is for.
    pw = _getpass.getpass("New /admin password: ")
    if pw != _getpass.getpass("Confirm: "):
        console.print("[red]passwords differ[/]")
        raise typer.Exit(2)
    # Hashed through `deploy`, which is in the docker group — so this prompts for your
    # password, exactly as a deploy does. That's the right shape: setting the panel's
    # password is a provisioning act, and it means geoff needs no docker grant of his
    # own (ADR-0018 retired his passwordless sudo; a `docker` grant IS root).
    #
    # `caddy hash-password` insists on a terminal unless given --plaintext, and a
    # --plaintext argument here would put the password in the host's `ps` output and in
    # the sudo log. So the read happens INSIDE the container: the password crosses on
    # stdin, and the only argv it ever reaches is a short-lived process in an ephemeral
    # container's namespace. What the sudo log records is this literal command.
    proc = subprocess.run(
        [*ops._as_deploy(), "docker", "run", "--rm", "-i", "caddy:2",
         "sh", "-c", 'IFS= read -r p; caddy hash-password --plaintext "$p"'],
        input=pw, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        console.print(f"[red]caddy hash-password failed:[/] {proc.stderr.strip()}")
        raise typer.Exit(1)
    hash_file.write_text(proc.stdout.strip() + "\n")
    hash_file.chmod(0o640)
    console.print(f"[green]wrote {hash_file}[/] — run `./sparky.sh deploy` to apply it.")


def _render_status(s: dict) -> None:
    """Human view of the control panel's /status.json."""
    if not s.get("has_topology"):
        console.print("[yellow]No topology recorded[/] — nothing deployed, or a deploy is in flight.")
        for u in s.get("units", []):
            console.print(f"  {u['unit']}: {u['state']}")
        return
    # The phase is what distinguishes "wait" from "something is wrong" — a weight load
    # is minutes of not-serving, and reporting that as a fault trains you to ignore it.
    verdict = {
        "serving": "[green]✓ healthy[/]",
        "idle": "[green]✓ idle (nothing serving)[/]",
        "loading": "[yellow]⏳ loading weights — not serving yet[/]",
        "switching": "[yellow]⏳ activation in flight — switching profile[/]",
        "stalled": "[red]✗ stalled — up but never became ready[/]",
        "down": "[red]✗ down[/]",
        "failsafe": "[red]⚠ fail-safe recovery[/]",
    }.get(s.get("phase"), "[green]✓ healthy[/]" if s.get("ok") else "[red]✗ degraded[/]")
    when = s.get("deployed_at") or "?"
    console.print(f"[bold]{s.get('profile')}[/]   activated {when}   {verdict}")
    if s.get("pending"):
        console.print(f"  [yellow]pending:[/] {', '.join(s['pending'])} are running an older "
                      f"definition than the last deploy rendered — re-activate to apply it.")
    table = Table(title_justify="left")
    for col in ("engine", "node", "systemd", "API", "model"):
        table.add_column(col, overflow="fold")
    for e in s.get("engines", []):
        api = {"serving": "[green]ready[/]",
               "loading": f"[yellow]loading {e.get('elapsed') or 0}s[/]",
               "switching": "[yellow]switching…[/]",
               "stalled": "[red]stalled[/]"}.get(
                   e.get("phase"), "[green]ready[/]" if e.get("api_ok") else "[red]down[/]")
        for i, n in enumerate(e.get("nodes", [])):
            st = n["state"] + (" [red](fail-safe)[/]" if n.get("failsafe") else "")
            st = f"[green]{st}[/]" if n["state"] in ("active", "running") else f"[red]{st}[/]"
            table.add_row(e["name"] if i == 0 else "", n["node"], st,
                          api if i == 0 else "", (e.get("model") or "—") if i == 0 else "")
    console.print(table)
    console.print("  " + "   ".join(f"{svc['name']}: {svc['state']}" for svc in s.get("services", [])))


@app.command()
def status(
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON (machine-readable)."),
) -> None:
    """Live cluster status — reads the control panel (no sudo, every node).

    Exit **0** healthy · **1** degraded or in fail-safe · **2** panel unreachable.

    There is **no fallback**, by design. There used to be one, over
    `sudo -u deploy ansible` — a password-gated route to information that isn't
    privileged at all (reading systemd state needs no rights; so does the reconciler's
    `--status` verb). It fired when the panel merely got *slow*, which happens when a
    node is down — exactly when you most need status to work without sudo, and exactly
    when an unannounced password prompt hangs an agent. A panel that is genuinely down
    is a fault worth reporting plainly, not papering over with a second implementation
    that would drift from the first.
    """
    s = ops.panel_status()
    if s is None:
        raise typer.Exit(_panel_unreachable(json_out))
    if json_out:
        console.print_json(data=s)
    else:
        _render_status(s)
    raise typer.Exit(0 if s.get("ok") else 1)


@app.command()
def logs(node: str = typer.Argument("head", help="head | worker")) -> None:
    """Follow the vLLM journal on a node."""
    raise typer.Exit(ops.logs(node))


@app.command()
def lint() -> None:
    """Ansible syntax-check + validate the whole allowlist (ADR-0011 Layer 1)."""
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
