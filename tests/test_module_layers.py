"""The four tiers, enforced (ADR-0027).

`sparky` is a stack: `foundation` < `verify` < `serve` < `measure`, with `cli` above all
of them. Dependencies point the way need points — a thing may import what it is built ON,
never what is built FROM it. `measure` may reach down to `serve` to activate a model;
`serve` may not reach up to `measure`, or a measurement bug could take activation down with
it. `foundation` imports nothing else in `sparky` at all, which is what makes it the base a
reboot can depend on.

Prose in the tier `__init__` files states the rule; this makes it *true*. Without it the
layering is a diagram that drifts the first time a deferred `from sparky.measure import …`
is dropped into `serve` to fix something quickly — exactly the drift that put the smoke gate
in the CLI and the fleet lock in two files. A violation fails the suite with the file, the
edge, and the direction it broke.
"""

from __future__ import annotations

import ast
import pathlib

SPARKY = pathlib.Path(__file__).resolve().parent.parent / "sparky"

# Bottom to top. A module may import its own tier and any BELOW it, never one above.
RANK = {"foundation": 0, "verify": 1, "serve": 2, "measure": 3, "cli": 4}


def _tier_of_file(path: pathlib.Path) -> str | None:
    """The tier a module lives in, or None for the top-level (`cli`, `__init__`)."""
    rel = path.relative_to(SPARKY)
    if len(rel.parts) >= 2 and rel.parts[0] in RANK:
        return rel.parts[0]
    return None


def _tiers_imported(tree: ast.AST) -> set[str]:
    """Every sparky tier a module pulls in, across top-level AND deferred imports."""
    tiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "sparky" and len(parts) >= 2 and parts[1] in RANK:
                tiers.add(parts[1])                     # from sparky.<tier>[...] import X
            elif node.module == "sparky":
                for alias in node.names:                # from sparky import <tier|cli>
                    if alias.name in RANK:
                        tiers.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")           # import sparky.<tier>...
                if parts[0] == "sparky" and len(parts) >= 2 and parts[1] in RANK:
                    tiers.add(parts[1])
    return tiers


def _modules():
    return [p for p in SPARKY.rglob("*.py") if p.name != "__init__.py"]


def test_the_four_tiers_exist():
    for tier in ("foundation", "verify", "serve", "measure"):
        assert (SPARKY / tier / "__init__.py").is_file(), f"missing tier package: {tier}"


def test_every_module_lives_in_a_tier_or_is_the_cli():
    """A new module dropped flat into `sparky/` forces a tier decision rather than silently
    belonging to none — the same 'declare your scope' discipline the CLI surface uses."""
    stray = [p.name for p in _modules()
             if _tier_of_file(p) is None and p.name != "cli.py"]
    assert not stray, (f"modules with no tier: {stray}. Put each in foundation/, verify/, "
                       f"serve/ or measure/, or it belongs beside cli.py as an entrypoint.")


def test_imports_only_point_downward():
    violations: list[str] = []
    for path in _modules():
        tier = _tier_of_file(path)
        if tier is None:
            continue                                    # cli sits above every tier
        limit = RANK[tier]
        for imported in _tiers_imported(ast.parse(path.read_text())):
            if RANK[imported] > limit:
                violations.append(
                    f"{path.relative_to(SPARKY.parent)}: {tier} imports {imported} "
                    f"({tier} < {imported} — dependencies must point downward)")
    assert not violations, "tier import-direction violated:\n  " + "\n  ".join(violations)
