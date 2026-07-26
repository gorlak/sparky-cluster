"""Unit tests for the cluster control panel (ADR-0011 Layer 3).

FastAPI TestClient over roles/control-panel/files/app/main.py with the system
calls (_is_active / _marker_present / _vllm_api / _container / load_topology)
mocked — no hardware. Focus: the ADR-0009 fail-safe detection in gather(), the
command builder, the profile path-traversal guard, and /health.json.

The app reads config from env at import and resolves app/static + app/templates
relative to cwd, so the fixture sets env and chdirs into the app's files/ dir,
then loads main.py as a standalone module.
"""

import importlib.util
from pathlib import Path

import pytest

APP_FILES = Path(__file__).resolve().parent.parent / "ansible/roles/control-panel/files"


@pytest.fixture()
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("ANSIBLE_DIR", str(tmp_path / "ansible"))
    monkeypatch.setenv("CLUSTER_TOPOLOGY", str(tmp_path / "topo.json"))
    monkeypatch.setenv("CLUSTER_PROFILE", "step")
    monkeypatch.setenv("WORKER_SSH", "deploy@10.0.200.13")
    monkeypatch.setenv("PANEL_NODE", "sparky")
    (tmp_path / "ansible" / "profiles").mkdir(parents=True)
    monkeypatch.chdir(APP_FILES)  # so app/static + app/templates resolve

    spec = importlib.util.spec_from_file_location("panel_main", APP_FILES / "app" / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)

    from fastapi.testclient import TestClient

    return main, TestClient(main.app)


def _topo(marker="/opt/vllm/state/vllm-e.running", nodes=("sparky", "snoopy")):
    return {"profile": "p", "engines": [{
        "name": "e", "kind": "vllm", "unit": "vllm-e.service", "nodes": list(nodes),
        "marker": marker, "api_url": "http://x:8000", "served_as": "e",
    }]}


def _mock_system(main, monkeypatch, *, state, marker, marker_calls=None):
    monkeypatch.setattr(main, "load_topology", lambda: _topo())
    monkeypatch.setattr(main, "_is_active", lambda unit, ssh=None: state)
    monkeypatch.setattr(main, "_vllm_api", lambda base: (False, "down"))
    monkeypatch.setattr(main, "_container", lambda name: "running")

    def _marker(path, ssh=None):
        if marker_calls is not None:
            marker_calls.append(path)
        return marker

    monkeypatch.setattr(main, "_marker_present", _marker)


# --- gather() fail-safe detection (ADR-0009) -------------------------------

def test_failsafe_when_marker_present_and_unit_down(panel, monkeypatch):
    main, _ = panel
    _mock_system(main, monkeypatch, state="inactive", marker=True)
    s = main.gather()
    assert s["failsafe"] is True
    assert s["engines"][0]["failsafe"] is True


def test_not_failsafe_when_unit_active_and_marker_not_even_checked(panel, monkeypatch):
    main, _ = panel
    calls = []
    _mock_system(main, monkeypatch, state="active", marker=True, marker_calls=calls)
    s = main.gather()
    assert s["failsafe"] is False
    assert calls == []  # short-circuit: don't probe the marker when the unit is up


def test_not_failsafe_when_marker_absent(panel, monkeypatch):
    main, _ = panel
    _mock_system(main, monkeypatch, state="inactive", marker=False)
    assert main.gather()["failsafe"] is False


def test_not_failsafe_when_engine_has_no_marker(panel, monkeypatch):
    main, _ = panel
    monkeypatch.setattr(main, "load_topology", lambda: _topo(marker=None))
    monkeypatch.setattr(main, "_is_active", lambda unit, ssh=None: "inactive")
    monkeypatch.setattr(main, "_vllm_api", lambda base: (False, "x"))
    monkeypatch.setattr(main, "_container", lambda name: "running")
    assert main.gather()["failsafe"] is False


def test_gather_fallback_when_no_topology(panel, monkeypatch):
    main, _ = panel
    monkeypatch.setattr(main, "load_topology", lambda: None)
    monkeypatch.setattr(main, "_vllm_units", lambda ssh=None: [("vllm-e.service", "active")])
    monkeypatch.setattr(main, "_container", lambda name: "running")
    s = main.gather()
    assert s["has_topology"] is False
    assert s["failsafe"] is False


# --- /health.json ----------------------------------------------------------

def test_health_json_reflects_failsafe(panel, monkeypatch):
    main, client = panel
    _mock_system(main, monkeypatch, state="inactive", marker=True)
    r = client.get("/health.json")
    assert r.status_code == 200
    assert r.json() == {"failsafe": True, "profile": "p", "has_topology": True}


# --- /status.json (the no-sudo agent surface) ------------------------------

def _mock_status(main, monkeypatch, *, state, api_ok, marker=False, topo=None):
    monkeypatch.setattr(main, "load_topology", lambda: topo if topo is not None else _topo())
    monkeypatch.setattr(main, "_is_active", lambda unit, ssh=None: state)
    monkeypatch.setattr(main, "_vllm_api",
                        lambda base: (api_ok, "step-3.5-flash" if api_ok else "down"))
    monkeypatch.setattr(main, "_container", lambda name: "running")
    monkeypatch.setattr(main, "_marker_present", lambda path, ssh=None: marker)


def test_status_json_ok_when_healthy(panel, monkeypatch):
    main, client = panel
    _mock_status(main, monkeypatch, state="active", api_ok=True)
    j = client.get("/status.json").json()
    assert j["ok"] is True and j["has_topology"] is True and j["failsafe"] is False
    assert j["profile"] == "p"
    nodes = j["engines"][0]["nodes"]
    assert [n["node"] for n in nodes] == ["sparky", "snoopy"]
    assert all(n["state"] == "active" for n in nodes)
    # JSON-clean: no template-only set, services normalized to objects.
    assert "ok_states" not in j
    assert j["services"] and all({"name", "state"} <= s.keys() for s in j["services"])


def test_status_json_not_ok_when_api_down(panel, monkeypatch):
    main, client = panel
    _mock_status(main, monkeypatch, state="active", api_ok=False)
    assert client.get("/status.json").json()["ok"] is False


def test_status_json_not_ok_and_failsafe(panel, monkeypatch):
    main, client = panel
    _mock_status(main, monkeypatch, state="inactive", api_ok=False, marker=True)
    j = client.get("/status.json").json()
    assert j["ok"] is False and j["failsafe"] is True
    assert j["engines"][0]["nodes"][0]["failsafe"] is True


def test_status_json_ok_for_empty_profile(panel, monkeypatch):
    main, client = panel
    _mock_status(main, monkeypatch, state="active", api_ok=True,
                 topo={"profile": "empty", "engines": []})
    j = client.get("/status.json").json()
    # all() over zero engines is true — an intended-empty deploy is healthy.
    assert j["has_topology"] is True and j["ok"] is True and j["engines"] == []


def test_status_json_no_topology_is_not_ok(panel, monkeypatch):
    main, client = panel
    monkeypatch.setattr(main, "load_topology", lambda: None)
    monkeypatch.setattr(main, "_vllm_units", lambda ssh=None: [("vllm-e.service", "active")])
    monkeypatch.setattr(main, "_container", lambda name: "running")
    j = client.get("/status.json").json()
    assert j["has_topology"] is False and j["ok"] is False
    assert j["units"] and {"unit", "state"} <= j["units"][0].keys()


# --- _build_cmd ------------------------------------------------------------

def test_build_cmd_per_action(panel):
    main, _ = panel
    assert main._build_cmd("deploy", "minimax").endswith("site.yml -e @profiles/minimax.yml")
    assert "--check --diff" in main._build_cmd("check", "minimax")
    assert main._build_cmd("teardown", "anything").endswith("teardown.yml")
    assert main._build_cmd("bogus", "x") is None


def test_build_cmd_quotes_profile(panel):
    main, _ = panel
    assert "'weird; rm -rf'" in main._build_cmd("deploy", "weird; rm -rf")


# --- available_profiles blocked filter -------------------------------------

def test_available_profiles_skips_blocked(panel):
    main, _ = panel
    profiles = Path(main.ANSIBLE_DIR) / "profiles"
    (profiles / "minimax.yml").write_text("profile_name: minimax\n")
    (profiles / "step-3.7.yml").write_text("profile_name: step-3.7\nblocked: true\n")
    assert main.available_profiles() == ["minimax"]


# --- run_action guards -----------------------------------------------------

def test_run_action_unknown_action_404(panel, monkeypatch):
    main, client = panel
    monkeypatch.setattr(main, "start_run", lambda *a: pytest.fail("must not launch"))
    assert client.post("/run/bogus").status_code == 404


def test_run_action_path_traversal_profile_rejected(panel, monkeypatch):
    main, client = panel
    # one valid profile; a traversal attempt is not in it and the env fallback
    # ('step') isn't either -> 400, no run launched.
    (Path(main.ANSIBLE_DIR) / "profiles" / "minimax.yml").write_text("x")
    monkeypatch.setattr(main, "start_run", lambda *a: pytest.fail("must not launch"))
    r = client.post("/run/deploy", data={"profile": "../../etc/passwd"})
    assert r.status_code == 400
