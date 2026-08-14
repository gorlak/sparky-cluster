"""The fleet — the allowlist, and what it implies per node (ADR-0018).

**The allowlist is the profiles directory.** A deployable/keepable model *is* an
`ansible/profiles/*.yml`; there is no separate manifest to drift. Two gestures follow:

    blocked: true      keep the weights + the rendered env file, but the profile
                       cannot be ACTIVATED — parked, e.g. waiting on an upstream fix.
    delete the .yml    it leaves the allowlist, so the next `deploy` evicts its
                       weights (behind plan-and-confirm).

*Block to park it; delete the file to evict it.*

This module is the harness's typed view of that policy — the same derivation
`roles/fleet` does in Ansible, available to `sparky lint`, the CLI, and the test
regiment without running a playbook.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from sparky.topology import (API_PORT, PROFILES_DIR, Engine, Profile,  # noqa: F401
                             all_profiles)

GROUP_VARS = PROFILES_DIR.parent / "group_vars" / "all.yml"

# The one well-known front port. At most one live engine per port fleet-wide is what
# lets the stable endpoint be a static health-checked upstream list.
# Always activatable: `empty` is the fail-safe target, so it can never depend on a
# file being right.
EMPTY = "empty"


class FleetError(Exception):
    """A profile set that `deploy` would refuse."""


@dataclass(frozen=True)
class Placement:
    """One engine and the profile that selects it."""

    profile: Profile
    engine: Engine

    @property
    def blocked(self) -> bool:
        return self.profile.blocked


@dataclass(frozen=True)
class Fleet:
    """Every profile in the allowlist, and the engines they place."""

    profiles: tuple[Profile, ...]

    @property
    def placements(self) -> tuple[Placement, ...]:
        return tuple(Placement(p, e) for p in self.profiles for e in p.engines)

    @property
    def allowlist(self) -> list[str]:
        """ACTIVATABLE profile names — everything not parked."""
        return sorted(p.name for p in self.profiles if not p.blocked)

    @property
    def models(self) -> list[str]:
        """Every model the fleet keeps — including parked profiles', which is the
        point of `blocked`: it holds the weights so re-testing costs no download."""
        return sorted({m for pl in self.placements for m in pl.engine.all_models})

    @property
    def nodes(self) -> list[str]:
        return sorted({n for pl in self.placements for n in pl.engine.nodes})

    def engines_on(self, node: str) -> list[Engine]:
        return [pl.engine for pl in self.placements if node in pl.engine.nodes]

    def models_on(self, node: str, *, head: str | None = None) -> list[str]:
        """The weights a node must hold.

        Workers hold exactly what their engines serve — per-node disk tracks what the
        node actually runs, not the whole fleet. The head is the exception: it is the
        canonical store and the rsync source every other node mirrors from, so
        evicting a worker-only model there would leave no way to repair that worker's
        copy short of re-downloading.
        """
        if head is not None and node == head:
            return self.models
        return sorted({m for e in self.engines_on(node) for m in e.all_models})

    def evictions_on(self, node: str, present: list[str], *, head: str | None = None) -> list[str]:
        """What `deploy --evict` would delete on `node`, given what's on its disk."""
        keep = set(self.models_on(node, head=head))
        return sorted(m for m in present if m not in keep)

    def profile(self, name: str) -> Profile:
        for p in self.profiles:
            if p.name == name:
                return p
        raise KeyError(name)

    def validate(self) -> None:
        """The invariants `deploy` asserts before writing anything. Raises FleetError
        with everything that's wrong, not just the first thing."""
        problems: list[str] = []

        names = [p.name for p in self.profiles]
        if len(set(names)) != len(names):
            problems.append(
                f"profile names must be unique (the activation key): {sorted(names)}")

        engines = [pl.engine.name for pl in self.placements]
        if len(set(engines)) != len(engines):
            dupes = sorted({e for e in engines if engines.count(e) > 1})
            problems.append(
                f"engine names must be unique FLEET-wide, not just within a profile — "
                f"an engine name is its systemd instance (vllm@<name>.service) and its "
                f"env file: {dupes}")

        # Archetypes are what tests bind to instead of model names, so a typo does not
        # fail — it silently matches nothing, and the test passes while checking nothing.
        # This is also the guard that catches YAML's `[null]` parsing as `[None]`, which
        # is how the `empty` profile lost its only tag on 2026-08-10.
        from .topology import ARCHETYPES
        for prof in self.profiles:
            unknown = [a for a in prof.archetypes if a not in ARCHETYPES]
            if unknown:
                problems.append(
                    f"{prof.name}: unknown archetype(s) {unknown} — known: "
                    f"{sorted(ARCHETYPES)}. (A bare `null` in YAML parses as None, not "
                    f'the string "null".)')

        offenders = sorted({pl.engine.name for pl in self.placements if pl.engine.port != API_PORT})
        if offenders:
            problems.append(
                f"every engine must serve on port {API_PORT} (ADR-0018: at most one "
                f"live engine per port fleet-wide is what makes the stable endpoint a "
                f"static upstream list): {offenders}")

        for pl in self.placements:
            for arg in tuple(pl.engine.head_extra_args) + tuple(pl.engine.worker_extra_args):
                if "'" in arg or "\n" in arg:
                    problems.append(
                        f"{pl.engine.name}: serve flag {arg!r} cannot survive the engine "
                        f"env file — it is wrapped in single quotes as one value. Spaces "
                        f"and double quotes are fine (the renderer escapes quotes, because "
                        f"systemd's $VAR expansion unquotes); single quotes and newlines "
                        f'are not. Write JSON args unspaced, e.g. -sc {{"method":"mtp"}}')

        # --- flags that MUST be on both ranks ------------------------------------
        # See BOTH_RANK_FLAGS for why, and docs/bring-up-failures.md for what a violation
        # looks like from the outside.
        for pl in self.placements:
            if not pl.engine.is_multinode:
                continue
            head = {a.split()[0] for a in pl.engine.head_extra_args if a.strip()}
            worker = {a.split()[0] for a in pl.engine.worker_extra_args if a.strip()}
            for flag in BOTH_RANK_FLAGS:
                if flag in head and flag not in worker:
                    problems.append(
                        f"{pl.engine.name}: {flag} is on the head but not the worker. It "
                        f"changes how the MODEL is built, and every rank builds its own "
                        f"VllmConfig — so the ranks would disagree and TP=2 deadlocks at "
                        f"startup — exactly this cost an hour of stalled cluster on 2026-08-12; see docs/bring-up-failures.md. "
                        f"Add it to worker_extra_args too.")
                if flag in worker and flag not in head:
                    problems.append(
                        f"{pl.engine.name}: {flag} is on the worker but not the head — "
                        f"same desync, mirrored. Add it to head_extra_args too.")

        managed = managed_images()
        if managed:
            for p in self.profiles:
                if p.vllm_image and p.vllm_image not in managed:
                    problems.append(
                        f"{p.name}: selects {p.vllm_image!r}, which is not in "
                        f"container_images — nothing would pull or build it, and the "
                        f"engine would fail to start (ADR-0013)")

        if problems:
            raise FleetError("\n".join(f"  - {p}" for p in problems))
# Flags that MUST be on both ranks.
#
# Every rank builds its own VllmConfig from its own argv. A flag that changes the MODEL —
# how it is parsed, loaded, or executed — on only one rank makes the ranks disagree about
# the work to do, and TP=2 then deadlocks: each blocks on a collective the other never
# issues. It presents as a hang at startup, minutes from the flag that caused it, with no
# error naming it.
#
# A NAMED LIST rather than "both argv must match", because the opposite case is legitimate:
# API-surface flags (`--enable-auto-tool-choice`, `--tool-call-parser`, `--reasoning-parser`)
# belong to the API server, which runs on the head alone.
BOTH_RANK_FLAGS: frozenset[str] = frozenset({
    "--tokenizer-mode",      # config-time tokenizer TYPE validation, per rank
    "--config-format",       # which config file the rank parses
    "--load-format",         # which weight layout the rank's loader expects
    "--speculative-config",  # adds a draft model + an extra profiling pass, per rank
    "--quantization",        # changes the kernels a rank loads
    "--kv-cache-dtype",      # changes each rank's KV allocation
    "--attention-backend",   # each rank builds its own attention; a split here deadlocks
    "--limit-mm-per-prompt", # multimodal config, and profiling runs per rank
})


def managed_images(path=GROUP_VARS) -> set[str]:
    """Every image `container_images` guarantees, with `{{ var }}` references resolved.

    ADR-0013's rule is that a profile may only select an image the `images` role ensures.
    Unenforced, a profile naming an unmanaged image deploys "successfully" and then fails
    at engine start — and since the pins are digests copied by hand, one wrong character
    does it.
    """
    try:
        gv = yaml.safe_load(path.read_text()) or {}
    except OSError:
        return set()
    out = set()
    for entry in gv.get("container_images") or []:
        ref = entry.get("pull") or entry.get("build") or ""
        out.add(gv.get(ref.strip("{} \"\'"), ref) if ref.startswith("{{") else ref)
    return {r for r in out if r}


def load_fleet(profiles_dir=PROFILES_DIR) -> Fleet:
    """The fleet as committed under `ansible/profiles/`."""
    return Fleet(tuple(all_profiles(profiles_dir)))
