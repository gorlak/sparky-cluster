"""The dead-man's switch (scale to zero) — ADR-0018-safe unloading.

`vllm-idle` is installed as a standalone program (not part of the `sparky` package), so it
is loaded here the way the reconciler tests load `vllm-activate`: by path, as a module.

**What is worth testing is when it REFUSES.** Unloading is cheap to get right and
expensive to get wrong: pulling a model out from under a campaign wastes hours of
measurement, and unloading on a transient network blip evicts a model nobody asked to
lose. The happy path is one `if`.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

IDLE = Path(__file__).resolve().parent.parent / "ansible/roles/activate/files/vllm-idle"


@pytest.fixture()
def prog(tmp_path, monkeypatch):
    """Load vllm-idle with every path pointed at a tmp dir."""
    monkeypatch.setenv("IDLE_UNLOAD_ENABLED", "true")
    monkeypatch.setenv("IDLE_AFTER_SECONDS", "3600")
    spec = importlib.util.spec_from_loader(
        "vllm_idle", importlib.machinery.SourceFileLoader("vllm_idle", str(IDLE)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.CLUSTER = tmp_path
    mod.TOPOLOGY = tmp_path / "current-topology.json"
    mod.DESIRED = tmp_path / "desired-profile"
    mod.FLEET_LOCK = tmp_path / "fleet.lock"
    mod.STATE = tmp_path / "idle-state.json"
    # Derived at IMPORT from the real /opt/cluster, so reassigning CLUSTER above is too
    # late — every path must be overridden explicitly. On 2026-08-13 a test wrote
    # "qwen3.6" into the LIVE /opt/cluster/last-serving-profile, the running idle manager
    # read it, and tried to activate a profile that does not exist. The allowlist check
    # caught it, but a test must never be able to reach the cluster at all.
    mod.LAST_PROFILE = tmp_path / "last-serving-profile"
    mod.CADDY_METRICS = "http://127.0.0.1:1/metrics"    # unreachable: no accidental probes
    mod.CADDY_CONFIG = "http://127.0.0.1:1/config"
    mod.IDLE_AFTER = 3600.0
    mod.ENABLED = True
    mod.WAKE_ENABLED = False
    mod.DESIRED.write_text("qwen3.6\n")
    return mod


def _topo(mod, profile="qwen3.6", engines=True):
    mod.TOPOLOGY.write_text(json.dumps({
        "profile": profile,
        "engines": [{"name": "e", "api_url": "http://127.0.0.1:1"}] if engines else [],
    }))


def _never_unloads(mod, monkeypatch):
    """Fail loudly if the switch reaches for the reconciler."""
    monkeypatch.setattr(mod, "unload",
                        lambda: pytest.fail("unloaded when it must not have"))


def test_disabled_by_default_does_nothing(prog, monkeypatch):
    """A deploy must never start unloading a fleet by surprise; it is opt-in."""
    prog.ENABLED = False
    _topo(prog)
    _never_unloads(prog, monkeypatch)
    assert prog.main() == 0


def test_refuses_while_a_deploy_or_campaign_holds_the_fleet_lock(prog, monkeypatch):
    """A campaign is MEASURING the model this would pull out from under it, and a deploy
    is reshaping the boundary. Same lock and same reasoning as docs/synchronization.md."""
    _topo(prog)
    _never_unloads(prog, monkeypatch)
    monkeypatch.setattr(prog, "activity", lambda: (0, 0))
    fd = os.open(prog.FLEET_LOCK, os.O_RDWR | os.O_CREAT, 0o664)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert prog.main() == 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_unreachable_engines_are_not_idle(prog, monkeypatch):
    """The dangerous confusion. A model we cannot SEE is not a model nobody is USING —
    treating a network blip as silence would evict a model in active use."""
    _topo(prog)
    _never_unloads(prog, monkeypatch)
    monkeypatch.setattr(prog, "activity", lambda: None)
    assert prog.main() == 0


def test_requests_in_flight_reset_the_clock(prog, monkeypatch):
    _topo(prog)
    _never_unloads(prog, monkeypatch)
    prog.write_state({"profile": "qwen3.6", "tokens": 10, "since": time.time() - 99999})
    monkeypatch.setattr(prog, "activity", lambda: (10, 1))     # one request running
    assert prog.main() == 0
    assert prog.read_state()["since"] > time.time() - 5, "clock should have restarted"


def test_token_counter_moving_resets_the_clock(prog, monkeypatch):
    """Traffic happened between checks even though nothing is in flight right now."""
    _topo(prog)
    _never_unloads(prog, monkeypatch)
    prog.write_state({"profile": "qwen3.6", "tokens": 10, "since": time.time() - 99999})
    monkeypatch.setattr(prog, "activity", lambda: (11, 0))     # counter advanced
    assert prog.main() == 0
    assert prog.read_state()["since"] > time.time() - 5


def test_a_different_profile_restarts_the_clock(prog, monkeypatch):
    """Someone activated something else; the old profile's idle time says nothing."""
    _topo(prog, profile="minimax")
    _never_unloads(prog, monkeypatch)
    prog.write_state({"profile": "qwen3.6", "tokens": 10, "since": time.time() - 99999})
    monkeypatch.setattr(prog, "activity", lambda: (10, 0))
    assert prog.main() == 0
    assert prog.read_state()["profile"] == "minimax"


def test_empty_is_never_unloaded_again(prog, monkeypatch):
    _topo(prog, profile="empty")
    _never_unloads(prog, monkeypatch)
    assert prog.main() == 0


def test_unloads_only_after_the_full_quiet_period(prog, monkeypatch):
    """The happy path, and the ONLY action it can take: `empty`."""
    _topo(prog)
    calls = []
    monkeypatch.setattr(prog, "activity", lambda: (10, 0))
    monkeypatch.setattr(prog, "unload", lambda: calls.append(prog.DESIRED) or 0)

    prog.write_state({"profile": "qwen3.6", "tokens": 10, "since": time.time() - 60})
    assert prog.main() == 0 and not calls, "60s of quiet is not 4 hours"

    prog.write_state({"profile": "qwen3.6", "tokens": 10, "since": time.time() - 7200})
    assert prog.main() == 0
    assert calls, "two hours past the threshold and it did not unload"


def test_the_only_profile_it_can_request_is_empty():
    """The safety property the whole design rests on: an unattended actor whose single
    possible action is the FAIL-SAFE target cannot make the cluster less safe. It must not
    be able to name, choose or start a model."""
    src = IDLE.read_text()
    body = src.split("def unload(")[1].split("\ndef ")[0]
    assert "EMPTY" in body
    for forbidden in ("profile", "sys.argv", "input("):
        assert forbidden not in body, f"unload() must not be steerable by {forbidden!r}"
    assert 'EMPTY = "empty"' in src


def test_it_holds_no_privilege_of_its_own():
    """It calls the same bounded reconciler `activate` does, through the grant the
    activate group already has. If this ever grows its own sudoers entry, that is a new
    privileged program and needs the scrutiny ADR-0018 gives the other three."""
    src = IDLE.read_text()
    assert "/usr/local/sbin/vllm-activate" in src
    assert "vllm-probe" not in src and "vllm-runbook" not in src
    assert "os.geteuid" not in src, "it must not expect to run as root"


@pytest.mark.parametrize("threshold", [0, -1, 0.0])
def test_a_zero_threshold_disables_rather_than_firing_immediately(prog, monkeypatch, threshold):
    """`idle_unload_after: 0` must mean OFF, not "unload now".

    Read literally it says "idle for >= 0 seconds, so unload" — firing on the FIRST check
    and leaving the cluster unserveable. Nobody types 0 to mean that. Interpreting it
    literally would hand the most destructive setting to the most natural way of asking for
    the least, so zero and negative both disable.
    """
    _topo(prog)
    _never_unloads(prog, monkeypatch)
    monkeypatch.setattr(prog, "activity", lambda: (10, 0))
    prog.IDLE_AFTER = threshold
    prog.DISABLED = (not prog.ENABLED) or prog.IDLE_AFTER <= 0
    prog.write_state({"profile": "qwen3.6", "tokens": 10, "since": time.time() - 99999})
    assert prog.main() == 0


def test_the_role_does_not_even_arm_the_timer_at_zero():
    """Belt and braces: the script no-ops AND the timer is left unenabled. Either alone
    would do; neither is where the next person will look."""
    root = Path(__file__).resolve().parent.parent
    tasks = (root / "ansible/roles/activate/tasks/main.yml").read_text()
    block = tasks.split("Enable the idle timer")[1].split("- name:")[0]
    assert "idle_unload_after | int) > 0" in block, \
        "the timer must not be armed when the threshold disables it"


# --- implicit wake: restore-only, driven by demand ------------------------

def test_wake_restores_the_profile_it_unloaded(prog, monkeypatch):
    """The wake half. Caddy holds an inference request when nothing serves; that shows up
    as an in-flight request, which is the demand signal — no endpoint, nothing invocable."""
    prog.LAST_PROFILE = prog.CLUSTER / "last-serving-profile"
    prog.LAST_PROFILE.write_text("qwen3.6\n")
    prog.WAKE_ENABLED = True
    _topo(prog, profile="empty")
    monkeypatch.setattr(prog, "waiting_requests", lambda: 1)
    monkeypatch.setattr(prog, "subprocess", type("S", (), {
        "run": staticmethod(lambda *a, **k: type("R", (), {"returncode": 0})())})())
    assert prog.main() == 0
    assert prog.DESIRED.read_text().strip() == "qwen3.6"


def test_wake_does_nothing_without_demand(prog, monkeypatch):
    """An empty fleet with nobody waiting must STAY empty — that is the whole point."""
    prog.LAST_PROFILE = prog.CLUSTER / "last-serving-profile"
    prog.LAST_PROFILE.write_text("qwen3.6\n")
    prog.WAKE_ENABLED = True
    _topo(prog, profile="empty")
    monkeypatch.setattr(prog, "waiting_requests", lambda: 0)
    monkeypatch.setattr(prog, "restore", lambda: pytest.fail("woke with nobody waiting"))
    assert prog.main() == 0


def test_wake_cannot_be_steered_to_a_chosen_profile():
    """RESTORE, NEVER SELECT — the property that makes implicit wake acceptable.

    An unauthenticated request can cause the cluster to resume what it was already doing.
    It must never be able to choose a model, start one that was not running, or name a
    profile at all. The name comes from a file WE wrote when WE unloaded.
    """
    src = IDLE.read_text()
    body = src.split("def restore(")[1].split("\ndef ")[0]
    assert "LAST_PROFILE.read_text()" in body, "the name must come from our own marker"
    for forbidden in ("sys.argv", "input(", "environ", "urllib"):
        assert forbidden not in body, f"restore() must not take {forbidden!r} as input"


def test_the_demand_signal_never_invents_demand(prog, monkeypatch):
    """If Caddy cannot be reached, that is not evidence anyone is waiting. Guessing would
    make the cluster wake itself for nobody, forever."""
    prog.CADDY_METRICS = "http://127.0.0.1:1/metrics"     # nothing listening
    assert prog.waiting_requests() == 0


def test_wake_refuses_while_the_fleet_lock_is_held(prog, monkeypatch):
    """Same guard as unloading: never activate into a deploy or a campaign."""
    prog.LAST_PROFILE = prog.CLUSTER / "last-serving-profile"
    prog.LAST_PROFILE.write_text("qwen3.6\n")
    prog.WAKE_ENABLED = True
    _topo(prog, profile="empty")
    monkeypatch.setattr(prog, "waiting_requests", lambda: 5)
    monkeypatch.setattr(prog, "restore", lambda: pytest.fail("woke during a deploy"))
    fd = os.open(prog.FLEET_LOCK, os.O_RDWR | os.O_CREAT, 0o664)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert prog.main() == 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def test_no_test_can_reach_the_real_cluster(prog):
    """Guard the guard: every path the program writes must point inside tmp_path.

    A test wrote to the LIVE /opt/cluster/last-serving-profile on 2026-08-13 because the
    module derives its paths at import time and the fixture only overrode the parent.
    """
    for name in ("TOPOLOGY", "DESIRED", "FLEET_LOCK", "STATE", "LAST_PROFILE"):
        path = str(getattr(prog, name))
        assert "/opt/cluster" not in path, f"{name} still points at the real cluster: {path}"


def test_the_timer_survives_being_restarted():
    """A deploy restarts the timer mid-uptime, and it must still schedule (2026-08-13).

    With only `OnBootSec` + `OnUnitActiveSec`, a restarted timer has NO reachable anchor:
    boot is hours past (and Persistent=false skips missed triggers) and the service has not
    run since the restart. systemd schedules nothing — `list-timers` shows NEXT "-" — so
    scale-to-zero silently stops after every deploy.

    A dead-man's switch that dies quietly is worse than not having one.
    """
    root = Path(__file__).resolve().parent.parent
    timer = (root / "ansible/roles/activate/templates/vllm-idle.timer.j2").read_text()
    assert "OnActiveSec=" in timer, \
        "without an anchor relative to the TIMER starting, a deploy stops the switch"


# --- the demand signal must mean exactly one thing (ADR-0022 part 4) ------

def test_demand_is_counted_only_on_the_model_listener(prog, monkeypatch):
    """The first attempt summed in-flight across ALL Caddy servers and was meaningless.

    The metric carries {handler, server} and no host or path, so `:80` lumps the model
    endpoint together with Open WebUI's long-lived websockets, Grafana, the panel and the
    scrape doing the reading — it read 3 with nothing waiting (2026-08-13). Only the model
    listener's own server label answers "is anyone waiting for a MODEL?".
    """
    metrics = (
        'caddy_http_requests_in_flight{handler="subroute",server="srv0"} 7\n'   # the web UI
        'caddy_http_requests_in_flight{handler="subroute",server="srv1"} 2\n'   # the models
    )
    monkeypatch.setattr(prog, "model_server_label", lambda: "srv1")
    monkeypatch.setattr(prog.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(metrics))
    assert prog.waiting_requests() == 2, "srv0's websockets must not read as demand"


def test_the_server_label_is_resolved_not_assumed(prog, monkeypatch):
    """Caddy names servers srv0, srv1... in CONFIG ORDER. Hardcoding an index would
    silently point at the web UI the first time a site is added above the listener — and
    that failure looks like phantom demand rather than a bug."""
    cfg = json.dumps({
        "srv0": {"listen": [":80"]},
        "srv7": {"listen": ["127.0.0.1:8090"]},
    })
    prog.INNER_PORT = "8090"
    monkeypatch.setattr(prog.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(cfg))
    assert prog.model_server_label() == "srv7"


def test_an_unidentifiable_listener_reads_as_no_demand(prog, monkeypatch):
    """If we cannot tell which server is the model listener we must not guess: counting
    everything is exactly the bug this replaced."""
    monkeypatch.setattr(prog, "model_server_label", lambda: None)
    assert prog.waiting_requests() == 0


class _fake_response:
    def __init__(self, body): self._b = body.encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_a_manual_empty_is_not_undone_by_the_wake(prog, monkeypatch):
    """The manager must never reverse a deliberate `activate empty` (2026-08-13).

    `LAST_PROFILE` is written only when the manager unloads. If it is never cleared, this
    happens: manager unloads X (marker = X) -> operator later activates empty BY HAND to
    free the box for a build -> a stray held request restores X against an explicit
    instruction. So the marker is dropped whenever something is serving: it may only ever
    describe an unload we performed and have not yet undone.
    """
    prog.LAST_PROFILE = prog.CLUSTER / "last-serving-profile"
    prog.LAST_PROFILE.write_text("qwen3.6\n")        # a marker from an earlier unload
    _topo(prog, profile="minimax")                    # ...but something is serving NOW
    monkeypatch.setattr(prog, "activity", lambda: (10, 0))
    prog.main()
    assert prog.LAST_PROFILE.read_text().strip() == "", \
        "a stale marker survived while a profile was live — a later manual `empty` would be undone"


# --- wake latency: the timer must actually tick as fast as it claims ------

def _group_vars():
    import yaml
    root = Path(__file__).resolve().parent.parent
    return yaml.safe_load((root / "ansible/group_vars/all.yml").read_text())


def _timer():
    root = Path(__file__).resolve().parent.parent
    return (root / "ansible/roles/activate/templates/vllm-idle.timer.j2").read_text()


def test_the_timer_pins_its_own_accuracy():
    """THE highest-value test here, because the failure is SILENT.

    systemd's default `AccuracySec` is one MINUTE — it batches timer wake-ups to save power.
    Verified on this host before the change: `AccuracyUSec=1min`. So `OnUnitActiveSec=5s`
    without pinning accuracy produces a unit file that says 5 s and fires whenever it likes
    within the minute. Everything looks applied; nothing is.
    """
    assert "AccuracySec=" in _timer(), "without this the interval is a fiction"
    assert _group_vars()["idle_check_accuracy"] == "1s"


def test_all_three_anchors_share_one_interval():
    """Speeding up only the steady state leaves a 60 s hole after every deploy — precisely
    when the endpoint is most likely to be hit next."""
    t = _timer()
    for anchor in ("OnActiveSec", "OnBootSec", "OnUnitActiveSec"):
        assert f"{anchor}={{{{ idle_check_seconds }}}}s" in t, f"{anchor} drifted off the shared value"


def test_wake_latency_is_seconds_not_a_minute():
    """A held caller waits this long before anything looks, on top of a ~300 s cold start."""
    assert _group_vars()["idle_check_seconds"] <= 10


def test_the_jitter_stays_off():
    """Jitter is added directly to how long a held caller waits, and there is one timer on
    this box to spread."""
    assert _group_vars()["idle_check_jitter"] == 0


def test_the_program_thins_by_the_interval_the_timer_uses():
    """The thinning is sound only if the program and the timer share ONE interval. If they
    drift, narration silently over- or under-samples."""
    root = Path(__file__).resolve().parent.parent
    svc = (root / "ansible/roles/activate/templates/vllm-idle.service.j2").read_text()
    assert "IDLE_CHECK_SECONDS={{ idle_check_seconds }}" in svc
    assert "idle_check_interval" not in _group_vars(), "a second interval variable would drift"


def test_narration_is_thinned_but_actions_never_are(prog, monkeypatch):
    """Actions and failures always log; observations are sampled."""
    seen = []
    monkeypatch.setattr(prog, "log", lambda m: seen.append(m))
    prog.CHECK_EVERY, prog.NARRATE_EVERY = 5.0, 60.0
    clock = [1000.0]
    monkeypatch.setattr(prog.time, "time", lambda: clock[0])
    for _ in range(12):                      # one minute of 5s ticks
        prog.narrate("routine")
        clock[0] += 5
    assert len(seen) == 1, f"expected 1 narration per minute, got {len(seen)}"


def test_thinning_is_a_no_op_at_the_old_cadence(prog, monkeypatch):
    """Putting the interval back to 60 turns the thinning off — one number to move."""
    seen = []
    monkeypatch.setattr(prog, "log", lambda m: seen.append(m))
    prog.CHECK_EVERY, prog.NARRATE_EVERY = 60.0, 60.0
    clock = [1000.0]
    monkeypatch.setattr(prog.time, "time", lambda: clock[0])
    for _ in range(5):
        prog.narrate("routine")
        clock[0] += 60
    assert len(seen) == 5


def test_the_level_filter_does_not_swallow_the_program():
    """`LogLevelMax` drops PID 1's per-tick chatter (34,560 lines/day at 5 s). Alone it
    would ALSO swallow stdout and every traceback, because the unit's output is logged at
    info — a dead-man's switch that dies without saying so. `SyslogLevel` lifts it clear."""
    root = Path(__file__).resolve().parent.parent
    svc = (root / "ansible/roles/activate/templates/vllm-idle.service.j2").read_text()
    assert "LogLevelMax=" in svc and "SyslogLevel=notice" in svc, \
        "both are required; either alone is a bug"
