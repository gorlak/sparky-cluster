"""Cluster control panel — P1: read-only status.

Runs as the `deploy` user (systemd), bound to 127.0.0.1; Caddy fronts it at
/admin. Config comes from environment (set by the systemd unit), so there are no
hardcoded hosts here. Control actions (deploy/teardown via ansible-runner) come
in a later phase — see docs/control-interface.md.
"""
import os
import subprocess

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

VLLM_API = os.environ.get("VLLM_API", "http://127.0.0.1:8000")
WORKER_SSH = os.environ.get("WORKER_SSH", "")  # e.g. deploy@10.0.200.13
DEPLOY_SSH_KEY = os.environ.get("DEPLOY_SSH_KEY", "/home/deploy/.ssh/id_ed25519")

app = FastAPI()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

_OK_STATES = {"active", "running"}


def _run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout.strip() or p.stderr.strip() or "?")
    except Exception as e:  # noqa: BLE001 - surface any failure as a status string
        return f"error: {e}"


def _systemctl(unit, ssh=None):
    check = ["systemctl", "is-active", unit]
    if ssh:
        return _run(["ssh", "-i", DEPLOY_SSH_KEY, "-o", "BatchMode=yes",
                     "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=accept-new",
                     ssh, *check])
    return _run(check)


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
    services = [
        ("vLLM head (sparky)", _systemctl("vllm.service")),
        ("vLLM worker (snoopy)", _systemctl("vllm-worker.service", WORKER_SSH) if WORKER_SSH else "n/a"),
        ("Open WebUI", _container("open-webui")),
        ("Caddy", _container("caddy")),
    ]
    return {"api_ok": api_ok, "model": model, "services": services, "ok_states": _OK_STATES}


def _ctx(request):
    # Starlette injects `request` into the context itself (new signature below).
    return {"root": request.scope.get("root_path", ""), "s": gather()}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


@app.get("/status", response_class=HTMLResponse)
def status(request: Request):
    return templates.TemplateResponse(request, "_status.html", _ctx(request))
