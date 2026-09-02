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
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sparky.foundation import scope
from sparky.verify import smoke
from sparky.serve import activate as act
from sparky.measure.loop import runner, suite, suitectl
from sparky.measure.instruments import coding, reference, sandbox, soak, tools
from sparky.serve import ansible as ops
from sparky.measure.instruments import bench as httpbench
from sparky.foundation import topology
from sparky.measure.instruments import evals
from sparky.measure.record import report, scoreboard, store
from sparky.foundation.api import VllmClient
from sparky.serve.fleet import load_fleet
from sparky.measure.record.store import Store

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
            img = topology.load_profile(live).image
            if img:
                return img
        except Exception:
            pass
    from collections import Counter
    try:
        images = Counter(p.image for p in topology.all_profiles() if p.image)
    except Exception:
        return None
    return images.most_common(1)[0][0] if images else None


@app.callback()
def _root() -> None:
    """Sparky Cluster — operator entrypoint (ADR-0015)."""


@app.command("topology", rich_help_panel=scope.OPERATE)
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

    title = p.name + (f"   image: {p.image}" if p.image else "")
    if p.blocked:
        title += "   [yellow](blocked — weights kept, not activatable)[/]"
    table = Table(title=title, title_justify="left")
    for col in ("engine", "served as", "nodes (rank0=API)", "TP", "port", "model", "mem_frac", "context"):
        table.add_column(col, overflow="fold")
    for e in p.engines:
        nodes = "+".join(e.nodes) + (f"  API:{e.api_node}" if e.is_multinode else "")
        table.add_row(
            e.name, e.served_as, nodes, str(e.tensor_parallel_size),
            str(e.port), e.model, f"{e.memory_fraction:g}", str(e.context_length),
        )
    console.print(table)


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


@app.command("smoke", rich_help_panel=scope.OPERATE)
def smoke(
    topology_file: str = typer.Option(
        None, "--topology", help="Topology JSON to probe (default: the live current-topology.json)."),
    report_file: str = typer.Option(
        str(smoke.SMOKE_REPORT), "--report",
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
    raise typer.Exit(smoke.run(topology_file, report_file))


@app.command("report", rich_help_panel=scope.OPERATE)
def report_cmd(
    label_a: str = typer.Argument(..., help="Baseline label."),
    label_b: str = typer.Argument(..., help="Comparison label."),
) -> None:
    """Compare two benchmark labels from the trend store (direction-aware A/B)."""
    with Store() as store:
        comparison = report.compare(store, label_a, label_b)
    report.render(console, label_a, label_b, comparison)


# --- deploy: converge the fleet (privileged, human, password-gated) ---------

@app.command(rich_help_panel=scope.PROVISION)
def deploy(
    evict: bool = typer.Option(
        False, "--evict",
        help="Actually delete weights no profile keeps any more. Without it they are "
             "only reported — no silent loss.",
    ),
    tags: str = typer.Option(None, "--tags", help="Limit to these ansible tags."),
    check: bool = typer.Option(
        False, "--check",
        help="Dry run (ansible --check --diff): show what would change, change nothing.",
    ),
) -> None:
    """Converge the whole FLEET to the allowlist (ansible site.yml).

    No profile argument: `deploy` means *deploy the fleet*. It installs every
    allowlisted profile's engines, images and weights, and the activation grants —
    setting the boundary of what may run. It is selection-neutral: whatever is
    serving keeps serving (falling to `empty` only if its profile left the
    allowlist), and it never auto-promotes a model. Use `activate` to choose.

    `--check` is the dry run. It was a separate `check` command until 2026-08-10, which
    made it a third thing to classify — it looked like a development command but is
    `deploy` in every way that matters: same code path, same `sudo -u deploy` password
    gate, same publish to /opt/cluster. As a flag it also composes, so
    `deploy --check --evict` asks the question you actually want answered: what would an
    evicting deploy delete?
    """
    raise typer.Exit(ops.deploy(evict=evict, tags=tags, dry_run=check))


# --- activate: choose the live model (unprivileged, human OR agent) ---------

@app.command(rich_help_panel=scope.OPERATE)
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

    # One definition of "live", shared with the suite runner. This used to be an inline
    # sequence here and a different, shorter one in the runner — which is how the runner
    # ended up measuring an engine that was still loading (2026-08-10).
    try:
        act.bring_up(profile, force=force, wait=wait,
                     smoke=smoke_gate if wait else False,
                     on_event=lambda m: console.print(f"[bold]{m}[/]"))
    except act.NotLive as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(exc.code)
    raise typer.Exit(0)


@app.command(rich_help_panel=scope.OPERATE)
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


@app.command("bench", rich_help_panel=scope.OPERATE)
def bench(
    label: str = typer.Argument(None, help="Label to record under (default: the profile)."),
    scenarios: str = typer.Option("latency,throughput,prefix_cache", "--scenarios", "-s"),
    record: bool = typer.Option(True, "--record/--no-record"),
) -> None:
    """Throughput and latency over HTTP (ADR-0016).

    Needs no root and no docker: it drives the stable model endpoint and times the token
    stream client-side. It replaced a regiment that shelled `sudo docker exec … vllm
    bench serve`, which could not run unattended (ADR-0018 retired the docker grant) and
    refused every single-node profile, since the container is only reachable on its own
    node — so for the first time the model that has actually been serving can be measured.

    Numbers compare to other runs of THIS harness, not to `vllm bench serve`: timings are
    client-side and include network and client overhead (ADR-0018's errata).
    """
    current = topology.load_current_topology()
    if not current or not current.get("engines"):
        console.print("[yellow]Nothing serving — activate a profile first.[/]")
        raise typer.Exit(1)
    engine = current["engines"][0]
    profile = current.get("profile", "?")
    label = label or profile

    chosen = [s.strip() for s in scenarios.split(",") if s.strip()]
    unknown = [s for s in chosen if s not in httpbench.SCENARIOS]
    if unknown:
        console.print(f"[red]unknown scenario(s): {', '.join(unknown)}[/] — "
                      f"choose from {', '.join(httpbench.SCENARIOS)}")
        raise typer.Exit(2)

    console.print(f"[bold]bench[/] · {profile} · {', '.join(chosen)}")
    console.print(f"[dim]{engine['api_url']} · served as {engine['served_as']}[/]")

    # Context capacity: how much this configuration can READ. Speed metrics cannot say
    # it, and for long-document work it is the binding constraint — so it is measured
    # once per bench run and attached to every scenario row.
    ctx = httpbench.context_capacity(engine["api_url"])
    if ctx:
        console.print(
            f"  [bold]context[/]: {ctx.get('usable_context', 0):,} usable tokens "
            f"(KV holds {ctx.get('kv_tokens', 0):,}, context "
            f"{ctx.get('context_length', 0):,}) · "
            f"{ctx.get('full_slots', 0):.1f} full-length slots")
    else:
        console.print("  [yellow]context: unavailable[/] — cache_config_info not exposed")

    table = Table(title=f"bench: {label}", title_justify="left")
    for col in ("scenario", "out tok/s", "req/s", "TTFT mean", "TTFT p99", "TPOT mean", "failed"):
        table.add_column(col, overflow="fold")

    with VllmClient(engine["api_url"], timeout=600.0) as client:
        for name in chosen:
            scenario = httpbench.SCENARIOS[name]
            console.print(f"[dim]  {name}: {scenario.requests} requests, "
                          f"concurrency {scenario.concurrency}…[/]")
            result = httpbench.run_scenario(client, engine["served_as"], scenario,
                                            model_dir=engine.get("model", ""))
            m = result.metrics()
            failed = len(result.results) - len(result.good)
            if not m:
                # Show WHY, not just how many. A count alone cannot distinguish a 400
                # from a timeout, and the first live run cost a round-trip to learn that
                # the requests were addressing the wrong model name.
                reasons = sorted({r.error for r in result.results if r.error})
                table.add_row(name, "—", "—", "—", "—", "—", f"[red]{failed}[/]")
                for reason in reasons[:3]:
                    console.print(f"    [red]{reason}[/]")
                continue
            table.add_row(
                name,
                f"{m['output_toks_s']:.1f}", f"{m['requests_s']:.2f}",
                f"{m['ttft_mean_ms']:.0f}ms", f"{m['ttft_p99_ms']:.0f}ms",
                f"{m['tpot_mean_ms']:.1f}ms" if m.get("tpot_mean_ms") else "—",
                str(failed) if not failed else f"[red]{failed}[/]")
            if record:
                with store.Store() as db:
                    run = httpbench.to_run(result, label=label,
                                           model=engine.get("model", "?"), profile=profile)
                    run.kv_tokens = ctx.get("kv_tokens")
                    run.context_length = ctx.get("context_length")
                    db.record(run)
    console.print(table)
    if record:
        console.print(f"[dim]  recorded as '{label}'[/]")
        _refresh_panel_snapshot()



# The panel renders a FILE — it does no analysis of its own (see `scoreboard.to_json`),
# which is what keeps dominance and best-marking from drifting between two
# implementations. That makes the file the freshness boundary: regenerate it whenever a
# measurement lands, and the web scoreboard is never staler than the last recorded run.
# The alternative — a timer — is a thing to forget about and a second place to look when
# the page is wrong.
PANEL_SNAPSHOT = Path("/opt/cluster/scoreboard.json")


def _restore_serving(was_serving: str | None) -> None:
    """Put back whatever was serving before the suite took the cluster.

    A measurement must not decide what serves. Left alone, a suite promotes its LAST job
    by accident — a candidate, chosen by job ordering, at whatever hour the run finished.
    That is the "restored ≠ promoted" rough edge, closed at the point it is created.

    Best-effort and loud about failing: the run's real work is already recorded and its
    exit code belongs to the measurements, not to this.
    """
    if not was_serving:
        return
    live = act.live_profile()
    if live == was_serving:
        return
    console.print(f"\n[dim]restoring {was_serving} (was serving before the run; "
                  f"{live or 'nothing'} is what the last job left)[/]")
    try:
        act.bring_up(was_serving, on_event=lambda m: console.print(f"  [dim]{m}[/]"))
    except Exception as exc:  # noqa: BLE001 - a failed restore must not fail the suite
        console.print(f"[yellow]could not restore {was_serving}: {exc}[/]")
        console.print(f"[yellow]{live or 'nothing'} is serving — "
                      f"`./sparky.sh activate {was_serving}` when you want it back.[/]")


def _scoreboard_table(*, include_retired: bool = False):
    """The scoreboard, built and attributed. Returns `(table, dropped labels)`.

    **One producer, deliberately.** The panel renders a file and does no analysis, which
    is what keeps dominance and best-marking from drifting — but that only holds while
    there is one thing WRITING the file. There were two, and they disagreed: this path
    skipped the profile attribution entirely, so every snapshot the suite wrote had no
    `hf_repo` (no Hub links on the web scoreboard, silently) and `retired: False` on
    everything (so Step-3.5-Flash and the four single-node profiles never left the page).
    Each suite overwrote whatever a correct `scoreboard --json` had produced.
    """
    with store.Store() as db:
        rows = db.rows()
    table = scoreboard.build(rows)
    # Attach the upstream repo id from the profile. The store records a label; only the
    # profile knows which org published the checkpoint, and that is the one string that
    # makes a scoreboard row searchable on the Hub.
    live = {p.name: p.hf_repo for p in topology.all_profiles()}
    # Retired profiles keep their repo id (docs/models/retired/), so a retired row still
    # links to the Hub — the measurement is real and its provenance is exactly as useful
    # as a live one's. `retired` is decided by absence from the LIVE set, not by whether
    # a repo was found.
    archived = {}
    retired_dir = topology.PROFILES_DIR / "retired"
    if retired_dir.is_dir():
        for f in sorted(retired_dir.glob("*.yml")):
            try:
                prof = topology.load_profile(f)
            except Exception:
                continue
            archived[prof.name] = prof.hf_repo
    for row in table:
        row.hf_repo = (live.get(row.label) or live.get(row.profile)
                       or archived.get(row.label) or archived.get(row.profile))
        # Off the default board iff the profile is not in the LIVE allowlist. The scoreboard
        # answers "which model I could serve NOW should be serving", so a measurement of a
        # profile that no longer exists — a formal retirement OR a deleted experimental variant
        # (`-single`, `-mtp3`) — is noise on that question. The rows are never deleted, only
        # hidden; `--retired` brings them all back, and the hf_repo stays attached (from the
        # retired-doc set when present) so a shown-with-flag row is still searchable on the Hub.
        # (The old code checked `archived` — a dir of profile .yml that does not exist — so it
        # hid nothing, contradicting the intent stated just above. 2026-08-31.)
        row.retired = not (row.label in live or row.profile in live)

    # Dropped BEFORE pareto, so dominance is a claim about models you could actually
    # activate. "Beaten on every axis" is only useful as advice, and advice about a
    # profile that no longer exists is noise — worse, a retired row can dominate a live
    # one on numbers taken under a different container.
    dropped = [r.label for r in table if r.retired]
    if not include_retired:
        table = [r for r in table if not r.retired]
    return table, dropped


def _make_snapshot_shared(path: Path) -> None:
    """The panel snapshot is rewritten by BOTH refreshers — the CLI (geoff) and the detached
    suite runner (activator) — which share no owning identity, only the `activate` group. So it
    must be group=activate and group-WRITABLE (0o664), or whoever wrote it last freezes the other
    out: 2026-09-02, an activator suite run could not overwrite a geoff-owned `chmod 644` snapshot
    and the panel sat two weeks stale. chown/chmod bind only for the file's OWNER; a non-owner
    writer reaches the file through the group-write bit and simply skips them (the owner already
    set them). Best-effort — a dev checkout has no `activate` group, which is fine."""
    import grp
    try:
        os.chown(path, -1, grp.getgrnam("activate").gr_gid)
    except (KeyError, PermissionError, OSError):
        pass
    try:
        path.chmod(0o664)
    except (PermissionError, OSError):
        pass


def _refresh_panel_snapshot() -> None:
    """Best-effort rewrite of the panel's snapshot. Never raises.

    A measurement that succeeded must not be reported as a failure because the panel's
    directory is missing, read-only, or this is a dev checkout with no /opt/cluster.
    """
    try:
        if not PANEL_SNAPSHOT.parent.is_dir():
            return
        import json as _json
        table, _ = _scoreboard_table()
        if not table:
            return
        points, dominated = scoreboard.pareto(table)
        payload = scoreboard.to_json(table, points, dominated)
        payload["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        PANEL_SNAPSHOT.write_text(_json.dumps(payload, indent=2) + "\n")
        _make_snapshot_shared(PANEL_SNAPSHOT)
    except Exception:
        pass


@app.command("run", rich_help_panel=scope.OPERATE)
def run_cmd(
    name: str = typer.Argument(None, help="Suite name (not a path). Omit to list."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Tail the log after starting."),
    stop: bool = typer.Option(False, "--stop", help="Stop the running suite."),
    log: bool = typer.Option(False, "--log", help="Show the log and exit; nothing starts."),
    restart: bool = typer.Option(False, "--restart",
                                 help="Discard breadcrumbs first — re-measure everything."),
) -> None:
    """Start a named suite — detached and logged (ADR-0020, ADR-0021).

    A suite is to a procedure what a profile is to a model: declarative, installed by
    `deploy`, started by NAME. Taking a name rather than a path is what makes the
    allowlist mean anything — a path argument would let any YAML on the box run.

    **This does not run the suite; it starts it.** The run is a systemd unit of its
    own, so it survives this shell, a dropped connection, and the deploy that restarts the
    panel — a measurement that takes three hours must not depend on a terminal staying
    open. Its output appends to a per-suite log. `--follow` tails it; Ctrl-C leaves the
    run going.

    **Breadcrumbs are shared across suites**, because they are keyed on
    `(profile, regiment)` rather than on which file asked for it — so a suite that
    overlaps one you just ran skips the overlap instead of re-measuring it. That is
    usually what you want and occasionally not: after a container bump every number is
    stale, and `--restart` is how you say so.

    Iterating on a job list that is not an installed suite yet? That is
    `sparky suite <path>`, in the foreground.
    """
    if stop:
        raise typer.Exit(suitectl.stop())
    if not name:
        _suite_list()
        raise typer.Exit(0 if suite.available() else 1)
    if log:
        raise typer.Exit(suitectl.follow(name, once=True))

    if restart:
        # Done HERE rather than as a second argument to the trigger. That program takes
        # one bare identifier and composes every path itself, which is most of why it is
        # safe to expose to a web request; adding a flag it must interpret would trade
        # that away for a gesture the caller can just perform first. Clearing the file is
        # unprivileged — the measurement artifacts are activate-writable (ADR-0021).
        runner.DEFAULT_BREADCRUMBS.unlink(missing_ok=True)
        console.print("[dim]breadcrumbs cleared — every regiment will be re-measured, "
                      "for every profile, not just this suite's[/]")

    code = suitectl.start(name)
    if code != 0:
        raise typer.Exit(code)
    console.print(f"[green]started[/] {name} — detached, logging to {suitectl.log_path(name)}")
    console.print(f"[dim]follow: ./sparky.sh run {name} --follow   ·   "
                  f"stop: ./sparky.sh run --stop[/]")
    if follow:
        raise typer.Exit(suitectl.follow(name))


def _suite_list() -> None:
    """What may be started, and whether anything is running."""
    installed = suite.describe()
    if not installed:
        console.print("[bold]suites:[/] (none installed — ./sparky.sh deploy)")
    for rb in installed:
        # `estimate` can be absent — an older installed copy, or a file `lint` would have
        # rejected. Show what is there rather than a stray separator around nothing.
        meta = " · ".join(x for x in (rb["estimate"], f"{rb['jobs']} profile(s)") if x)
        console.print(f"  [bold]{rb['name']}[/]  [dim]{meta}[/]")
        if rb["description"]:
            console.print(f"    [dim]{rb['description']}[/]")
    # Named separately rather than merged: an authored-but-not-deployed suite is not a
    # thing you can start, and showing it in one list is how you learn that at the moment
    # you try. See ADR-0021 — the installed set is the allowlist.
    startable = {rb["name"] for rb in installed}
    pending = [n for n in suite.authored() if n not in startable]
    if pending:
        console.print(f"[yellow]not deployed:[/] {'  '.join(pending)} "
                      f"[dim](./sparky.sh deploy)[/]")
    running = suitectl.running()
    console.print(f"[bold]running:[/] {running}" if running else "[dim]nothing running[/]")


@app.command("suite", rich_help_panel=scope.OPERATE)
def suite_cmd(
    spec_file: str = typer.Argument(..., help="YAML job list (profile x regiments)."),
    resume: bool = typer.Option(True, "--resume/--restart",
                                help="Continue from breadcrumbs (default), or start over."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; run nothing."),
) -> None:
    """Run a job list end to end — the ADR-0016 outer loop.

    A suite COMMANDEERS the cluster: it activates one profile after another, so serving is
    whatever the suite is currently measuring. That is why it takes an exclusive lock and
    why the job list is explicit rather than expanded from a matrix — it is a thing you
    read and approve before it takes the fleet for two hours.

    Interrupted runs resume: breadcrumbs are written after every REGIMENT, so a 45-minute
    soak is never repeated because the bench after it failed.
    """
    import yaml
    spec = yaml.safe_load(Path(spec_file).read_text())
    jobs = runner.load_jobs(spec)

    if dry_run:
        for job in jobs:
            console.print(f"  {job.key}: {', '.join(job.regiments)}")
        console.print(f"[dim]{len(jobs)} job(s). Nothing was run.[/]")
        raise typer.Exit()

    state = runner.load_state() if resume else runner.Breadcrumbs()
    if resume and state.done:
        console.print(f"[dim]resuming — {len(state.done)} regiment(s) already done[/]")

    def activate_profile(name: str) -> None:
        # `bring_up` returns only when the profile is LIVE and gated, and raises otherwise
        # — which is what quarantines it. The comment this replaced claimed activate()
        # "already waits for readiness and runs the smoke gate". It does not: it returns
        # when systemd accepts the start, and this runner then measured an engine that was
        # still loading (2026-08-10).
        act.bring_up(name, on_event=lambda m: console.print(f"  [dim]{m}[/]"))

    def _live():
        current = topology.load_current_topology()
        if not current or not current.get("engines"):
            raise RuntimeError("nothing serving after activate")
        return current["engines"][0]

    def _recorded_since(label: str, since: float, scenarios: set[str]) -> set[str]:
        """Which scenarios actually LANDED in the store for this label."""
        with store.Store() as db:
            return {r["scenario"] for r in db.rows()
                    if r["label"] == label and (r.get("ts") or 0) >= since
                    and r["scenario"] in scenarios}

    def _measured(job, fn, want: set[str], what: str) -> str:
        """Run a recording regiment and VERIFY it recorded something.

        On 2026-08-10 this returned "recorded" for both bench and quality after they ran
        against an engine that was still loading: every request failed, nothing was
        written, and each printed its usual closing line anyway. The runner believed the
        print. A regiment whose deliverable is a row in the trend store has exactly one
        honest success condition — the row is there.
        """
        started = int(time.time())
        try:
            fn()
        except SystemExit as exc:            # typer.Exit from the command function
            if getattr(exc, "code", 0) not in (0, None):
                raise RuntimeError(f"{what} exited {exc.code}") from None
        landed = _recorded_since(job.key, started, want)
        missing = want - landed
        if missing:
            raise RuntimeError(f"{what} recorded nothing for {sorted(missing)} — the "
                               f"engine answered nothing worth storing")
        return f"recorded {', '.join(sorted(landed))}"

    def _bench(job) -> str:
        # The prefill sweep (prefill@4k/16k/64k) rides the full sweep, not the interactive
        # default: a 64k prefill is seconds of work, worth it once per campaign for the
        # long-context curve, too slow for a quick `sparky bench`.
        scenarios = ("latency,throughput,prefix_cache,"
                     "prefill@4k,prefill@16k,prefill@64k")
        return _measured(
            job, lambda: bench(label=job.key, scenarios=scenarios, record=True),
            {"latency", "throughput", "prefix_cache",
             "prefill@4k", "prefill@16k", "prefill@64k"}, "bench")

    def _quality(job) -> str:
        return _measured(
            job, lambda: eval_cmd(label=job.key, limit=evals.DEFAULT_LIMIT,
                                  concurrency=8, record=True, dump=None),
            {"quality:mmlu-pro"}, "quality")

    def _tools(job) -> str:
        engine = _live()
        with VllmClient(engine["api_url"], timeout=300.0) as client:
            result = tools.check(client, engine["served_as"])
        if not result.ok:
            raise RuntimeError(result.summary())
        return result.summary()

    def _soak(job) -> str:
        engine = _live()
        with VllmClient(engine["api_url"], timeout=600.0) as client:
            result = soak.run(client, engine["served_as"],
                              on_progress=lambda m: console.print(f"    [dim]{m}[/]"))
        if not result.ok:
            raise RuntimeError(result.summary())
        return result.summary()

    def _coding(job) -> str:
        # Every parameter passed explicitly, as `_evals` does: calling a typer command as a
        # plain function leaves anything unpassed as an `OptionInfo`, which fails the first
        # time it is used as the value it is declared to be.
        # `via="local"` is not a default here, it is a rule: a suite must not depend on
        # a third party's availability or on a credential (ADR-0025). Every parameter is
        # passed because an unpassed one stays an `OptionInfo` and fails on first use.
        return _measured(
            job, lambda: coding_cmd(label=job.key, only=None, via="local", model=None,
                                    publish_prompts=False, concurrency=4, record=True),
            {s.scenario for s in coding.discover_sets()}, "coding")

    regiments = {"bench": _bench, "quality": _quality, "tools": _tools, "soak": _soak,
                 "coding": _coding}

    lock = None
    try:
        lock = runner.acquire()
    except runner.SuiteBusy as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    # What was serving before we took the cluster. A suite is a MEASUREMENT: it should
    # leave the fleet as it found it, the same way `deploy` is selection-neutral. Without
    # this, which model is live afterwards is an artifact of job ordering — the last thing
    # measured, promoted by accident, at whatever hour the run happened to finish.
    was_serving = act.live_profile()
    try:
        state = runner.run(jobs, activate=activate_profile, regiments=regiments,
                          state=state, on_event=lambda m: console.print(m))
    finally:
        # In the `finally`, so a suite that CRASHES still puts the fleet back. It was
        # outside it until 2026-08-11, and the first crash proved that wrong: the run died
        # on a breadcrumb write and left its candidate serving overnight.
        #
        # A `--stop` still does not restore, and gets that for free rather than by a flag:
        # systemd sends SIGTERM, whose default disposition terminates the interpreter
        # without unwinding, so this never runs. Which is the behaviour we want — an
        # activation cannot finish inside the 180s a stopped unit has left.
        _restore_serving(was_serving)
        runner.release(lock)
        _refresh_panel_snapshot()

    console.print()
    console.print(runner.summary(state))
    if any(not o.ok for o in state.outcomes) or state.quarantined:
        raise typer.Exit(1)


@app.command("scoreboard", rich_help_panel=scope.OPERATE)
def scoreboard_cmd(
    markdown: bool = typer.Option(False, "--markdown", "-m",
                                  help="Emit a markdown table for docs/."),
    no_plot: bool = typer.Option(False, "--no-plot"),
    json_out: str = typer.Option(None, "--json", help="Write a snapshot for the panel."),
    retired: bool = typer.Option(False, "--retired",
                                 help="Include profiles that have left the allowlist."),
) -> None:
    """The whole fleet on one screen — quality against speed (ADR-0016).

    `report` compares two labels; this compares everything measured, which is the shape
    the sourcing question actually has: is a model worth its disk, and which one should
    be serving?

    Shows the trade-off rather than a ranking. A composite score would recommend the
    biggest model every time — wrong for a two-node cluster, where serving one model
    means not serving another. The one objective claim it does make is **dominance**: a
    model beaten on every axis is never the right choice.

    **Retired profiles are excluded.** Not because their measurements were wrong, but
    because they stop being comparable: a container bump, a driver, a change to how a
    regiment measures — each moves the numbers under everything, and only the live set
    gets re-measured. A row nobody will ever re-run drifts from the ones beside it, and
    the drift is invisible. `--retired` shows them anyway; the verdicts live in
    `docs/models/tombstones.md`, and the store keeps every row regardless.
    """
    table, dropped = _scoreboard_table(include_retired=retired)
    if not table:
        console.print("[yellow]No measurements yet — run `sparky eval` and `sparky bench`.[/]")
        raise typer.Exit(1)
    # Said out loud, not silently: a scoreboard that quietly omits rows reads as "this is
    # everything measured", which is exactly the wrong thing to believe about it.
    if dropped and not retired and not json_out:
        console.print(f"[dim]{len(dropped)} retired profile(s) hidden "
                      f"({', '.join(sorted(dropped))}) — `--retired` to include.[/]")

    if json_out:
        import json as _json
        points, dominated = scoreboard.pareto(table)
        payload = scoreboard.to_json(table, points, dominated)
        payload["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = Path(json_out)
        path.write_text(_json.dumps(payload, indent=2) + "\n")
        _make_snapshot_shared(path)
        console.print(f"[dim]snapshot -> {json_out}[/]")
        raise typer.Exit()

    if markdown:
        print(scoreboard.to_markdown(table))
        raise typer.Exit()

    rich_table = Table(title="fleet scoreboard", title_justify="left")
    rich_table.add_column("model", overflow="fold")
    for name, *_ in scoreboard.COLUMNS:
        rich_table.add_column(name, justify="right")
    for row in table:
        cells = [f"[green]{c.text}[/]" if c.best and c.value is not None else c.text
                 for c in row.cells]
        rich_table.add_row(row.label, *cells)
    console.print(rich_table)

    points, dominated = scoreboard.pareto(table)
    if not no_plot:
        console.print()
        console.print(scoreboard.plot(points, dominated))
    if dominated:
        console.print(f"\n  [yellow]dominated[/] (beaten on every measured axis): "
                      f"{', '.join(sorted(dominated))}")
    missing = [r.label for r in table if r.missing]
    if missing:
        console.print(f"  [dim]incomplete: {', '.join(missing)} — "
                      f"run the missing regiment to place them[/]")


@app.command("eval", rich_help_panel=scope.OPERATE)
def eval_cmd(
    label: str = typer.Argument(None, help="Label to record under (default: the profile)."),
    limit: int = typer.Option(140, "--limit", "-n", help="Questions (max 280)."),
    concurrency: int = typer.Option(8, "--concurrency", "-c"),
    record: bool = typer.Option(True, "--record/--no-record", help="Write to the trend store."),
    dump: str = typer.Option(None, "--dump", help="Write per-item results as JSONL."),
) -> None:
    """Score the live model for ACCURACY — the `quality` regiment (ADR-0016).

    Everything else measures throughput or liveness; this is the only thing that says
    whether a model is any *good*, which is what ranking candidates requires.

    Uses a committed, category-balanced MMLU-Pro subset. Scores compare to other runs of
    the same subset — NOT to published MMLU-Pro numbers.
    """
    current = topology.load_current_topology()
    if not current or not current.get("engines"):
        console.print("[yellow]Nothing serving — activate a profile first.[/]")
        raise typer.Exit(1)
    engine = current["engines"][0]
    profile = current.get("profile", "?")
    label = label or profile

    items = evals.load_items(limit)
    console.print(f"[bold]quality[/] · {profile} · {len(items)} questions · "
                  f"concurrency {concurrency}")
    console.print(f"[dim]{engine['api_url']} · served as {engine['served_as']}[/]")

    done = {"n": 0}

    def tick(_result):
        done["n"] += 1
        if done["n"] % 20 == 0:
            console.print(f"[dim]  {done['n']}/{len(items)}…[/]")

    # Timeout sized from the WORKLOAD, not picked round. Under continuous batching each
    # request gets roughly (aggregate tok/s / concurrency), so a big TP=2 model at 148
    # tok/s across 32 requests gives ~4.6 tok/s each — and 4096 tokens then needs ~890s.
    # A flat 600s silently turned 41 of MiniMax's 140 answers into ReadTimeouts, scored
    # them wrong, and cost it ~29 points of accuracy that were never about the model.
    # 15 tok/s per request is a pessimistic floor for anything this cluster serves.
    timeout = max(600.0, evals.MAX_TOKENS / 15.0 * max(1, concurrency) / 4)
    console.print(f"[dim]per-request timeout {timeout:.0f}s[/]")
    with VllmClient(engine["api_url"], timeout=timeout) as client:
        result = evals.run(client, engine["served_as"], limit=limit,
                           concurrency=concurrency, on_item=tick)

    if dump:
        evals.dump_items(result, dump)
        console.print(f"[dim]  per-item results -> {dump}[/]")

    table = Table(title=f"quality: {label}", title_justify="left")
    for col in ("category", "score", "n"):
        table.add_column(col, overflow="fold")
    for category, (ok, n) in result.by_category().items():
        table.add_row(category, f"{100 * ok / n:.0f}%", str(n))
    console.print(table)
    console.print(f"  [bold]accuracy {100 * result.accuracy:.1f}%[/] "
                  f"({sum(1 for i in result.items if i.ok)}/{len(result.items)}) · "
                  f"unparseable {result.unparseable} (truncated {result.truncated}, "
                  f"rescued {result.rescued}, timed out {result.timed_out}) · "
                  f"{result.seconds / 60:.1f} min")
    if result.timed_out:
        console.print(f"  [red]{result.timed_out} request(s) timed out[/] — that is the "
                      f"harness giving up, not the model failing. Lower --concurrency or "
                      f"the score understates it.")

    # A run where most items produced no answer is not a measurement of the model — it
    # is a measurement of something going wrong (an engine torn away mid-run recorded
    # 1.4% on 2026-08-09, and that row would have sat on the scoreboard as a real score).
    # Refuse to record it; a missing cell is honest, a fabricated one is not.
    if record and result.unparseable > len(result.items) / 2:
        console.print(f"[red]NOT RECORDED[/] — {result.unparseable}/{len(result.items)} "
                      f"items unparseable. That is a broken run, not a score. "
                      f"Check the engine was serving throughout.")
        record = False
    if record:
        with store.Store() as db:
            db.record(store.Row(
                label=label, model=engine.get("model", "?"), profile=profile,
                scenario="quality:mmlu-pro", accuracy=result.accuracy,
                items=len(result.items), unparseable=result.unparseable))
        console.print(f"[dim]  recorded as '{label}' (scenario quality:mmlu-pro)[/]")
        _refresh_panel_snapshot()


@app.command("coding", rich_help_panel=scope.OPERATE)
def coding_cmd(
    label: str = typer.Argument(None, help="Label to record under (default: the profile)."),
    only: str = typer.Option(None, "--set", "-s", help="Score just this problem set."),
    via: str = typer.Option("local", "--via",
                            help="local (the serving fleet) or anthropic (a reference)."),
    model: str = typer.Option(None, "--model",
                              help="Reference model when --via anthropic."),
    publish_prompts: bool = typer.Option(
        False, "--publish-prompts",
        help="Allow a PRIVATE set's prompts to be sent to an external service."),
    concurrency: int = typer.Option(4, "--concurrency", "-c"),
    record: bool = typer.Option(True, "--record/--no-record", help="Write to the trend store."),
) -> None:
    """Score the live model on CODE — pass@1 against hidden tests (ADR-0024).

    The axis that matters most for software work and the one the fleet was ranked without.
    Answers execute in a bounded sandbox (`vllm-sandbox`) and either satisfy the tests or
    do not: no judge model, no reference-similarity. Correctness being decidable here is
    the entire reason this is worth more than another multiple-choice set.

    Each problem set under `benchmarks/coding/problems/` is scored and recorded
    SEPARATELY. A set may be a submodule that was never fetched; that is reported rather
    than treated as an error. Scores are never blended across sets — one set's number is
    not comparable to another's, and averaging them would produce a figure whose meaning
    changed with whichever submodules happened to be checked out.
    """
    external = via == "anthropic"
    if via not in ("local", "anthropic"):
        console.print(f"[red]unknown --via {via!r}[/] — local or anthropic")
        raise typer.Exit(2)

    if external:
        # A reference measures the SET, not the cluster, so it needs neither a live engine
        # nor the fleet lock — it can run while a model is serving.
        engine = None
        profile = reference.REFERENCE_PROFILE
        label = label or (model or reference.DEFAULT_MODEL)
    else:
        current = topology.load_current_topology()
        if not current or not current.get("engines"):
            console.print("[yellow]Nothing serving — activate a profile first.[/]")
            raise typer.Exit(1)
        engine = current["engines"][0]
        profile = current.get("profile", "?")
        label = label or profile

    if not sandbox.available():
        # Said before a single request is sent. Without it every problem fails identically
        # for one environmental reason, and the run would look like a model that cannot
        # write code at all.
        console.print(f"[red]no sandbox at {sandbox.SANDBOX_BIN}[/] — answers cannot be "
                      f"executed, so nothing here would be a measurement. "
                      f"Run ./sparky.sh deploy (ADR-0024).")
        raise typer.Exit(2)

    sets = [s for s in coding.discover_sets() if not only or s.name == only]
    absent = coding.missing_sets()
    if not sets:
        console.print(f"[yellow]no problem sets found under {coding.SETS_ROOT}[/]"
                      + (f" — {', '.join(absent)} present but unfetched" if absent else ""))
        raise typer.Exit(1)
    if absent:
        # Named, not silent: a private set missing turns a partial run into one that looks
        # complete, which is the failure ADR-0009 exists to prevent.
        console.print(f"[yellow]not measured[/] — {', '.join(absent)} "
                      f"(submodule not fetched)")

    if external:
        # A private set's PROMPTS are the asset that was kept private. Sending them to a
        # third party is a decision, not a side effect of choosing an endpoint (ADR-0025).
        # The hidden tests never leave either way.
        risky = [s.name for s in sets if s.is_private]
        if risky and not publish_prompts:
            console.print(f"[red]refusing to send private prompts[/] — "
                          f"{', '.join(risky)} "
                          f"{'is' if len(risky) == 1 else 'are'} private, and --via "
                          f"anthropic would publish {'its' if len(risky) == 1 else 'their'} "
                          f"problem prompts to a third party.")
            console.print("[dim]  Re-run with --publish-prompts to allow it, or with "
                          "--set <a-public-set> to measure something else.[/]")
            raise typer.Exit(2)
        if risky:
            console.print(f"[yellow]publishing prompts[/] for {', '.join(risky)} "
                          f"— explicitly allowed")

    console.print(f"[bold]coding[/] · {profile}{' (reference)' if external else ''} · "
                  f"{len(sets)} set{'s' if len(sets) != 1 else ''} · "
                  f"concurrency {concurrency}")
    console.print(f"[dim]{reference.API_URL if external else engine['api_url']}"
                  f" · {model or reference.DEFAULT_MODEL if external else engine['served_as']}[/]")

    timeout = max(600.0, coding.MAX_TOKENS / 15.0 * max(1, concurrency) / 4)
    if external:
        try:
            client_cm = reference.AnthropicClient(timeout=timeout)
        except reference.MissingKey as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(2)
        served_as = model or reference.DEFAULT_MODEL
    else:
        client_cm = VllmClient(engine["api_url"], timeout=timeout)
        served_as = engine["served_as"]
    with client_cm as client:
        for pset in sets:
            problems = coding.load_problems(pset)
            console.print(f"\n[bold]{pset.name}[/] {pset.version} · {len(problems)} problems "
                          f"· {pset.toolchain}")
            counter = {"n": 0}

            def tick(result, _total=len(problems)):
                counter["n"] += 1
                mark = "ok  " if result.passed else result.verdict.value
                console.print(f"[dim]  {counter['n']}/{_total} {mark:<13} {result.problem}"
                              f"{'' if result.passed else ' · ' + result.detail[:60]}[/]")

            result = coding.run(client, served_as, execute=sandbox.execute,
                                pset=pset, problems=problems,
                                concurrency=concurrency, on_item=tick)

            table = Table(title=f"coding: {label} · {pset.name}", title_justify="left")
            for col in ("track", "passed", "n"):
                table.add_column(col, overflow="fold")
            for track, (ok, n) in sorted(result.by_track().items()):
                table.add_row(track, f"{100 * ok / n:.0f}%", str(n))
            console.print(table)
            # The distribution, not just the pass rate: it ranks models even when none of
            # them pass, which is the expected state of a hard set (ADR-0024 §7).
            spread = " · ".join(f"{v.value} {n}" for v, n in sorted(
                result.by_verdict().items(), key=lambda kv: kv[0].value))
            console.print(f"  [bold]pass@1 {100 * result.accuracy:.1f}%[/] "
                          f"({result.passed}/{len(result.items)}) · "
                          f"weighted {100 * result.score:.1f}% · "
                          f"{result.seconds / 60:.1f} min")
            console.print(f"  [dim]{spread}[/]")

            # Same refusal as the quality regiment, for the same reason: a run where most
            # answers never arrived is a measurement of the harness, not of the model, and
            # a fabricated cell on the scoreboard is worse than a missing one.
            keep = record
            if keep and result.no_answer > len(result.items) / 2:
                console.print(f"[red]NOT RECORDED[/] — {result.no_answer}/"
                              f"{len(result.items)} answers never arrived. "
                              f"That is a broken run, not a score.")
                keep = False
            if keep:
                with store.Store() as db:
                    db.record(store.Row(
                        label=label,
                        model=served_as if external else engine.get("model", "?"),
                        profile=profile,
                        scenario=pset.scenario, accuracy=result.accuracy,
                        items=len(result.items), unparseable=result.no_answer,
                        score=result.score))
                console.print(f"[dim]  recorded as '{label}' (scenario {pset.scenario})[/]")
    _refresh_panel_snapshot()


@app.command(rich_help_panel=scope.OPERATE)
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


@app.command(rich_help_panel=scope.OPERATE)
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


@app.command("admin-password", rich_help_panel=scope.PROVISION)
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


@app.command(rich_help_panel=scope.OPERATE)
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


@app.command(rich_help_panel=scope.OPERATE)
def logs(node: str = typer.Argument("head", help="head | worker")) -> None:
    """Follow the vLLM journal on a node."""
    raise typer.Exit(ops.logs(node))


@app.command(rich_help_panel=scope.DEVELOP)
def lint() -> None:
    """Ansible syntax-check + validate the whole allowlist (ADR-0011 Layer 1)."""

    # Repo hygiene, checked here rather than in `Fleet.validate()`: validate() guards the
    # invariants a DEPLOY would break (unique names, one port, quoting), and it also runs
    # against synthetic fleets in unit tests. "Every profile says what it is an example
    # of" is a property of THIS repo's allowlist, and its whole purpose is that tests can
    # bind to a shape instead of a model name (topology.ARCHETYPES).
    #
    # Suites are an allowlist too (ADR-0020), so `lint` validates them alongside
    # profiles — the REPO copies, since this is the gate a deploy passes through. A
    # suite that names a privileged command should fail here, at Layer 1, and not two
    # hours into a suite.
    for name in suite.authored():
        problems = suite.validate(name)
        if problems:
            for problem in problems:
                console.print(f"[red]lint FAILED[/] — {problem}")
            raise typer.Exit(1)

    untagged = [p.name for p in topology.all_profiles() if not p.archetypes]
    if untagged:
        console.print(f"[red]lint FAILED[/] — no `archetypes:` on: {', '.join(untagged)}. "
                      f"Known: {sorted(topology.ARCHETYPES)}")
        raise typer.Exit(1)
    raise typer.Exit(ops.lint())


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
             rich_help_panel=scope.DEVELOP)
def test(ctx: typer.Context) -> None:
    """Run the harness unit tests (pytest). Extra args pass through (e.g. -k name, -x)."""
    import pytest

    os.chdir(ops.REPO_ROOT)
    raise typer.Exit(int(pytest.main(list(ctx.args))))


@app.command(rich_help_panel=scope.DEVELOP)
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
