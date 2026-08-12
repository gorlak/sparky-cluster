"""Typed reader of the cluster's serving topology (ADR-0010 shared substrate).

A profile (`ansible/profiles/<name>.yml`) declares the serving topology once as
structured data; every model-dependent service is a projection of it (see
`docs/serving-topology.md`). This module is the harness's typed view of that
declaration — the base both the test regiment (ADR-0011) and the benchmark
regiment (ADR-0012) read to know which engines exist, where they serve, and under
what unit/served name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# The repo when there is one, the published tree when there is not (ADR-0021). Since the
# harness is now *installed* on the head — a venv, so a detached runbook run has an
# interpreter — `__file__` can sit in site-packages, where `../ansible/profiles` is
# nothing. `/opt/cluster/ansible` is the same content, published by the same deploy.
PROFILES_DIR = next(
    (d for d in (REPO_ROOT / "ansible" / "profiles",
                 Path("/opt/cluster/ansible/profiles")) if d.is_dir()),
    REPO_ROOT / "ansible" / "profiles")
ANSIBLE_DIR = PROFILES_DIR.parent
# Written at the end of every deploy (runtime, not in the repo tree).
CURRENT_TOPOLOGY = Path("/opt/cluster/current-topology.json")


@dataclass(frozen=True)
class Engine:
    """One vLLM engine as declared in a profile's `serving_topology`."""

    name: str
    kind: str
    nodes: tuple[str, ...]
    port: int
    model: str
    served_as: str
    tensor_parallel_size: int
    gpu_memory_utilization: float
    max_model_len: int
    head_extra_args: tuple[str, ...] = ()
    worker_extra_args: tuple[str, ...] = ()
    # Weights this engine NEEDS but does not SERVE — today, a speculative-decoding draft
    # model. `model` drives the served name and the API; the fleet role derives which
    # weights each node must hold from these two together.
    #
    # It exists because a draft is named inside a FLAG
    # (`--speculative-config {"model":"/models/…-eagle",…}`) and a flag is an opaque
    # string to everything upstream of the engine. Without this the deploy would leave
    # the draft in the inbox, never mirror it to the worker, and the engine would die on
    # a path that is not there — a failure whose cause is nowhere near its symptom.
    extra_models: tuple[str, ...] = ()

    @property
    def all_models(self) -> tuple[str, ...]:
        """Every weight directory this engine needs on a node that runs it."""
        return (self.model, *self.extra_models)

    @property
    def api_node(self) -> str:
        """`nodes[0]` is rank 0 — the node that hosts the API (ADR-0003)."""
        return self.nodes[0]

    @property
    def is_multinode(self) -> bool:
        return len(self.nodes) > 1

    @property
    def unit(self) -> str:
        """systemd unit — ONE template unit, instanced per engine (ADR-0018), with
        the same name on every node it spans (ADR-0003)."""
        return f"vllm@{self.name}.service"

    @property
    def env_file(self) -> str:
        """The engine's whole per-variant surface, rendered by `deploy`."""
        return f"/opt/vllm/engines/{self.name}.env"

    @property
    def active_marker(self) -> str:
        """The PER-NODE desired marker the reconciler writes; one of the two
        `ConditionPathExists` gates that decide whether this unit boots."""
        return f"/opt/vllm/active/{self.name}"

    @property
    def container(self) -> str:
        return f"vllm-{self.name}"

    def rank_of(self, node: str) -> int:
        """torch.distributed rank = the node's index in `nodes`."""
        return self.nodes.index(node)


# --- archetypes: what a profile is an EXAMPLE OF ------------------------------
#
# Tests kept naming specific models — `step-3.5-flash-fp8` for "a profile on the old container",
# `qwen3-coder-next-nvfp4-single` for "a TP=1 shape". Both are instances standing in for a
# SHAPE, and on 2026-08-10 seven profiles were retired in one afternoon and four tests
# broke, none of which cared about those models at all.
#
# An archetype names the property a test is actually reaching for, so the fleet can churn
# underneath it and the test still says what it means. `by_archetype("single-node")` reads
# as the reason it is there; `load_profile("qwen3-coder-next-nvfp4-single")` does not.
#
# Keep this vocabulary SMALL. An archetype per profile would be a second naming scheme
# with no leverage — the point is that several profiles share one, and that a test binds
# to the shared property rather than to whichever profile happens to have it today.
ARCHETYPES: dict[str, str] = {
    "big-shared":      "TP=2 across every node — one model sharded, the whole fleet committed",
    "single-node":     "TP=1 on one worker, leaving the other node free for dev work",
    # NOT "null": YAML parses a bare `null` as None, so `archetypes: [null]` silently
    # becomes `[None]` and matches nothing. Named for what a test wants it FOR.
    "fail-safe":       "no engines: the `empty` profile, always activatable, the recovery target",
    "mixed-precision": "checkpoint self-declares MIXED_PRECISION — never pass --quantization",
    "tool-calling":    "carries a verified --tool-call-parser (a guessed name refuses to start)",
    "vision":          "multimodal — the vision gate applies",
}

# The ONLY suffixes we are allowed to invent — a closed set, and deliberately tiny.
#
# A profile's name IS the upstream repo's model name, lowercased. It is NOT composed. The
# quant appears in most names (`…-nvfp4`, `…-awq`) purely because the VENDOR put it in the
# repo name — `nvidia/Mistral-Medium-3.5-128B-NVFP4` really is called that. When a vendor
# ships a quant as its base repo (`mistralai/Mistral-Medium-3.5-128B`, genuinely FP8), the
# profile is `mistral-medium-3.5-128b`, and adding `-fp8` would invent a name that matches
# nothing on the Hub — breaking the one property the scheme exists for.
#
# That mistake was made on 2026-08-11, by following `docs/profiles.md`, which described the
# name as a "`<model>-<version>-<quant>` triple" — a rule for CONSTRUCTING names when the
# enforced rule is to COPY one.
#
# WHAT A SUFFIX IS FOR: **a second way of serving the SAME weights** — something a repo
# name cannot express, because upstream ships one checkpoint and we serve it two ways. That
# is the whole test, and it admits exactly two kinds:
#
#   * TOPOLOGY   — `-single`: TP=1 on one worker instead of TP=2 across both.
#   * OPTIMIZATION — `-eagle`, `-mtp3`: speculative decoding on, against a bare-name twin
#     with it off. These exist to be **A/B'd** (ADR-0014): the pair is the experiment, and
#     collapsing them into one edited profile destroys the control. The retired
#     `qwen3.6-35b-a3b-nvfp4-mtp3-single` / `…-nvfp4-single` pair is the precedent.
#
# The first version of this constant listed only `-single`, which would have refused
# `-mtp3` — a name the repo had already shipped — and pushed the EAGLE experiment into
# editing one profile in place. A quant or precision still never qualifies: it is either in
# the upstream name already or it is not ours to add.
VARIANT_SUFFIXES: tuple[str, ...] = ("-single", "-eagle", "-mtp3")


def name_matches_repo(profile_name: str, hf_repo: str) -> bool:
    """Is `profile_name` the repo's model name, give or take one topology suffix?

    The enforced half of the naming rule, as a function so the test and any future lint
    check cannot drift apart the way the three prose copies did.
    """
    model = hf_repo.partition("/")[2].lower()
    if profile_name == model:
        return True
    return any(profile_name == model + s for s in VARIANT_SUFFIXES)


@dataclass(frozen=True)
class Profile:
    """A profile: its serving topology, and whether it may be activated.

    Since ADR-0018 a profile is an entry in the **allowlist** (the profiles
    directory) rather than a thing you deploy: `deploy` installs every profile's
    engines, `activate <name>` picks one to serve. `blocked` parks a profile — its
    weights and env files are kept, but it cannot be activated.
    """

    name: str
    engines: tuple[Engine, ...]
    vllm_image: str | None = None
    blocked: bool = False
    path: Path | None = None
    # What this profile is an EXAMPLE OF. See ARCHETYPES above; validated by lint, so a
    # typo is caught at Layer 1 rather than by a test quietly matching nothing.
    archetypes: tuple[str, ...] = ()
    # The exact upstream repo, `org/Name`. The profile name is that name lowercased, which
    # is enough to operate but not enough to paste into huggingface.co — the org is not
    # recoverable from it (`Qwen3-Coder-Next-NVFP4` is RedHatAI's, not Qwen's). Kept so the
    # scoreboard can show what a human would search for.
    hf_repo: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.engines

    def engine(self, name: str) -> Engine:
        for e in self.engines:
            if e.name == name:
                return e
        raise KeyError(f"no engine {name!r} in profile {self.name!r}")



# The one well-known front port. At most ONE live engine per port fleet-wide is what
# lets the stable endpoint be a static upstream list (ADR-0018), so this is an invariant
# `lint` asserts — which makes stating it per profile a request for a value that can only
# have one answer. Defined here rather than in `fleet` because the PARSER needs it and
# fleet imports topology, not the other way round.
API_PORT = 8000

INVENTORY = ANSIBLE_DIR / "inventory.yml"


def fleet_nodes(inventory: Path = INVENTORY) -> tuple[str, ...]:
    """Every node, head first — the default `nodes` for an engine.

    Order is not cosmetic: `nodes[0]` is rank 0, the API host and the NCCL master
    (ADR-0003), so head-then-workers is the only ordering that means anything.

    Read from the INVENTORY rather than hardcoded, because the inventory is already the
    file that owns node names. Before 2026-08-10 all eight profiles repeated
    `nodes: [sparky, snoopy]`, which meant adding a third node was an edit to every
    profile — and put hostnames in eight places that had no business knowing them.
    """
    data = yaml.safe_load(inventory.read_text()) or {}
    children = ((data.get("all") or {}).get("children") or {})
    head = list(((children.get("head") or {}).get("hosts") or {}))
    workers = [h for group, spec in children.items() if group != "head"
               for h in ((spec or {}).get("hosts") or {})]
    return tuple(head + workers)


def _engine_from_dict(d: dict) -> Engine:
    # Defaults, so a profile states only what makes it DIFFERENT. Since 2026-08-10 every
    # profile is TP=2 across both nodes on port 8000, and repeating that eight times was
    # ceremony that also hardcoded hostnames into files with no business knowing them.
    # A profile can still say something else — the constraint is measured, not structural,
    # and it is six hours old (docs/profile-tuning.md).
    nodes = tuple(d.get("nodes") or fleet_nodes())
    return Engine(
        name=d["name"],
        kind=d.get("kind", "vllm"),
        nodes=nodes,
        port=int(d.get("port", API_PORT)),
        model=d["model"],
        served_as=d["served_as"],
        tensor_parallel_size=int(d.get("tensor_parallel_size", len(nodes))),
        gpu_memory_utilization=float(d["gpu_memory_utilization"]),
        max_model_len=int(d["max_model_len"]),
        head_extra_args=tuple(d.get("head_extra_args") or ()),
        worker_extra_args=tuple(d.get("worker_extra_args") or ()),
        extra_models=tuple(d.get("extra_models") or ()),
    )


def load_profile(name_or_path: str | Path) -> Profile:
    """Load `<name>` from `ansible/profiles/`, or a direct path to a profile YAML.

    Profile names carry version dots (`minimax-m2.7-awq`), so a bare name is only
    treated as a path when it has a `.yml`/`.yaml` suffix or already exists.
    """
    path = Path(name_or_path)
    if path.suffix not in (".yml", ".yaml") and not path.exists():
        path = PROFILES_DIR / f"{name_or_path}.yml"
    data = yaml.safe_load(path.read_text()) or {}
    engines = tuple(
        _engine_from_dict(e)
        for e in (data.get("serving_topology") or [])
        if e.get("kind", "vllm") == "vllm"
    )
    return Profile(
        name=data.get("profile_name", path.stem),
        engines=engines,
        vllm_image=data.get("vllm_image"),
        blocked=bool(data.get("blocked", False)),
        archetypes=tuple(data.get("archetypes") or ()),
        hf_repo=data.get("hf_repo"),
        path=path,
    )


def all_profiles(profiles_dir: Path = PROFILES_DIR) -> list[Profile]:
    """Every profile committed under `ansible/profiles/` — the allowlist, name-sorted."""
    return [load_profile(p) for p in sorted(profiles_dir.glob("*.yml"))]


def load_current_topology(path: Path = CURRENT_TOPOLOGY) -> dict | None:
    """The live deployed topology (written each deploy), or None if absent."""
    if not path.exists():
        return None
    return json.loads(path.read_text())


def by_archetype(name: str, *, include_retired: bool = False,
                 profiles_dir: Path = PROFILES_DIR) -> list[Profile]:
    """Every profile that is an example of `name`. See `ARCHETYPES`.

    `include_retired` also searches `profiles/retired/`, which is NOT the allowlist and
    is invisible to `all_profiles()`. That is deliberate rather than a loophole: a shape
    can have no live example and still need its projection tested — `single-node` has
    none today, yet the rendering must stay correct for the day fleet occupancy makes it
    worth reviving one. A test that silently found nothing would pass while checking
    nothing at all.
    """
    if name not in ARCHETYPES:
        raise KeyError(f"unknown archetype {name!r}; known: {sorted(ARCHETYPES)}")
    found = [p for p in all_profiles(profiles_dir) if name in p.archetypes]
    if include_retired:
        retired = profiles_dir / "retired"
        if retired.is_dir():
            found += [p for p in (load_profile(f) for f in sorted(retired.glob("*.yml")))
                      if name in p.archetypes]
    return found


def one_of(name: str, **kw) -> Profile:
    """The first example of an archetype, or a loud failure.

    Tests want "a profile of this shape" far more often than a particular one. Raising
    here rather than returning None means a vocabulary that has drifted out of sync with
    the fleet is reported as a broken test, not as a passing one.
    """
    found = by_archetype(name, **kw)
    if not found:
        raise LookupError(
            f"no profile has archetype {name!r} ({ARCHETYPES[name]}). "
            f"Either tag one, or pass include_retired=True if an archived example will do.")
    return found[0]
