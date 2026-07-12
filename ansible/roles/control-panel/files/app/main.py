"""Cluster control panel.

Read-only status + control actions (deploy / dry-run / teardown / per-engine
restart). Topology-aware: status and the per-engine restart buttons are driven by
the live serving topology recorded at $CLUSTER_TOPOLOGY (current-topology.json,
written by the deploy and cleared by teardown). Falls back to unit discovery when
that file is absent (e.g. mid-deploy before it's written).

Runs as the `deploy` user (systemd), bound to 127.0.0.1; Caddy fronts it at
/admin. `deploy` has NOPASSWD sudo and owns the published playbooks at
$ANSIBLE_DIR, so it can run ansible-playbook directly (this is `make deploy`
minus the repo->live publish step). Config comes from environment set by the
systemd unit — nothing host-specific is hardcoded here.

Actions run as DETACHED processes (own session) that write their combined
output to <run>/output.log and their exit code to <run>/done.rc when finished.
That way a run survives the panel restarting mid-deploy (the deploy action
re-runs the control-panel role, which may restart this service): a fresh panel
process reconstructs the run's status from the filesystem. The unit sets
KillMode=process so a restart only kills uvicorn, not the in-flight child.

See docs/control-interface.md and docs/serving-topology.md.
"""
import json
import os
import re
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

WORKER_SSH = os.environ.get("WORKER_SSH", "")  # e.g. deploy@10.0.200.13
DEPLOY_SSH_KEY = os.environ.get("DEPLOY_SSH_KEY", "/home/deploy/.ssh/id_ed25519")
ANSIBLE_DIR = os.environ.get("ANSIBLE_DIR", "/opt/cluster/ansible")
PROFILE = os.environ.get("CLUSTER_PROFILE", "step")
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "runs")).resolve()
TOPOLOGY_FILE = os.environ.get("CLUSTER_TOPOLOGY", "/opt/cluster/current-topology.json")
PANEL_NODE = os.environ.get("PANEL_NODE", "sparky")  # which topology node is local

RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

_OK_STATES = {"active", "running"}

_SSH = (
    f"ssh -i {shlex.quote(DEPLOY_SSH_KEY)} -o BatchMode=yes "
    f"-o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new"
)

# Action metadata — commands are built dynamically at request time so that the
# profile can be chosen per-action from the UI (not baked in at startup).
ACTIONS = {
    "deploy": {
        "label": "Deploy",
        "danger": False,
        "desc": "Apply the selected profile to the cluster. Idempotent — restarts services only if a unit changed.",
    },
    "check": {
        "label": "Dry run",
        "danger": False,
        "desc": "Show what deploying the selected profile would change (--check --diff). Makes no changes.",
    },
    "teardown": {
        "label": "Teardown",
        "danger": True,
        "desc": "Stop and disable all vLLM + Ollama engines on both nodes (frees the GPUs). Open WebUI stays up. Profile-agnostic — stops whatever is running.",
    },
}

ACTION_LIST = [{"name": k, **{f: v[f] for f in ("label", "danger", "desc")}}
               for k, v in ACTIONS.items()]


def available_profiles():
    """Deployable profile names, sorted. Profiles that declare a top-level
    `blocked: true` are parked candidates (e.g. waiting on upstream support) and are
    hidden from the deploy UI — a deliberate CLI `make deploy PROFILE=<x>` still works."""
    p = Path(ANSIBLE_DIR) / "profiles"
    try:
        out = []
        for f in sorted(p.glob("*.yml")):
            try:
                if re.search(r"(?m)^blocked:\s*true\b", f.read_text()):
                    continue
            except OSError:
                pass
            out.append(f.stem)
        return out or [PROFILE]
    except OSError:
        return [PROFILE]


def current_profile():
    """Profile name from the live topology, fallback to env default."""
    topo = load_topology()
    if topo and topo.get("profile"):
        return topo["profile"]
    return PROFILE


def _build_cmd(name, profile):
    base = f"cd {shlex.quote(ANSIBLE_DIR)} && ansible-playbook"
    if name == "deploy":
        return f"{base} site.yml -e @profiles/{shlex.quote(profile)}.yml"
    if name == "check":
        return f"{base} site.yml -e @profiles/{shlex.quote(profile)}.yml --check --diff"
    if name == "teardown":
        return f"{base} teardown.yml"
    return None


def _run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout.strip() or p.stderr.strip() or "?")
    except Exception as e:  # noqa: BLE001 - surface any failure as a status string
        return f"error: {e}"


def _ssh_cmd(ssh, *args):
    return ["ssh", "-i", DEPLOY_SSH_KEY, "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=accept-new",
            ssh, *args]


def _ssh_for(node):
    """SSH target for a topology node, or None when it's the local (panel) node."""
    return None if node == PANEL_NODE else WORKER_SSH


def _is_active(unit, ssh=None):
    cmd = ["systemctl", "is-active", unit]
    return _run(_ssh_cmd(ssh, *cmd) if ssh else cmd)


def _marker_present(path, ssh=None):
    """True if the fail-safe boot marker (ADR-0009) exists on the node. Present
    while the unit is *down* means an unclean shutdown tripped ConditionPathExists
    and the node came up empty on purpose — the recovery state."""
    check = f"test -f {shlex.quote(path)} && echo yes || echo no"
    if ssh:
        out = _run(["ssh", "-i", DEPLOY_SSH_KEY, "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=accept-new",
                    ssh, check])
    else:
        out = _run(["sh", "-c", check])
    return out.strip() == "yes"


def _vllm_units(ssh=None):
    """Discover vllm-*.service units and their state on a node (fallback only)."""
    cmd = ["systemctl", "list-units", "--type=service", "--all",
           "--plain", "--no-legend", "vllm-*.service"]
    units = []
    for line in _run(_ssh_cmd(ssh, *cmd) if ssh else cmd).splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].endswith(".service"):
            units.append((parts[0], parts[2]))
    return units


def _container(name):
    return _run(["docker", "inspect", "-f", "{{.State.Status}}", name])


def _vllm_api(base):
    try:
        r = httpx.get(f"{base}/v1/models", timeout=4)
        if r.status_code == 200:
            data = r.json().get("data", [])
            return True, (data[0]["id"] if data else "(no models)")
        return False, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _ollama_api(base):
    try:
        r = httpx.get(f"{base}/api/version", timeout=4)
        return r.status_code == 200, ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def load_topology():
    """The live serving topology recorded by the last deploy, or None."""
    try:
        with open(TOPOLOGY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def topology_engines():
    return (load_topology() or {}).get("engines", [])


def gather():
    topo = load_topology()
    services = [("Open WebUI", _container("open-webui")), ("Caddy", _container("caddy"))]
    if not topo:
        # Fallback (no recorded topology, e.g. mid-deploy): show discovered units.
        units = [(f"{u} (sparky)", st) for u, st in _vllm_units()]
        if WORKER_SSH:
            units += [(f"{u} (snoopy)", st) for u, st in _vllm_units(WORKER_SSH)]
        return {"has_topology": False, "profile": None, "engines": [],
                "units": units, "services": services, "ok_states": _OK_STATES,
                "failsafe": False}

    engines = []
    any_failsafe = False
    for e in topo.get("engines", []):
        marker = e.get("marker")
        nodes = []
        e_failsafe = False
        for n in e["nodes"]:
            ssh = _ssh_for(n)
            state = _is_active(e["unit"], ssh)
            # Only the marker-present-while-down combination is the fail-safe
            # state; skip the (remote) marker check entirely when the unit is up.
            tripped = (bool(marker) and state not in _OK_STATES
                       and _marker_present(marker, ssh))
            e_failsafe = e_failsafe or tripped
            nodes.append({"node": n, "state": state, "failsafe": tripped})
        any_failsafe = any_failsafe or e_failsafe
        if e["kind"] == "vllm":
            api_ok, api_model = _vllm_api(e.get("api_url", ""))
            model = e.get("served_as") or e.get("model") or "—"
        else:
            api_ok, _ = _ollama_api(e.get("api_url", ""))
            model = api_model = ", ".join(e.get("models", [])) or "—"
        engines.append({"name": e["name"], "kind": e["kind"], "port": e.get("port"),
                        "nodes": nodes, "model": model, "failsafe": e_failsafe,
                        "api_ok": api_ok, "api_model": api_model})
    return {"has_topology": True, "profile": topo.get("profile"),
            "deployed_at": topo.get("deployed_at"), "engines": engines,
            "failsafe": any_failsafe,
            "services": services, "ok_states": _OK_STATES}


def _engine_restart_cmd(engine):
    """Restart one engine's unit across its nodes, worker(s) first, API node last,
    so the API endpoint returns last. `engine` comes from the trusted state file
    (Ansible-written), so its unit/node names are safe to interpolate."""
    unit = engine["unit"]
    api_node = engine.get("api_node", engine["nodes"][0])
    ordered = sorted(engine["nodes"], key=lambda n: n == api_node)  # api node last
    parts = []
    for node in ordered:
        if _ssh_for(node) is None:
            parts.append(f"sudo systemctl restart {shlex.quote(unit)}")
        else:
            parts.append(f"{_SSH} {shlex.quote(_ssh_for(node))} 'sudo systemctl restart {unit}'")
    return " && ".join(parts)


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
    deployed = current_profile()
    return _ctx(request, actions=ACTION_LIST, engines=topology_engines(),
                profile=deployed, profiles=available_profiles())


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    deployed = current_profile()
    return templates.TemplateResponse(request, "index.html", _ctx(
        request, s=gather(), actions=ACTION_LIST, engines=topology_engines(),
        profile=deployed, profiles=available_profiles(), run=current_run()))


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    return templates.TemplateResponse(request, "_status.html", _ctx(request, s=gather()))


@app.get("/health.json")
def health_json():
    """Lightweight JSON for the landing page to detect the fail-safe recovery
    state (ADR-0009) without rendering the whole panel. Served at /admin/health.json."""
    s = gather()
    return {"failsafe": s.get("failsafe", False), "profile": s.get("profile"),
            "has_topology": s.get("has_topology", False)}


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
    # Validate profile against known list — prevents path traversal / injection.
    if profile not in profiles:
        profile = current_profile()
    if profile not in profiles:
        raise HTTPException(400, f"Unknown profile: {profile!r}")
    cmd = _build_cmd(name, profile)
    a = ACTIONS[name]
    label = a["label"] if name == "teardown" else f"{a['label']} ({profile})"
    run = start_run(name, label, cmd)
    return templates.TemplateResponse(request, "_run.html", _ctx(request, run=run))


@app.post("/run/engine/{name}", response_class=HTMLResponse)
def run_engine(request: Request, name: str):
    engine = next((e for e in topology_engines() if e["name"] == name), None)
    if engine is None:
        raise HTTPException(404, "Unknown engine.")
    run = start_run(f"restart-{name}", f"Restart {name}", _engine_restart_cmd(engine))
    return templates.TemplateResponse(request, "_run.html", _ctx(request, run=run))


@app.get("/run", response_class=HTMLResponse)
def run_view(request: Request):
    run = current_run()
    if not run:
        return templates.TemplateResponse(request, "_actions.html", _actions_ctx(request))
    return templates.TemplateResponse(request, "_run.html", _ctx(request, run=run))
