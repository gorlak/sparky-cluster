"""The command scopes (ADR-0018), owned here rather than in the CLI (ADR-0027).

`sparky` mixes three kinds of command that differ in the one way that matters
operationally: whether they need a password, whether an agent may run them, and whether
they need a live cluster. That is ADR-0018's boundary. It lived only in the `cli` module's
help-panel strings, which forced two things to reach *up* into the entrypoint to read it:
`suite` (to know which commands a suite may invoke) and `test_cli_surface` (to validate the
grouping). A library importing the entrypoint is backwards; the scope is a fact about the
command surface, not a feature of the CLI, so it lives here and both read it.

`OPERATE_COMMANDS` is the security-relevant half: the surface an agent — and therefore a
suite step — may drive. It is **declared, not inferred** from help panels, because an
allowlist that decides what unprivileged automation may run should be reviewed as itself,
not reconstructed from formatting. `cli` tags its commands with these panel strings and a
test asserts its Operate commands are exactly this set, so the two cannot drift.
"""

from __future__ import annotations

OPERATE = "Operate — no privilege, agent-drivable, needs a live cluster"
PROVISION = "Provision — password-gated (sudo -u deploy), control node only"
DEVELOP = "Develop — repo only, no cluster, no privilege"

# The closed vocabulary. A fourth scope should be a deliberate act with a reason.
PANELS = frozenset({OPERATE, PROVISION, DEVELOP})

# The Operate surface, by command name — what an agent or a suite may invoke.
OPERATE_COMMANDS = frozenset({
    "activate", "status", "fleet", "logs", "smoke", "bench", "eval", "coding",
    "run", "suite", "scoreboard", "report", "topology", "teardown", "probe",
})
