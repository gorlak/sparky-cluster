"""Cluster control panel.

P1: read-only status. P3: control actions (deploy / dry-run / restart / teardown).

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

See docs/control-interface.md.
"""
import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

VLLM_API = os.environ.get("VLLM_API", "http://127.0.0.1:8000")
WORKER_SSH = os.environ.get("WORKER_SSH", "")  # e.g. deploy@10.0.200.13
DEPLOY_SSH_KEY = os.environ.get("DEPLOY_SSH_KEY", "/home/deploy/.ssh/id_ed25519")
ANSIBLE_DIR = os.environ.get("ANSIBLE_DIR", "/opt/cluster/ansible")
PROFILE = os.environ.get("CLUSTER_PROFILE", "step")
RUNS_DIR = Path(os.environ.get("RUNS_DIR", "runs")).resolve()

RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

_OK_STATES = {"active", "running"}

_SSH = (
    f"ssh -i {shlex.quote(DEPLOY_SSH_KEY)} -o BatchMode=yes "
    f"-o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new"
)
_PROFILE_ARG = f"-e @profiles/{shlex.quote(PROFILE)}.yml"

# Restart every vLLM engine: worker node first, then the head/API node, so the
# API comes back last. Globs vllm-*.service so it covers whatever the topology
# placed on each node (systemctl resolves the glob against loaded units).
_LOCAL_RESTART = "sudo systemctl restart 'vllm-*.service'"
_RESTART_CMD = (
    f"{_SSH} {shlex.quote(WORKER_SSH)} 'sudo systemctl restart vllm-*.service' && {_LOCAL_RESTART}"
    if WORKER_SSH else _LOCAL_RESTART
)

# Each action maps to a fixed shell command built from server-side constants
# only (no request data is ever interpolated), so there's no injection surface.
ACTIONS = {
    "deploy": {
        "label": "Deploy",
        "danger": False,
        "desc": (
            f"Apply the {PROFILE} profile to the cluster. Idempotent — restarts "
            "services only if a unit actually changed."
        ),
        "cmd": f"cd {shlex.quote(ANSIBLE_DIR)} && ansible-playbook site.yml {_PROFILE_ARG}",
    },
    "check": {
        "label": "Dry run",
        "danger": False,
        "desc": "Show what a deploy would change (--check --diff). Makes no changes.",
        "cmd": f"cd {shlex.quote(ANSIBLE_DIR)} && ansible-playbook site.yml {_PROFILE_ARG} --check --diff",
    },
    "restart": {
        "label": "Restart vLLM",
        "danger": True,
        "desc": "Restart vLLM on the worker node then the head. Briefly interrupts serving.",
        "cmd": _RESTART_CMD,
    },
    "teardown": {
        "label": "Teardown vLLM",
        "danger": True,
        "desc": "Stop and disable vLLM on both nodes (frees the GPUs). Open WebUI stays up.",
        "cmd": f"cd {shlex.quote(ANSIBLE_DIR)} && ansible-playbook teardown.yml {_PROFILE_ARG}",
    },
}

ACTION_LIST = [{"name": k, **{f: v[f] for f in ("label", "danger", "desc")}}
               for k, v in ACTIONS.items()]


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


def _vllm_units(ssh=None):
    """Discover vllm-*.service units and their state on a node.

    Topology-driven: whichever engines the current profile placed on this node
    show up here, so the panel never hardcodes unit names that change when the
    serving topology changes. Returns a list of (unit, state).
    """
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


def _api():
    try:
        r = httpx.get(f"{VLLM_API}/v1/models", timeout=4)
        if r.status_code == 200:
            return True, r.json()["data"][0]["id"]
        return False, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def gather():
    api_ok, model = _api()
    services = [(f"{unit} (sparky)", state) for unit, state in _vllm_units()]
    if WORKER_SSH:
        services += [(f"{unit} (snoopy)", state) for unit, state in _vllm_units(WORKER_SSH)]
    services += [
        ("Open WebUI", _container("open-webui")),
        ("Caddy", _container("caddy")),
    ]
    return {"api_ok": api_ok, "model": model, "services": services, "ok_states": _OK_STATES}


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


def start_run(name):
    state = current_run()
    if state and state["status"] == "running":
        raise HTTPException(409, "A run is already in progress.")
    action = ACTIONS[name]
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = RUNS_DIR / run_id
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(
        {"id": run_id, "name": name, "label": action["label"],
         "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}))
    log, rc = d / "output.log", d / "done.rc"
    wrapped = f"({action['cmd']}) > {shlex.quote(str(log))} 2>&1; echo $? > {shlex.quote(str(rc))}"
    proc = subprocess.Popen(["bash", "-c", wrapped], start_new_session=True)
    (d / "pid").write_text(str(proc.pid))
    (RUNS_DIR / "current").write_text(run_id)
    return current_run()


# --- views -----------------------------------------------------------------

def _ctx(request, **extra):
    return {"root": request.scope.get("root_path", ""), **extra}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(
        request, s=gather(), actions=ACTION_LIST, profile=PROFILE, run=current_run()))


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    return templates.TemplateResponse(request, "_status.html", _ctx(request, s=gather()))


@app.get("/actions", response_class=HTMLResponse)
def actions(request: Request):
    run = current_run()
    if run and run["status"] == "running":
        return templates.TemplateResponse(request, "_run.html", _ctx(request, run=run))
    return templates.TemplateResponse(request, "_actions.html", _ctx(
        request, actions=ACTION_LIST, profile=PROFILE))


@app.post("/run/{name}", response_class=HTMLResponse)
def run_action(request: Request, name: str):
    if name not in ACTIONS:
        raise HTTPException(404, "Unknown action.")
    run = start_run(name)
    return templates.TemplateResponse(request, "_run.html", _ctx(request, run=run))


@app.get("/run", response_class=HTMLResponse)
def run_view(request: Request):
    run = current_run()
    if not run:
        return templates.TemplateResponse(request, "_actions.html", _ctx(
            request, actions=ACTION_LIST, profile=PROFILE))
    return templates.TemplateResponse(request, "_run.html", _ctx(request, run=run))
