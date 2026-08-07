"""Unit tests for the cluster control panel (ADR-0011 Layer 3).

FastAPI TestClient over roles/control-panel/files/app/main.py with the system calls
(node_engine_states / _vllm_api / _http_ok / load_topology) mocked — no hardware.

Focus since ADR-0018: the panel has no path to root, so the tests that matter are
(a) its command surface really is just the two reconciler triggers, (b) the ADR-0009
fail-safe detection now reads from the bounded status channel, and (c) the activate
dropdown comes from the deploy-written allowlist rather than a directory scan.

The app reads config from env at import and resolves app/static + app/templates
relative to cwd, so the fixture sets env and chdirs into the app's files/ dir, then
loads main.py as a standalone module.
"""

import importlib.util
from pathlib import Path

import pytest

APP_FILES = Path(__file__).resolve().parent.parent / "ansible/roles/control-panel/files"


@pytest.fixture()
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CLUSTER_TOPOLOGY", str(tmp_path / "topo.json"))
    monkeypatch.setenv("CLUSTER_FLEET", str(tmp_path / "fleet.json"))
    monkeypatch.setenv("DESIRED_PROFILE", str(tmp_path / "desired-profile"))
    monkeypatch.setenv("ALLOWLIST_FILE", str(tmp_path / "allowlist"))
    monkeypatch.setenv("ACTIVATE_BIN", "/usr/local/sbin/vllm-activate")
    monkeypatch.setenv("CLUSTER_PROFILE", "empty")
    monkeypatch.setenv("NODE_ADDRS", "snoopy=10.0.200.13")
    monkeypatch.setenv("PANEL_NODE", "sparky")
    (tmp_path / "desired-profile").write_text("p\n")
    monkeypatch.chdir(APP_FILES)  # so app/static + app/templates resolve

    spec = importlib.util.spec_from_file_location("panel_main", APP_FILES / "app" / "main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)

    from fastapi.testclient import TestClient

    return main, TestClient(main.app)


def _topo(nodes=("sparky", "snoopy")):
    return {"profile": "p", "engines": [{
        "name": "e", "kind": "vllm", "unit": "vllm@e.service", "nodes": list(nodes),
        "api_url": "http://x:8000", "served_as": "e",
    }]}


def _states(state, *, failsafe=False, pending=False, nodes=("sparky", "snoopy")):
    """What the bounded `status` channel reports for each node."""
    return {n: {"e": {"name": "e", "state": state, "desired": True,
                      "failsafe": failsafe, "pending": pending}} for n in nodes}


def _mock_system(main, monkeypatch, *, state, failsafe=False, pending=False,
                 api_ok=False, topo=...):
    monkeypatch.setattr(main, "load_topology",
                        lambda: _topo() if topo is ... else topo)
    monkeypatch.setattr(main, "node_engine_states",
                        lambda: _states(state, failsafe=failsafe, pending=pending))
    monkeypatch.setattr(main, "_vllm_api",
                        lambda base: (api_ok, "sparky, e" if api_ok else "down"))
    monkeypatch.setattr(main, "_http_ok", lambda url, timeout=4: "running")


# --- gather() fail-safe detection (ADR-0009) -------------------------------

def test_failsafe_when_the_node_reports_it(panel, monkeypatch):
    main, _ = panel
    _mock_system(main, monkeypatch, state="inactive", failsafe=True)
    s = main.gather()
    assert s["failsafe"] is True
    assert s["engines"][0]["failsafe"] is True


def test_not_failsafe_when_the_unit_is_up(panel, monkeypatch):
    main, _ = panel
    _mock_system(main, monkeypatch, state="active", api_ok=True)
    assert main.gather()["failsafe"] is False


def test_pending_definition_change_is_surfaced(panel, monkeypatch):
    """A deploy re-rendered a serving engine. Deploy is selection-neutral, so the new
    definition is installed but not running — the panel has to say so, or the change
    looks applied when it isn't."""
    main, _ = panel
    _mock_system(main, monkeypatch, state="active", api_ok=True, pending=True)
    assert main.gather()["pending"] == ["e on snoopy", "e on sparky"]


def test_gather_fallback_when_nothing_has_been_activated(panel, monkeypatch):
    main, _ = panel
    _mock_system(main, monkeypatch, state="active", topo=None)
    s = main.gather()
    assert s["has_topology"] is False
    assert s["failsafe"] is False
    assert ("vllm@e (sparky)", "active") in s["units"]


# --- /health.json (the one route outside basic_auth) -----------------------

def test_health_json_reflects_failsafe(panel, monkeypatch):
    main, client = panel
    _mock_system(main, monkeypatch, state="inactive", failsafe=True)
    r = client.get("/health.json")
    assert r.status_code == 200
    assert r.json() == {"failsafe": True, "profile": "p", "phase": "failsafe",
                        "has_topology": True}


# --- /status.json (the no-sudo agent surface) ------------------------------

def test_status_json_ok_when_healthy(panel, monkeypatch):
    main, client = panel
    _mock_system(main, monkeypatch, state="active", api_ok=True)
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
    _mock_system(main, monkeypatch, state="active", api_ok=False)
    assert client.get("/status.json").json()["ok"] is False


def test_status_json_not_ok_and_failsafe(panel, monkeypatch):
    main, client = panel
    _mock_system(main, monkeypatch, state="inactive", failsafe=True)
    j = client.get("/status.json").json()
    assert j["ok"] is False and j["failsafe"] is True
    assert j["engines"][0]["nodes"][0]["failsafe"] is True


def test_status_json_ok_for_the_empty_profile(panel, monkeypatch):
    main, client = panel
    _mock_system(main, monkeypatch, state="active", api_ok=True,
                 topo={"profile": "empty", "engines": []})
    j = client.get("/status.json").json()
    # all() over zero engines is true — an activated `empty` is healthy by intent.
    assert j["has_topology"] is True and j["ok"] is True and j["engines"] == []


def test_status_json_reports_the_request_alongside_what_came_up(panel, monkeypatch):
    """"Asked for" and "serving" are genuinely different — a failed activation falls
    to `empty` while the request still names what was wanted."""
    main, client = panel
    _mock_system(main, monkeypatch, state="active", api_ok=True)
    assert client.get("/status.json").json()["requested"] == "p"


# --- the command surface: two triggers, nothing else -----------------------

def test_build_cmd_is_only_the_reconciler(panel):
    main, _ = panel
    activate = main._build_cmd("activate", "step-3.5-fp8")
    assert activate.endswith("&& sudo -n /usr/local/sbin/vllm-activate")
    assert main._build_cmd("reactivate", "x") == "sudo -n /usr/local/sbin/vllm-activate --force"
    assert main._build_cmd("bogus", "x") is None
    # No path to root: ansible, systemctl and docker are simply not reachable here.
    for forbidden in ("ansible-playbook", "systemctl", "docker", "site.yml"):
        assert forbidden not in activate


def test_build_cmd_quotes_the_profile(panel):
    main, _ = panel
    assert "'weird; rm -rf'" in main._build_cmd("activate", "weird; rm -rf")


def test_the_panel_has_no_deploy_action(panel):
    """ADR-0018 removes panel-triggered infra deploys BY DESIGN — that's the cost
    paid for having no web-API path to root."""
    main, _ = panel
    assert set(main.ACTIONS) == {"activate", "reactivate"}


# --- available_profiles comes from the deploy-written allowlist -------------

def test_available_profiles_reads_the_allowlist(panel, monkeypatch):
    main, _ = panel
    Path(main.ALLOWLIST_FILE).write_text("# generated\n\nminimax\nstep\n")
    assert main.available_profiles() == ["empty", "minimax", "step"]


def test_available_profiles_without_an_allowlist_is_just_empty(panel):
    """Never offer something the reconciler would refuse."""
    main, _ = panel
    assert main.available_profiles() == ["empty"]


# --- run_action guards -----------------------------------------------------

def test_run_action_rejects_an_unknown_action(panel):
    main, client = panel
    assert client.post("/run/nope").status_code == 404


def test_run_action_falls_back_to_the_current_profile(panel, monkeypatch):
    main, client = panel
    Path(main.ALLOWLIST_FILE).write_text("p\n")
    monkeypatch.setattr(main, "load_topology", lambda: _topo())
    started = {}
    monkeypatch.setattr(main, "start_run",
                        lambda name, label, cmd: started.update(name=name, cmd=cmd) or
                        {"id": "1", "name": name, "label": label, "status": "running",
                         "code": None, "log": "", "started": "now"})
    r = client.post("/run/activate", data={"profile": "../../etc/passwd"})
    assert r.status_code == 200
    # Fell back to the live profile; the traversal attempt never reaches the command.
    assert "passwd" not in started["cmd"]
    assert "printf '%s\\n' p >" in started["cmd"]


# --- phase: telling "wait" from "something is wrong" (ADR-0018) -------------
#
# Activation became a panel action, so the panel is the surface you watch through a
# ten-to-twenty-minute weight load. Before this, that whole window rendered exactly
# like a broken engine — which is both wrong and a good way to learn to ignore red.

def _nodes(state="active", failsafe=False, n=2):
    return [{"node": f"n{i}", "state": state, "failsafe": failsafe} for i in range(n)]


def test_serving_when_the_api_answers(panel):
    main, _ = panel
    assert main.engine_phase(_nodes(), api_ok=True, elapsed=5) == "serving"


def test_loading_while_the_weights_load(panel):
    """The engine of this whole change: unit up, API down, and it has not been that
    way for long. A 122 GiB TP=2 model is minutes of exactly this."""
    main, _ = panel
    assert main.engine_phase(_nodes(), api_ok=False, elapsed=240, load_timeout=1200) == "loading"


def test_stalled_once_the_load_window_has_passed(panel):
    """A fault that was previously invisible — minute 2 and minute 60 of a hung
    bring-up looked identical."""
    main, _ = panel
    assert main.engine_phase(_nodes(), api_ok=False, elapsed=1500, load_timeout=1200) == "stalled"


def test_stalled_when_the_clock_is_unknown(panel):
    """No timestamp means we cannot claim it is merely loading — say so rather than
    reassure."""
    main, _ = panel
    assert main.engine_phase(_nodes(), api_ok=False, elapsed=None) == "stalled"


def test_down_beats_loading(panel):
    """A unit that is not running is not loading, however recently it was started."""
    main, _ = panel
    assert main.engine_phase(_nodes(state="inactive"), api_ok=False, elapsed=10) == "down"


def test_failsafe_beats_everything(panel):
    main, _ = panel
    assert main.engine_phase(_nodes(failsafe=True), api_ok=True, elapsed=5) == "failsafe"


def test_a_partially_started_tp2_engine_is_down(panel):
    """One rank up and one down is not a load in progress."""
    main, _ = panel
    nodes = [{"node": "sparky", "state": "active", "failsafe": False},
             {"node": "snoopy", "state": "inactive", "failsafe": False}]
    assert main.engine_phase(nodes, api_ok=False, elapsed=10) == "down"


def test_overall_phase_takes_the_worst(panel):
    main, _ = panel
    assert main.overall_phase(["serving", "loading"]) == "loading"
    assert main.overall_phase(["loading", "failsafe"]) == "failsafe"
    assert main.overall_phase(["serving", "serving"]) == "serving"
    assert main.overall_phase([]) == "idle"   # `empty` is activated, nothing to serve


def test_status_json_reports_loading_not_ok(panel, monkeypatch):
    """`ok` must stay "serving right now" so `sparky status` still gates correctly —
    an agent must not start benchmarking a model that is still loading. `phase` is
    what says the not-ok is expected."""
    main, client = panel
    monkeypatch.setattr(main, "load_topology", lambda: _topo())
    monkeypatch.setattr(main, "node_engine_states", lambda: {
        n: {"e": {"name": "e", "state": "active", "desired": True,
                  "failsafe": False, "pending": False, "active_for": 120.0}}
        for n in ("sparky", "snoopy")})
    monkeypatch.setattr(main, "_vllm_api", lambda base: (False, "down"))
    monkeypatch.setattr(main, "_http_ok", lambda url, timeout=4: "running")
    j = client.get("/status.json").json()
    assert j["phase"] == "loading"
    assert j["ok"] is False
    assert j["engines"][0]["phase"] == "loading"
    assert j["engines"][0]["elapsed"] == 120


def test_status_json_phase_serving_when_healthy(panel, monkeypatch):
    main, client = panel
    _mock_system(main, monkeypatch, state="active", api_ok=True)
    j = client.get("/status.json").json()
    assert j["phase"] == "serving" and j["ok"] is True


# --- switching: the panel describes the OUTGOING profile mid-activation -----

def test_switching_detected_when_request_and_live_differ(panel):
    main, _ = panel
    assert main.switching("step-3.5-fp8", "minimax-m2.7-awq") is True
    assert main.switching("step-3.5-fp8", "step-3.5-fp8") is False
    assert main.switching("", "step-3.5-fp8") is False


def test_outgoing_engines_read_as_switching_not_down(panel, monkeypatch):
    """current-topology.json is rewritten only when the switch LANDS, so mid-flight
    the panel is describing engines that are legitimately stopping. True, but useless
    — and indistinguishable from a broken cluster."""
    main, client = panel
    Path(main.DESIRED_PROFILE).write_text("step-3.5-fp8\n")   # asked for
    monkeypatch.setattr(main, "load_topology", lambda: _topo())  # profile 'p' still live
    monkeypatch.setattr(main, "node_engine_states", lambda: _states("inactive"))
    monkeypatch.setattr(main, "_vllm_api", lambda base: (False, "down"))
    monkeypatch.setattr(main, "_http_ok", lambda url, timeout=4: "running")
    j = client.get("/status.json").json()
    assert j["switching"] is True
    assert j["phase"] == "switching"
    assert j["engines"][0]["phase"] == "switching"


def test_a_real_failsafe_is_never_masked_by_a_switch(panel, monkeypatch):
    """The one state that must survive every other consideration."""
    main, client = panel
    Path(main.DESIRED_PROFILE).write_text("step-3.5-fp8\n")
    monkeypatch.setattr(main, "load_topology", lambda: _topo())
    monkeypatch.setattr(main, "node_engine_states", lambda: _states("inactive", failsafe=True))
    monkeypatch.setattr(main, "_vllm_api", lambda base: (False, "down"))
    monkeypatch.setattr(main, "_http_ok", lambda url, timeout=4: "running")
    j = client.get("/status.json").json()
    assert j["failsafe"] is True
    assert j["phase"] == "failsafe"
