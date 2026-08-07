"""Structural guards over the Ansible roles (ADR-0011 Layer 1, no hardware).

`sparky lint` proves the playbooks *parse*; these prove things a syntax check can't
see. They exist because each one has actually bitten a deploy — a role that only
fails on the node, ten minutes in, is the expensive kind of bug.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROLES = Path(__file__).resolve().parent.parent / "ansible/roles"
PLAYBOOKS = Path(__file__).resolve().parent.parent / "ansible"


def task_files() -> list[Path]:
    return sorted(ROLES.glob("*/tasks/*.yml"))


def walk_tasks(path: Path):
    """Every task dict in a task file, including those nested in block/rescue/always."""
    def _walk(items):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            yield item
            for key in ("block", "rescue", "always"):
                yield from _walk(item.get(key))

    yield from _walk(yaml.safe_load(path.read_text()))


def test_ansible_managed_is_only_used_by_the_template_module():
    """`ansible_managed` is provided by the *template* action, not as a global var —
    ansible-core 2.20 no longer defines it anywhere else. Using it inside
    `copy: content:` templates fine locally and dies on the node with
    "'ansible_managed' is undefined", after earlier tasks have already changed state.
    """
    offenders = []
    for path in task_files():
        for task in walk_tasks(path):
            for module, args in task.items():
                if not isinstance(args, dict) or "content" not in args:
                    continue
                if "ansible_managed" in str(args.get("content", "")):
                    offenders.append(f"{path.relative_to(ROLES.parent)}: {task.get('name')} "
                                     f"({module})")
    assert not offenders, (
        "these tasks reference ansible_managed outside the template module — render "
        "them with `template:` and a .j2 file instead:\n  " + "\n  ".join(offenders))


def test_every_templated_src_exists():
    """A `template: src:` naming a file that isn't there fails only when the task runs."""
    missing = []
    for path in task_files():
        role = path.parent.parent
        for task in walk_tasks(path):
            args = task.get("ansible.builtin.template") or task.get("template")
            if not isinstance(args, dict):
                continue
            src = args.get("src", "")
            if "{{" in src:  # computed at run time; can't check statically
                continue
            if not (role / "templates" / src).exists():
                missing.append(f"{path.relative_to(ROLES.parent)}: {task.get('name')} -> {src}")
    assert not missing, "template src not found:\n  " + "\n  ".join(missing)


def test_every_copied_src_exists():
    """Same, for `copy: src:` (the reconciler is shipped this way — verbatim, so that
    the program `deploy` installs is byte-identical to the one under test)."""
    missing = []
    for path in task_files():
        role = path.parent.parent
        for task in walk_tasks(path):
            args = task.get("ansible.builtin.copy") or task.get("copy")
            if not isinstance(args, dict) or "src" not in args:
                continue
            src = args["src"]
            if "{{" in src:
                continue
            if not (role / "files" / src.rstrip("/")).exists():
                missing.append(f"{path.relative_to(ROLES.parent)}: {task.get('name')} -> {src}")
    assert not missing, "copy src not found:\n  " + "\n  ".join(missing)


def test_every_role_referenced_by_a_playbook_exists():
    """A play naming a deleted role fails at parse time on the node, not here."""
    known = {p.name for p in ROLES.iterdir() if p.is_dir()}
    missing = []
    for playbook in sorted(PLAYBOOKS.glob("*.yml")):
        plays = yaml.safe_load(playbook.read_text())
        if not isinstance(plays, list):  # inventory.yml et al — not a playbook
            continue
        for play in plays:
            for entry in (play.get("roles") or []):
                name = entry.get("role") if isinstance(entry, dict) else entry
                if name and name not in known:
                    missing.append(f"{playbook.name}: {name}")
    assert not missing, "playbook references a role that doesn't exist:\n  " + "\n  ".join(missing)
