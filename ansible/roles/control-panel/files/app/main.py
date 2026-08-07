"""Cluster control panel.

Read-only status + **one** control action: `activate` — make an already-deployed,
allowlisted profile the live one.

Runs as the low-privilege activation identity (ADR-0018 took it off `deploy`), bound
to 127.0.0.1, fronted by Caddy at /admin behind basic_auth. It holds exactly three
things and nothing else:

  * write access to the activation request ($DESIRED_PROFILE — group-writable, no sudo),
  * a single-command sudoers entry for $ACTIVATE_BIN,
  * an SSH key whose forced command on each worker is that same reconciler.

So there is **no web-API path to root**: the panel cannot run ansible, cannot deploy,
cannot restart arbitrary units, and is not in the docker group (which would be
root-equivalent). Its privileged action is on rails — the reconciler only ever
activates a deployed, allowlisted profile, never an arbitrary command.

Status is topology-aware via $CLUSTER_TOPOLOGY (current-topology.json, written by the
reconciler when an activation lands). Worker state comes back over the same bounded
channel as activation — `status`, the read-only verb of the forced command.

Actions run as DETACHED processes (own session) that write their combined output to
<run>/output.log and their exit code to <run>/done.rc when finished, so a run
survives the panel restarting. The unit sets KillMode=process to match.

See docs/control-interface.md and ADR-0018.
"""
import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ACTIVATE_BIN = os.environ.get("ACTIVATE_BIN", "/usr/local/sbin/vllm-activate")
DESIRED_PROFILE = Path(os.environ.get("DESIRED_PROFILE", "/opt/cluster/desired-profile"))
ALLOWLIST_FILE = Path(os.environ.get("ALLOWLIST_FILE", "/opt/vllm/engines/allowlist"))
ACTIVATE_SSH_KEY = os.environ.get("ACTIVATE_SSH_KEY", "/home/activator/.ssh/id_ed25519")
ACTIVATE_SSH_USER = os.environ.get("ACTIVATE_SSH_USER", "activator")
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "runs")).resolve()
TOPOLOGY_FILE = os.environ.get("CLUSTER_TOPOLOGY", "/opt/cluster/current-topology.json")
FLEET_FILE = os.environ.get("CLUSTER_FLEET", "/opt/cluster/fleet.json")
PANEL_NODE = os.environ.get("PANEL_NODE", "sparky")  # which topology node is local
FALLBACK_PROFILE = os.environ.get("CLUSTER_PROFILE", "empty")
WEBUI_URL = os.environ.get("WEBUI_URL", "http://127.0.0.1:8080/health")
PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:80/")
MODEL_ENDPOINT = os.environ.get("MODEL_ENDPOINT", "")
# How long an engine may sit "unit up, API down" before that stops being a weight load
# and starts being a fault. Mirrors the unit's TimeoutStartSec.
LOAD_TIMEOUT = float(os.environ.get("LOAD_TIMEOUT", "1200"))
# node -> ConnectX address, e.g. "snoopy=10.0.200.13 woodstock=10.0.200.14"
NODE_ADDRS = dict(
    pair.split("=", 1) for pair in os.environ.get("NODE_ADDRS", "").split() if "=" in pair
)

RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

_OK_STATES = {"active", "running"}

# An engine is not simply up or down (ADR-0018). Activation became a panel action, so
# the panel is now the surface you watch during a ten-to-twenty-minute weight load —
# a window in which "not serving" is the CORRECT state. Reporting that identically to
# a broken engine is wrong, and worse, it teaches you to ignore the red.
#
#   idle      nothing desired here (e.g. `empty` is live)
#   switching an activation is in flight — what's asked for is not yet what's live.
#             The reconciler rewrites current-topology.json only once the switch
#             LANDS, so until then the panel is describing the OUTGOING profile,
#             whose engines are legitimately stopping. Calling that "down" is true
#             but useless; the reader wants to know a switch is happening.
#   serving   unit up, API answering
#   loading   unit up, API not yet — and it has not been that way for long
#   stalled   …and it HAS been that way too long. A real fault, previously invisible:
#             minute 2 and minute 60 of a hung load looked identical.
#   down      desired, but the unit is not running
#   failsafe  the ADR-0009 recovery state
_SEVERITY = {"idle": 0, "serving": 0, "switching": 1, "loading": 1,
             "stalled": 2, "down": 3, "failsafe": 4}


def engine_phase(nodes, api_ok, elapsed, load_timeout=None):
    """Derive one engine's phase. Pure — see tests/test_control_panel.py."""
    load_timeout = LOAD_TIMEOUT if load_timeout is None else load_timeout
    if any(n.get("failsafe") for n in nodes):
        return "failsafe"
    if not nodes or not all(n.get("state") in _OK_STATES for n in nodes):
        return "down"
    if api_ok:
        return "serving"
    if elapsed is not None and elapsed < load_timeout:
        return "loading"
    return "stalled"


def overall_phase(phases):
    """Worst phase wins — one loading engine among healthy ones is still a switch in
    flight, and one failsafe among healthy ones is still a cluster needing attention."""
    return max(phases, key=lambda p: _SEVERITY.get(p, 0)) if phases else "idle"


def switching(requested, live):
    """True while an activation is in flight. `requested` is what was asked for (the
    request file, written first); `live` is what the reconciler last recorded as
    actually serving (written last). They differ exactly across a switch — and also
    when a switch FAILED and the fleet fell to `empty`, which is why the panel keeps
    showing both rather than collapsing them."""
    return bool(requested) and bool(live) and requested != live

ACTIONS = {
    "activate": {
        "label": "Activate",
        "danger": True,
        "desc": ("Make the selected profile the live one. Stops whatever is serving "
                 "fleet-wide, then starts the selected profile's engines. The model "
                 "load takes minutes; chat is unavailable until it finishes."),
    },
    "reactivate": {
        "label": "Restart engines",
        "danger": True,
        "desc": ("Re-activate the current profile, restarting its engines even though "
                 "nothing changed. The recovery gesture after a fail-safe boot, and how "
                 "you apply a deploy that re-rendered the live engine's definition."),
    },
}

ACTION_LIST = [{"name": k, **{f: v[f] for f in ("label", "danger", "desc")}}
               for k, v in ACTIONS.items()]


def available_profiles():
    """Activatable profile names, straight from the allowlist the reconciler
    re-validates against — so the dropdown cannot offer something an activation
    would then refuse. `deploy` writes it; parked (`blocked: true`) profiles keep
    their weights but are absent here."""
    names = ["empty"]
    try:
        for line in ALLOWLIST_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line not in names:
                names.append(line)
    except OSError:
        pass
    return names


def requested_profile():
    """What was last asked for (the request file), independent of what came up."""
    try:
        return DESIRED_PROFILE.read_text().strip().splitlines()[0].strip() or FALLBACK_PROFILE
    except (OSError, IndexError):
        return FALLBACK_PROFILE


def current_profile():
    """Profile name from the live topology, falling back to the request."""
    topo = load_topology()
    if topo and topo.get("profile"):
        return topo["profile"]
    return requested_profile()


def _build_cmd(name, profile):
    """The panel's whole command surface. Both forms are the SAME fixed program:
    write the request (no sudo), then trigger the reconciler through the
    single-command sudoers entry. Nothing else is invocable from here."""
    if name == "activate":
        return (f"printf '%s\\n' {shlex.quote(profile)} > {shlex.quote(str(DESIRED_PROFILE))}"
                f" && sudo -n {shlex.quote(ACTIVATE_BIN)}")
    if name == "reactivate":
        return f"sudo -n {shlex.quote(ACTIVATE_BIN)} --force"
    return None


def _run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout.strip() or p.stderr.strip() or "?")
    except Exception as e:  # noqa: BLE001 - surface any failure as a status string
        return f"error: {e}"


def _remote_status(node):
    """A worker's engine states, over the forced-command channel's read-only verb.
    The panel holds no general-purpose remote command — `status` is the only thing
    besides an activation request that key can ask for."""
    addr = NODE_ADDRS.get(node)
    if not addr:
        return None
    out = _run(["ssh", "-i", ACTIVATE_SSH_KEY, "-l", ACTIVATE_SSH_USER,
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                "-o", "StrictHostKeyChecking=accept-new", addr, "status"], timeout=10)
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return None


def _local_status():
    """This node's engine states, straight from the reconciler's read-only path
    (no sudo needed — reading systemd state is not privileged)."""
    out = _run([ACTIVATE_BIN, "--status"], timeout=10)
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return None


def node_engine_states():
    """{node: {engine: {...}}} across the fleet, gathered once per status render."""
    states = {}
    local = _local_status()
    if local:
        states[local.get("node", PANEL_NODE)] = {e["name"]: e for e in local.get("engines", [])}
    for node in NODE_ADDRS:
        remote = _remote_status(node)
        if remote:
            states[remote.get("node", node)] = {e["name"]: e for e in remote.get("engines", [])}
    return states


def _http_ok(url, timeout=4):
    """Service liveness over HTTP. Deliberately not `docker inspect`: the panel is
    not in the docker group, because docker group membership is root-equivalent and
    would re-open exactly the hole ADR-0018 closes."""
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        return "running" if r.status_code < 500 else f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return f"down ({type(e).__name__})"


def _vllm_api(base):
    try:
        r = httpx.get(f"{base}/v1/models", timeout=4)
        if r.status_code == 200:
            data = r.json().get("data", [])
            return True, ", ".join(d["id"] for d in data) if data else "(no models)"
        return False, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def load_topology():
    """The live topology, written by the reconciler when an activation lands."""
    try:
        with open(TOPOLOGY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def load_fleet():
    """What the last deploy provisioned (the allowlist + per-node placement)."""
    try:
        with open(FLEET_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def topology_engines():
    return (load_topology() or {}).get("engines", [])


def gather():
    topo = load_topology()
    services = [("Open WebUI", _http_ok(WEBUI_URL)), ("Proxy", _http_ok(PROXY_URL))]
    if MODEL_ENDPOINT:
        services.append(("Model endpoint", _http_ok(f"{MODEL_ENDPOINT}/health")))
    states = node_engine_states()
    requested = requested_profile()

    if not topo:
        # No activation has landed yet: show what each node reports directly.
        units = [(f"vllm@{name} ({node})", e["state"])
                 for node, engines in sorted(states.items())
                 for name, e in sorted(engines.items())]
        return {"has_topology": False, "profile": None, "requested": requested,
                "engines": [], "units": units, "services": services,
                "ok_states": _OK_STATES, "failsafe": False, "pending": [],
                "phase": "idle"}

    mid_switch = switching(requested, topo.get("profile"))
    engines, any_failsafe, pending = [], False, []
    for e in topo.get("engines", []):
        nodes, e_failsafe, elapsed = [], False, None
        for n in e["nodes"]:
            reported = states.get(n, {}).get(e["name"], {})
            state = reported.get("state", "unknown")
            # The API node's clock is the meaningful one — it is the rank that has to
            # finish loading before anything answers.
            if n == e.get("api_node") or elapsed is None:
                elapsed = reported.get("active_for", elapsed)
            # The fail-safe recovery state (ADR-0009): the unit is down and its
            # unclean-shutdown marker survived, so ConditionPathExists skipped it on
            # purpose and the node came up empty and reachable.
            tripped = bool(reported.get("failsafe"))
            e_failsafe = e_failsafe or tripped
            if reported.get("pending"):
                pending.append(f"{e['name']} on {n}")
            nodes.append({"node": n, "state": state, "failsafe": tripped})
        any_failsafe = any_failsafe or e_failsafe
        api_ok, api_model = _vllm_api(e.get("api_url", ""))
        phase = engine_phase(nodes, api_ok, elapsed)
        # Mid-switch, the outgoing engines are meant to be stopping. Don't dress that
        # up as a fault — but never mask a genuine fail-safe, which is the one state
        # that must survive every other consideration.
        if mid_switch and phase in ("down", "stalled", "loading"):
            phase = "switching"
        engines.append({"name": e["name"], "kind": e.get("kind", "vllm"), "port": e.get("port"),
                        "nodes": nodes, "model": e.get("served_as") or e.get("model") or "—",
                        "failsafe": e_failsafe, "api_ok": api_ok, "api_model": api_model,
                        "phase": phase,
                        "elapsed": None if elapsed is None else int(elapsed)})
    return {"has_topology": True, "profile": topo.get("profile"),
            "requested": requested, "switching": mid_switch,
            "deployed_at": topo.get("activated_at") or topo.get("deployed_at"),
            "engines": engines, "failsafe": any_failsafe, "pending": sorted(set(pending)),
            "phase": overall_phase([e["phase"] for e in engines]),
            "services": services, "ok_states": _OK_STATES}


# --- action runner ---------------------------------------------------------

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tail(path, n=500):
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except FileNotFoundError:
        return ""


def current_run():
    """Reconstruct the latest run's state from the filesystem (survives restarts)."""
    ptr = RUNS_DIR / "current"
    if not ptr.exists():
        return None
    d = RUNS_DIR / ptr.read_text().strip()
    meta_file = d / "meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text())
    rc_file = d / "done.rc"
    if rc_file.exists():
        code = int((rc_file.read_text().strip() or "1"))
        status = "success" if code == 0 else "failed"
    else:
        pid = int((d / "pid").read_text().strip() or "0")
        status = "running" if _pid_alive(pid) else "unknown"
        code = None
    return {**meta, "status": status, "code": code, "log": _tail(d / "output.log")}


def start_run(name, label, cmd):
    state = current_run()
    if state and state["status"] == "running":
        raise HTTPException(409, "A run is already in progress.")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = RUNS_DIR / run_id
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"id": run_id, "name": name, "label": label,
         "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}))
    log, rc = d / "output.log", d / "done.rc"
    wrapped = f"({cmd}) > {shlex.quote(str(log))} 2>&1; echo $? > {shlex.quote(str(rc))}"
    proc = subprocess.Popen(["bash", "-c", wrapped], start_new_session=True)
    (d / "pid").write_text(str(proc.pid))
    (RUNS_DIR / "current").write_text(run_id)
    return current_run()


# --- views -----------------------------------------------------------------

def _ctx(request, **extra):
    return {"root": request.scope.get("root_path", ""), **extra}


def _actions_ctx(request):
    return _ctx(request, actions=ACTION_LIST, engines=topology_engines(),
                profile=current_profile(), profiles=available_profiles(),
                fleet=load_fleet())


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(
        request, s=gather(), actions=ACTION_LIST, engines=topology_engines(),
        profile=current_profile(), profiles=available_profiles(),
        fleet=load_fleet(), run=current_run()))


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    return templates.TemplateResponse(request, "_status.html", _ctx(request, s=gather()))


@app.get("/health.json")
def health_json():
    """Lightweight JSON for the landing page to detect the fail-safe recovery state
    (ADR-0009) without rendering the whole panel. Caddy exposes this one read-only
    sliver outside /admin's basic_auth."""
    s = gather()
    return {"failsafe": s.get("failsafe", False), "profile": s.get("profile"),
            "phase": s.get("phase", "idle"),
            "has_topology": s.get("has_topology", False)}


def status_dict():
    """gather() as a JSON-clean, machine-readable dict — the no-sudo live-status
    surface for the CLI (`sparky status`) and agents. Drops the template-only
    `ok_states` set, normalizes `services` tuples to objects, and derives a single
    top-line `ok`: everything the activation intended is up and serving, and nothing
    is in the fail-safe recovery state (ADR-0009). `ok` is only meaningful when a
    topology is recorded (has_topology); it's False in the no-topology fallback."""
    s = gather()
    services = [{"name": n, "state": st} for n, st in s.get("services", [])]
    if not s.get("has_topology"):
        return {"has_topology": False, "ok": False, "phase": "idle", "profile": None,
                "requested": s.get("requested"), "failsafe": s.get("failsafe", False),
                "engines": [], "services": services, "pending": [],
                "units": [{"unit": u, "state": st} for u, st in s.get("units", [])]}
    engines = s.get("engines", [])
    # ok = nothing the activation intended is unhealthy and nothing tripped fail-safe.
    # all() over no engines is True, so an activated `empty` profile is healthy (it's
    # intended to serve nothing) — only a down/unreachable engine or fail-safe is not-ok.
    ok = (not s.get("failsafe", False) and all(
        e.get("api_ok") and all(n["state"] in _OK_STATES for n in e["nodes"])
        for e in engines))
    # `ok` deliberately stays "serving RIGHT NOW" — an agent gating on `sparky status`
    # must not proceed while weights load. `phase` is what says whether not-ok means
    # "wait" or "something is wrong".
    return {"has_topology": True, "ok": ok, "phase": s.get("phase", "idle"),
            "switching": s.get("switching", False), "profile": s.get("profile"),
            "requested": s.get("requested"), "deployed_at": s.get("deployed_at"),
            "failsafe": s.get("failsafe", False), "pending": s.get("pending", []),
            "engines": engines, "services": services}


@app.get("/status.json")
def status_json():
    """Full live cluster status as JSON — per-engine, per-node systemd state + API
    readiness + fail-safe, gathered live from every node over the bounded status
    channel. The machine-readable twin of the `/status` HTML view; `sparky status`
    reads it. Served at /admin/status.json."""
    return status_dict()


@app.get("/actions", response_class=HTMLResponse)
def actions(request: Request):
    run = current_run()
    if run and run["status"] == "running":
        return templates.TemplateResponse(request, "_run.html", _ctx(request, run=run))
    return templates.TemplateResponse(request, "_actions.html", _actions_ctx(request))


@app.post("/run/{name}", response_class=HTMLResponse)
async def run_action(request: Request, name: str, profile: str = Form("")):
    if name not in ACTIONS:
        raise HTTPException(404, "Unknown action.")
    profiles = available_profiles()
    # Validate against the allowlist. The reconciler re-validates anyway (on every
    # node), so this is about a clear error rather than about safety.
    if profile not in profiles:
        profile = current_profile()
    if name == "activate" and profile not in profiles:
        raise HTTPException(400, f"Not activatable: {profile!r}")
    cmd = _build_cmd(name, profile)
    a = ACTIONS[name]
    label = f"{a['label']} ({profile})"
    return templates.TemplateResponse(
        request, "_run.html", _ctx(request, run=start_run(name, label, cmd)))


@app.get("/run", response_class=HTMLResponse)
def run_view(request: Request):
    run = current_run()
    if not run:
        return templates.TemplateResponse(request, "_actions.html", _actions_ctx(request))
    return templates.TemplateResponse(request, "_run.html", _ctx(request, run=run))
