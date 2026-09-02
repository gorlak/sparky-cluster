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
import json
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
    monkeypatch.setenv("SUITE_BIN", "/usr/local/sbin/vllm-suite")
    monkeypatch.setenv("SUITE_DIR", str(tmp_path / "suites"))
    monkeypatch.setenv("SUITE_LOG_DIR", str(tmp_path / "suite-logs"))
    (tmp_path / "suites").mkdir()
    (tmp_path / "suites" / "nightly.yml").write_text(
        'description: Nightly suite of the fleet.\nestimate: "~7 h"\n'
        "jobs: [{profile: a}, {profile: b}]\n")
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
    activate = main._build_cmd("activate", "step-3.5-flash-fp8")
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
    assert main.switching("step-3.5-flash-fp8", "minimax-m2.7-awq") is True
    assert main.switching("step-3.5-flash-fp8", "step-3.5-flash-fp8") is False
    assert main.switching("", "step-3.5-flash-fp8") is False


def test_outgoing_engines_read_as_switching_not_down(panel, monkeypatch):
    """current-topology.json is rewritten only when the switch LANDS, so mid-flight
    the panel is describing engines that are legitimately stopping. True, but useless
    — and indistinguishable from a broken cluster."""
    main, client = panel
    Path(main.DESIRED_PROFILE).write_text("step-3.5-flash-fp8\n")   # asked for
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
    Path(main.DESIRED_PROFILE).write_text("step-3.5-flash-fp8\n")
    monkeypatch.setattr(main, "load_topology", lambda: _topo())
    monkeypatch.setattr(main, "node_engine_states", lambda: _states("inactive", failsafe=True))
    monkeypatch.setattr(main, "_vllm_api", lambda base: (False, "down"))
    monkeypatch.setattr(main, "_http_ok", lambda url, timeout=4: "running")
    j = client.get("/status.json").json()
    assert j["failsafe"] is True
    assert j["phase"] == "failsafe"


# --- exporters: data presence, not liveness (2026-08-09) --------------------

def test_an_exporter_answering_with_no_data_is_not_healthy(panel, monkeypatch):
    """The failure this exists for: on 2026-08-09 the GPU exporter returned HTTP 200 for
    ~15 hours with a body containing only `nvidia_smi_failed_scrapes_total`. Its
    Prometheus target stayed `up`, every GPU dashboard was empty, and nothing noticed —
    a `systemctl daemon-reload` during a deploy had broken NVML inside the container.
    Liveness checks cannot see this; only a content check can."""
    main, _ = panel

    class _Resp:
        status_code = 200
        text = ("# HELP nvidia_smi_command_exit_code\n"
                "nvidia_smi_command_exit_code 255\n"
                "nvidia_smi_failed_scrapes_total 3503\n")

    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: _Resp())
    assert main._exporter_ok("http://x/metrics") == "nvidia-smi exit 255"


def test_a_healthy_exporter_reports_running(panel, monkeypatch):
    main, _ = panel

    class _Resp:
        status_code = 200
        text = "nvidia_smi_command_exit_code 0\n" + "".join(
            f"metric_{i} 1\n" for i in range(20))

    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: _Resp())
    assert main._exporter_ok("http://x/metrics") == "running"


def test_an_exporter_emitting_almost_nothing_is_flagged(panel, monkeypatch):
    """The general case: an exporter that lost its data source without advertising a
    failure counter of its own."""
    main, _ = panel

    class _Resp:
        status_code = 200
        text = "# HELP something\nonly_one_series 1\n"

    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: _Resp())
    assert "only 1 series" in main._exporter_ok("http://x/metrics")


def test_node_exporter_503_is_reported(panel, monkeypatch):
    """node-exporter's other 2026-08-09 failure: a leaked in-flight counter made every
    scrape 503 while each collector individually answered in under 40ms."""
    main, _ = panel

    class _Resp:
        status_code = 503
        text = "Limit of concurrent requests reached (40), try again later.\n"

    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: _Resp())
    assert main._exporter_ok("http://x/metrics") == "HTTP 503"


def test_metrics_checks_the_panels_own_node_too(panel, monkeypatch):
    """NODE_ADDRS lists remote nodes only, so a naive loop checks the far side and skips
    the local one — which on 2026-08-09 was the node whose exporters were broken."""
    main, _ = panel
    monkeypatch.setattr(main, "_exporter_ok", lambda url, **k: url)
    labels = [name for name, _ in main.metrics_services()]
    assert any(main.PANEL_NODE in label for label in labels), labels
    assert any("snoopy" in label for label in labels), labels


# --- the scoreboard page (2026-08-10) ---------------------------------------

def test_scoreboard_renders_the_snapshot(panel, tmp_path, monkeypatch):
    """The panel READS the analysis; it never recomputes it. Dominance and best-marking
    live in `sparky scoreboard` alone — two implementations of that logic would drift,
    and a scoreboard that disagrees with itself is worse than none."""
    main, client = panel
    snapshot = tmp_path / "scoreboard.json"
    snapshot.write_text(json.dumps({
        "generated": "2026-08-10T00:00:00Z",
        "columns": ["accuracy", "out tok/s"],
        "rows": [
            {"label": "good", "profile": "p", "nodes": 2, "unreliable": False,
             "legacy_perf": False, "missing": [],
             "cells": [{"text": "75.0%", "best": True, "value": 0.75},
                       {"text": "110", "best": False, "value": 110.0}]},
            {"label": "beaten", "profile": "q", "nodes": 2, "unreliable": True,
             "legacy_perf": False, "missing": [],
             "cells": [{"text": "48.0%", "best": False, "value": 0.48},
                       {"text": "100", "best": False, "value": 100.0}]},
        ],
        "scatter": {"points": [{"label": "good", "x": 110.0, "y": 0.75, "dominated": False},
                               {"label": "beaten", "x": 100.0, "y": 0.48, "dominated": True}],
                    "x_label": "output tok/s", "y_label": "accuracy"},
        "dominated": ["beaten"],
    }))
    monkeypatch.setattr(main, "SCOREBOARD_FILE", str(snapshot))
    body = client.get("/scoreboard").text
    assert "75.0%" in body and "beaten" in body
    assert "†" in body, "an unreliable accuracy must stay flagged in the UI"
    # dominance now shows only as the faded/ringed dot — the word is not stamped on every
    # legend row nor repeated as a whole-list note (that read as clutter). Check the visible
    # markers are gone; the invisible JSON data blob still carries the field, which is fine.
    assert ">dominated<" not in body, "per-row dominated tag should be gone from the legend"
    assert "Dominated:" not in body, "the whole-list dominated note should be gone"


def test_scatter_uses_family_marks_colours_and_keeps_dots_in_frame(panel):
    """Model names printed ON the plot ran off the 640-wide right edge and overlapped each
    other. The chart now carries a short per-family mark (q1, n2, mm1) coloured by family, with
    the names in the legend. Marks number within a family in accuracy order, one family is one
    colour, every dot stays in the 640x340 frame, and two models on one coordinate are nudged
    apart rather than one hiding under the other."""
    main, _client = panel
    pts = [{"label": "qwen3.6-35b-a3b-nvfp4", "x": 96.0, "y": 0.807, "dominated": False},
           {"label": "qwen3.8-flash-next-nvfp4", "x": 14.0, "y": 0.757, "dominated": True},
           {"label": "nvidia-nemotron-3-super-120b-a12b-nvfp4", "x": 25.0, "y": 0.686,
            "dominated": True},
           {"label": "minimax-m2.7-nvfp4", "x": 25.0, "y": 0.686, "dominated": True},
           {"label": "mistral-small-4-119b-2603-nvfp4", "x": 45.0, "y": 0.543, "dominated": True}]
    placed = main._project_scatter(pts)["points"]
    mark = {p["label"]: p["mark"] for p in placed}
    assert mark["qwen3.6-35b-a3b-nvfp4"] == "q1"        # highest-accuracy qwen numbers first
    assert mark["qwen3.8-flash-next-nvfp4"] == "q2"
    assert mark["nvidia-nemotron-3-super-120b-a12b-nvfp4"] == "n1"   # `nvidia-` covers nemotron
    assert mark["minimax-m2.7-nvfp4"] == "mm1"
    assert mark["mistral-small-4-119b-2603-nvfp4"] == "ms1"
    colour = {p["mark"]: p["colour"] for p in placed}
    assert colour["q1"] == colour["q2"], "one family must be one colour"
    assert colour["q1"] != colour["n1"], "different families must differ in colour"
    assert all(60 <= p["cx"] <= 610 and 40 <= p["cy"] <= 290 for p in placed), "dot off-frame"
    coords = [(p["cx"], p["cy"]) for p in placed]
    assert len(set(coords)) == len(coords), "minimax and nemotron-super were not nudged apart"


def test_legend_is_grouped_by_family_not_raw_accuracy(panel):
    """Numbering is accuracy-ranked (q1 is the best qwen), but the legend GROUPS by family so a
    colour block reads together and marks run q1,q2,g1,n1,n2 — not interleaved by accuracy.
    Families lead with their strongest member (qwen first, holding the top model overall)."""
    main, _client = panel
    pts = [{"label": "qwen3.6-35b-a3b-nvfp4", "x": 96.0, "y": 0.807, "dominated": False},
           {"label": "glm-4.7-flash", "x": 37.0, "y": 0.686, "dominated": True},
           {"label": "qwen3-coder-next-nvfp4", "x": 54.0, "y": 0.729, "dominated": True},
           {"label": "nvidia-nemotron-3-super-120b-a12b-nvfp4", "x": 25.0, "y": 0.680,
            "dominated": True},
           {"label": "nvidia-nemotron-labs-3-puzzle-75b-a9b-nvfp4", "x": 32.0, "y": 0.671,
            "dominated": True}]
    order = [p["mark"] for p in main._project_scatter(pts)["points"]]
    assert order == ["q1", "q2", "g1", "n1", "n2"]


def test_scatter_axes_are_zero_based_with_round_graduations(panel):
    """The plot reads absolute magnitude, so the axes start at a true (0,0), not the data's
    min, and graduations fall on round numbers above the data (96 tok/s -> a 100 top, 81% ->
    100%). The dots map through those same tops, so a dot lands on the grid."""
    main, _client = panel
    pts = [{"label": "qwen3.6-35b-a3b-nvfp4", "x": 96.0, "y": 0.807, "dominated": False},
           {"label": "minimax-m2.7-nvfp4", "x": 25.0, "y": 0.543, "dominated": True}]
    svg = main._project_scatter(pts)
    # zero-based: a 0 graduation on each axis, sitting exactly at the origin corner
    x0 = next(t for t in svg["x_ticks"] if t["label"] == "0")
    y0 = next(t for t in svg["y_ticks"] if t["label"] == "0%")
    assert x0["px"] == 60 and y0["py"] == 290
    # round tops above the data
    assert svg["x_ticks"][-1]["label"] == "100"
    assert svg["y_ticks"][-1]["label"] == "100%"
    # graduations are evenly spaced (a real scale, not just endpoints)
    assert [t["label"] for t in svg["x_ticks"]] == ["0", "25", "50", "75", "100"]
    # the fastest model sits ~96/100 of the way along, on the grid — not pinned to the edge
    fast = next(p for p in svg["points"] if p["mark"] == "q1")
    assert 550 < fast["cx"] < 565


def test_scoreboard_says_so_when_there_is_no_snapshot(panel, monkeypatch):
    """A missing snapshot is a normal state (no suite yet) — say what to run, do not 500."""
    main, client = panel
    monkeypatch.setattr(main, "SCOREBOARD_FILE", "/nonexistent/scoreboard.json")
    response = client.get("/scoreboard")
    assert response.status_code == 404
    assert "sparky scoreboard" in response.text


def test_scoreboard_links_model_names_to_the_hub():
    """The model name is the link, so a row and its Hub page are visibly the same thing.

    The URL must come from `hf_repo` and never be constructed from the label: the profile
    name is the model name lowercased, but the ORG is not recoverable from it —
    `Qwen3-Coder-Next-NVFP4` is RedHatAI's, not Qwen's, and a guessed
    `huggingface.co/qwen/...` would 404 while looking authoritative.
    """
    import json
    from jinja2 import Environment, FileSystemLoader
    tpl_dir = (Path(__file__).resolve().parent.parent / "ansible" / "roles" /
               "control-panel" / "files" / "app" / "templates")
    data = {
        "columns": ["accuracy"],
        "column_meta": [{"name": "accuracy", "higher_is_better": True}],
        "generated": "now",
        "rows": [
            {"label": "qwen3-coder-next-nvfp4", "profile": "p", "nodes": 2,
             "hf_repo": "RedHatAI/Qwen3-Coder-Next-NVFP4", "retired": False,
             "unreliable": False, "legacy_perf": False, "missing": [],
             "cells": [{"text": "60.0%", "best": True, "value": 0.6}]},
            {"label": "no-repo-recorded", "profile": "q", "nodes": 2, "hf_repo": None,
             "retired": True, "unreliable": False, "legacy_perf": False, "missing": [],
             "cells": [{"text": "—", "best": False, "value": None}]},
        ],
        "scatter": {"points": [], "x_label": "x", "y_label": "y"},
        "plot_points": [], "dominated": [],
    }
    html = Environment(loader=FileSystemLoader(str(tpl_dir))).get_template(
        "scoreboard.html").render(data=data)
    assert 'href="https://huggingface.co/RedHatAI/Qwen3-Coder-Next-NVFP4"' in html
    # a row without a recorded repo must stay plain text, not link somewhere invented
    assert "huggingface.co/no-repo-recorded" not in html
    assert "no-repo-recorded" in html
    assert ">retired<" in html          # and it is marked as no longer activatable


# --- suites (ADR-0021) ----------------------------------------------------
#
# The panel starts a suite as a unit of its OWN rather than as a child. That is the
# whole point of the feature — every deploy restarts this service, and a child dies with
# the cgroup — so the tests are about the panel staying thin: it names a suite, and the
# trigger decides everything else.

def test_the_suite_list_comes_from_the_installed_directory(panel):
    """The buttons must offer exactly what the trigger would accept. Scanning a repo, or
    keeping a hand-written list, is how a button appears for something that then refuses
    to start."""
    main, _ = panel
    assert [r["name"] for r in main.installed_suites()] == ["nightly"]


def test_starting_a_suite_goes_through_the_trigger_unexamined(panel, monkeypatch):
    """The panel does NOT pre-validate the name. The trigger checks it against the
    installed allowlist and rejects anything that is not a bare identifier, and it is the
    same check `sparky run` gets — a second copy here would be a second thing to keep
    right."""
    main, client = panel
    calls = []

    class _Ok:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _Ok()

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    response = client.post("/suite/nightly")
    assert response.status_code == 200
    assert calls[0] == ["sudo", "-n", "/usr/local/sbin/vllm-suite", "start", "nightly"]


def test_the_triggers_refusal_is_surfaced_not_swallowed(panel, monkeypatch):
    """Its refusals say which of two things went wrong — a typo, or a missing deploy —
    and that is the useful part. Collapsing them into a generic 500 would throw away the
    only thing that tells you what to do next."""
    main, client = panel

    class _No:
        returncode, stdout, stderr = 2, "", "'x' is not an installed suite."

    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: _No())
    response = client.post("/suite/x")
    assert response.status_code == 400
    assert "not an installed suite" in response.text


def test_stop_takes_no_name(panel, monkeypatch):
    """One run at a time, fleet-wide — the unit is fixed, so there is nothing to name."""
    main, client = panel
    calls = []

    class _Ok:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(main.subprocess, "run",
                        lambda cmd, **kw: (calls.append(cmd), _Ok())[1])
    assert client.post("/suite-stop").status_code == 200
    assert calls[0] == ["sudo", "-n", "/usr/local/sbin/vllm-suite", "stop"]


def test_run_state_is_read_from_systemd_not_from_a_pid_file(panel, monkeypatch):
    """systemd already knows whether the unit is alive and how it exited; a pid file would
    be a second record that can be stale. The trigger deliberately does not `--collect`
    the unit, which is what leaves the exit status readable after the run finishes."""
    main, _ = panel

    class _Show:
        returncode = 0
        stdout = ("ActiveState=inactive\nDescription=sparky suite: nightly\n"
                  "ExecMainStatus=1\n")
        stderr = ""

    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: _Show())
    state = main.suite_state()
    assert state["name"] == "nightly"
    assert state["status"] == "failed" and state["code"] == 1


def test_a_missing_systemd_is_a_status_not_a_crash(panel, monkeypatch):
    """The panel is what you look at when things are broken. It must render then."""
    main, _ = panel

    def boom(*a, **kw):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(main.subprocess, "run", boom)
    assert main.suite_state()["status"] == "none"


# --- /metrics: what is serving, and what is measuring it ---------------------

def test_metrics_names_the_profile_not_the_model(panel, monkeypatch):
    """The profile is the identity everything keys on — the basename of its
    `ansible/profiles/<name>.yml`, what `activate` takes, what the trend store labels a
    measurement with. vLLM's own metrics cannot supply it: their `model_name` label is the
    stable alias every engine advertises so chat survives an activation, so it reads
    `sparky` no matter what is loaded."""
    main, client = panel
    monkeypatch.setattr(main, "load_topology", lambda: {
        "profile": "qwen3.6-35b-a3b-nvfp4",
        "engines": [{"name": "qwen3.6-35b-a3b-nvfp4", "model": "Qwen3.6-35B-A3B-NVFP4"}]})
    monkeypatch.setattr(main, "suite_state", lambda: {"name": "", "status": "none"})
    body = client.get("/metrics").text
    assert 'sparky_active_profile{profile="qwen3.6-35b-a3b-nvfp4"} 1' in body
    assert 'model="Qwen3.6-35B-A3B-NVFP4"' in body


def test_metrics_always_emits_exactly_one_profile_series(panel, monkeypatch):
    """A stat panel showing a label needs a single series, and "No data" is the wrong
    answer to "nothing is serving, on purpose" — that is the moment it is ambiguous."""
    main, client = panel
    monkeypatch.setattr(main, "suite_state", lambda: {"name": "", "status": "none"})
    for topo in (None,
                 {"profile": "empty", "engines": []},
                 {"profile": "p", "engines": [{"name": "a", "model": "A"},
                                              {"name": "b", "model": "B"}]}):
        monkeypatch.setattr(main, "load_topology", lambda t=topo: t)
        body = client.get("/metrics").text
        series = [ln for ln in body.splitlines()
                  if ln.startswith("sparky_active_profile{")]
        assert len(series) == 1, topo


def test_metrics_reports_no_suite_once_one_has_finished(panel, monkeypatch):
    """The unit keeps its description after it exits — which is what leaves the last run's
    exit status readable — so carrying that name into the metric would leave the dashboard
    reading `nemotron-family` for the twelve hours after nemotron-family finished."""
    main, client = panel
    monkeypatch.setattr(main, "load_topology", lambda: None)
    monkeypatch.setattr(main, "suite_state",
                        lambda: {"name": "nemotron-family", "status": "success"})
    assert 'sparky_suite_running{suite="none"} 0' in client.get("/metrics").text

    monkeypatch.setattr(main, "suite_state",
                        lambda: {"name": "nemotron-family", "status": "running"})
    assert ('sparky_suite_running{suite="nemotron-family"} 1'
            in client.get("/metrics").text)


def test_metrics_does_not_probe_the_nodes(panel, monkeypatch):
    """Scraped every 15s. `gather()` reaches every node over SSH — right for a page you
    open, wrong for something Prometheus polls, and it would make the metric endpoint the
    slowest thing on the box precisely when a node is wedged."""
    main, client = panel

    def boom():
        raise AssertionError("/metrics must not call gather()")

    monkeypatch.setattr(main, "gather", boom)
    monkeypatch.setattr(main, "suite_state", lambda: {"name": "", "status": "none"})
    assert client.get("/metrics").status_code == 200


def test_the_dashboard_queries_metrics_the_panel_actually_exports(panel, monkeypatch):
    """Two files, one contract. A renamed metric would leave the dashboard's top row
    reading "No data" — and a dashboard that is blank in the corner nobody committed to
    looking at is how you learn about it three weeks later."""
    main, client = panel
    monkeypatch.setattr(main, "load_topology", lambda: None)
    monkeypatch.setattr(main, "suite_state", lambda: {"name": "", "status": "none"})
    exported = {ln.split("{")[0].split(" ")[0]
                for ln in client.get("/metrics").text.splitlines()
                if ln and not ln.startswith("#")}

    dashboard = json.loads((Path(__file__).resolve().parent.parent / "ansible" / "roles" /
                            "grafana" / "files" / "cluster.json").read_text())
    top = [p for p in dashboard["panels"] if p["gridPos"]["y"] == 0]
    assert {p["title"] for p in top} == {"Serving", "Suite"}
    for p in top:
        expr = p["targets"][0]["expr"]
        assert any(metric in expr for metric in exported), expr
        # The value carries nothing here; the LABEL is the answer, which is what
        # textMode=name renders.
        assert p["options"]["textMode"] == "name"
        assert "{{" in p["targets"][0]["legendFormat"]


def test_a_suite_button_carries_what_it_does_and_what_it_costs(panel, monkeypatch):
    """A bare name is a poor label for a button that commandeers the cluster for an
    evening, and whoever presses it is not reading the YAML — which is why `description`
    and `estimate` are required fields (`sparky lint`) rather than comments.

    Unit state is mocked: without it this reads the HOST's systemd, so the test passed or
    failed depending on whether a real suite happened to be running on the machine —
    which is exactly how it failed the first time a suite was started for real.
    """
    main, client = panel
    monkeypatch.setattr(main, "_unit_fields", lambda *p: {"ActiveState": "inactive"})
    rb = main.installed_suites()[0]
    assert rb["description"] == "Nightly suite of the fleet."
    assert rb["estimate"] == "~7 h"
    assert rb["jobs"] == 2

    page = client.get("/suites").text
    assert "Nightly suite of the fleet." in page
    assert "~7 h" in page and "2 profiles" in page


def test_a_suite_missing_its_metadata_still_lists(panel, tmp_path):
    """The panel's job is to show what is installed, not to audit it. A file that lint
    would reject must not blank the whole section — that would hide every OTHER suite
    because of one bad one."""
    main, _ = panel
    (tmp_path / "suites" / "bare.yml").write_text("jobs: [{profile: a}]\n")
    (tmp_path / "suites" / "broken.yml").write_text("{[not yaml\n")
    names = [r["name"] for r in main.installed_suites()]
    assert names == ["bare", "broken", "nightly"]
    assert all(r["description"] == "" for r in main.installed_suites()
               if r["name"] in ("bare", "broken"))


def test_the_panel_and_the_cli_show_the_same_menu_in_the_same_order(panel, tmp_path):
    """Two renderings of one list. If they sorted differently, "the third one down" would
    mean different suites depending on where you were standing."""
    from sparky.measure.loop import suite

    main, _ = panel
    (tmp_path / "suites" / "later.yml").write_text(
        'description: d\nestimate: "~1 h"\norder: 90\njobs: [{profile: a}]\n')
    (tmp_path / "suites" / "earlier.yml").write_text(
        'description: d\nestimate: "~1 h"\norder: 10\njobs: [{profile: a}]\n')
    panel_order = [r["name"] for r in main.installed_suites()]
    cli_order = [r["name"] for r in suite.describe(tmp_path / "suites")]
    assert panel_order == cli_order == ["earlier", "nightly", "later"]


def test_the_dashboard_stacks_trends_above_host_noise():
    """Panel ORDER is the dashboard's argument about what matters (2026-08-12).

    Serving trends first — token throughput, then memory — then the two utilization
    rows, then temperature. Titles carry no `Node` prefix (redundant: everything here is
    a node) and units appear in the TITLE only, never repeated in a series name.

    **`SoC thermal zones` was added and removed the same day (2026-08-12).** GB10's SPBM
    firmware carries 8 named sensors (`tj_max`, `cpu_p_clu0/1`, `cpu_e_clu0/1`, `gpu`,
    `soc`, `dla`) but ACPI re-exports 7 of them anonymously. Only `z0` could be pinned —
    it equals max(others) in 99-100% of samples, so it IS `tj_max`. The other six sit
    within a few °C and reorder between nodes and windows: a "z2 is a P-core" reading
    from one window was contradicted by the next, and z5 was simultaneously hot on sparky
    and cool on snoopy. Seven anonymous lines is noise, so only Tj max is plotted.

    `Requests running / waiting` was removed rather than moved: at a fleet-wide
    concurrency of one serving engine it read 1 or 0 and answered nothing the `Serving`
    panel does not.

    **`TTFT p99` was removed too (2026-08-12), and the reason generalises.** It was
    reported as spotty; the cause was that `histogram_quantile` over an all-zero rate
    returns NaN, not 0, so an idle cluster drew holes. Making it plot a truthful 0 while
    the engine is up is a two-line fix — and it proved the panel had nothing to say. This
    cluster serves sporadic single-user traffic, so a p99 here is a percentile over about
    three requests, which is just the maximum wearing a statistic's name.

    The boundary it settles: **the dashboard shows what the cluster is DOING; the
    scoreboard shows how WELL a model does it.** A quality-of-service percentile only
    means something under controlled load, which is `sparky bench`'s job — and
    `scoreboard.COLUMNS` already carries `TTFT p99` from `ttft_p99_ms`, so nothing was
    lost by deleting the panel. Token throughput stays because it is a liveness signal
    that pairs with GPU utilization, not a model comparison.
    """
    dashboard = json.loads((Path(__file__).resolve().parent.parent / "ansible" / "roles" /
                            "grafana" / "files" / "cluster.json").read_text())
    titles = {p["title"] for p in dashboard["panels"]}
    assert "Requests running / waiting" not in titles

    rows = sorted(dashboard["panels"], key=lambda p: p["gridPos"]["y"])

    ts = [p for p in rows if p["type"] == "timeseries"]
    assert [p["title"] for p in ts] == [
        "Token throughput", "Memory used (%)", "GPU utilization (%)",
        "CPU utilization (%)", "Temp (°C) / power (W)"], "panel order is the argument"

    # No `Node` prefix: every panel here is a node, so it carried no information.
    assert not any(p["title"].startswith("Node") for p in dashboard["panels"])

    # Units live in the title, never also in a series name — one place, not two.
    for p in dashboard["panels"]:
        for t in p.get("targets", []):
            legend = t.get("legendFormat", "")
            assert "°C" not in legend and not legend.endswith(" W"), \
                f"{p['title']}: unit repeated in series name {legend!r}"

    # Only Tj max (zone 0) is plotted — the other six ACPI zones are unidentifiable.
    zone_exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])
                  if "thermal_zone" in t.get("expr", "")]
    assert zone_exprs == ['node_thermal_zone_temp{zone="0"}'], \
        "only the firmware-computed package max is trustworthy; see the docstring"

    # Both serving metrics are PLOTS ONLY (2026-08-12). The big-number stats were removed:
    # a single instantaneous value for a rate is the least informative thing on the page —
    # 40 tok/s tells you nothing without knowing whether it is climbing, flat or collapsing,
    # and the plot directly above it answers that. The only stats left are `Serving` and
    # `Suite`, which are STATES rather than magnitudes and have no trend to show.
    assert {p["title"] for p in dashboard["panels"] if p["type"] == "stat"} == {"Serving", "Suite"}
    # TTFT belongs to the scoreboard, not here — see the docstring. Assert it is gone from
    # the dashboard AND still owned by the scoreboard, so this can never become a silent
    # deletion of the metric rather than a relocation of it.
    assert "TTFT p99 (s)" not in titles, "TTFT is a scoreboard metric, not a dashboard one"
    assert not any("time_to_first_token" in t.get("expr", "")
                   for p in dashboard["panels"] for t in p.get("targets", [])), \
        "no panel should query the TTFT histogram"
    from sparky.measure.record import scoreboard
    assert any(c[0] == "TTFT p99" for c in scoreboard.COLUMNS), \
        "removing the panel is only correct while the scoreboard still reports TTFT p99"

    # ONE time axis for the whole page. `graphTooltip: 2` is shared crosshair AND tooltip,
    # so hovering any graph puts the cursor on every other and shows their values at that
    # instant — which is the only way to answer "what was the GPU doing when TTFT spiked".
    # It was 1 (crosshair, no values), which draws the line but tells you nothing.
    assert dashboard["graphTooltip"] == 2, "shared crosshair + tooltip across the stack"

    # Stats are NUMBERS, not sparklines. Grafana's stat default is `graphMode: "area"`, so
    # leaving it unset renders a mini-graph with its own implicit horizontal scale that
    # does not join the shared cursor — two time axes on one page, silently. Trends belong
    # in the stack where they are comparable; the stat is for the glance.
    for p_ in dashboard["panels"]:
        if p_["type"] == "stat":
            assert p_["options"]["graphMode"] == "none", f"{p_['title']!r} draws a sparkline"

    # No gaps or overlaps in the vertical stack — a hand-edited gridPos is easy to get
    # wrong and Grafana silently reflows it into something nobody designed.
    full = [p for p in rows if p["gridPos"]["w"] == 24]
    for a, b in zip(full, full[1:]):
        assert b["gridPos"]["y"] == a["gridPos"]["y"] + a["gridPos"]["h"], \
            f"{a['title']!r} -> {b['title']!r} leaves a gap or overlaps"


def test_all_three_pages_carry_the_wordmark():
    """Landing, control and scoreboard are one product (2026-08-13).

    They already share `app.css` for exactly this reason — before 2026-08-12 the landing
    page carried its own stale copy of the panel's old values and had drifted apart. The
    wordmark follows the same rule: one asset, one CSS class, referenced by all three.
    """
    root = Path(__file__).resolve().parent.parent
    pages = {
        "landing": root / "ansible/roles/caddy/templates/index.html.j2",
        "control": root / "ansible/roles/control-panel/files/app/templates/index.html",
        "scoreboard": root / "ansible/roles/control-panel/files/app/templates/scoreboard.html",
    }
    for name, path in pages.items():
        html = path.read_text()
        assert 'class="wordmark"' in html, f"{name} lost the wordmark"
        assert html.index("wordmark") < html.index("<h1"), f"{name}: wordmark belongs above the h1"

    css = (root / "ansible/roles/control-panel/files/app/static/app.css").read_text()
    assert ".wordmark" in css, "the sizing rule must stay in the SHARED stylesheet"


def test_the_panel_logos_go_to_the_site_root_not_the_admin_area():
    """The wordmark is a HOME affordance: it takes you to the `/` landing page, not back into
    the admin area. `{{ root }}/` resolves to `/admin/`, so the logo must hardcode `/`."""
    root = Path(__file__).resolve().parent.parent
    tpl = root / "ansible/roles/control-panel/files/app/templates"
    for name in ("index.html", "scoreboard.html"):
        html = (tpl / name).read_text()
        assert '<a href="/" title="home"><img class="wordmark"' in html, \
            f"{name}: the logo should link to the site root /"
        assert '{{ root }}/"><img class="wordmark"' not in html, \
            f"{name}: the logo still points into the admin area"


def test_there_is_exactly_one_wordmark_and_the_readme_uses_it():
    """One asset, one home (2026-08-13).

    It has to live under `ansible/` because that is what gets published to /opt/cluster and
    served by both the panel and the landing page; `docs/` is never rsynced. So the README
    reaches across to it rather than keeping its own copy — the same call the caddy role
    already makes for `app.css`, whose comment says why: "the alternative is two copies that
    drift, which is exactly what this replaced."
    """
    root = Path(__file__).resolve().parent.parent
    canonical = root / "ansible/roles/control-panel/files/app/static/sparky.png"
    assert canonical.exists(), "the served wordmark is missing"

    strays = [p for p in root.rglob("sparky.png")
              if p != canonical and ".git" not in p.parts and "/opt/" not in str(p)]
    assert not strays, f"a second copy of the wordmark appeared: {strays}"

    readme = (root / "README.md").read_text()
    assert "ansible/roles/control-panel/files/app/static/sparky.png" in readme, \
        "the README must reference the canonical asset, not a copy of its own"
