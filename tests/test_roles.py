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


# --- container images: sourced, pinned, and placed (ADR-0013) ---------------

def _group_vars() -> dict:
    return yaml.safe_load((PLAYBOOKS / "group_vars/all.yml").read_text())


def test_every_pulled_image_is_digest_pinned():
    """A floating tag is not a version. `:latest` pulled on two nodes days apart gives
    two different images — which is not hypothetical: it is exactly what happened to
    `alpine:latest` on this cluster. Locally-built images are exempt; they have no
    registry digest.
    """
    gv = _group_vars()
    floating = []
    for entry in gv["container_images"]:
        ref = entry.get("pull")
        if not ref:
            continue
        # entries reference a *_image var; resolve it
        var = ref.strip("{} \"'")
        resolved = gv.get(var, ref)
        if "@sha256:" not in resolved:
            floating.append(f"{var} = {resolved}")
    assert not floating, "these must be pinned by digest:\n  " + "\n  ".join(floating)


def test_images_are_run_by_the_same_reference_they_are_pulled_by():
    """The latent bug this fixes: `docker pull repo@sha256:…` creates no local tag, so
    a compose file or unit naming `repo:tag` would find nothing on a fresh node and
    trigger an unpinned runtime pull — reintroducing the drift the digest prevents."""
    gv = _group_vars()
    pulled = set()
    for entry in gv["container_images"]:
        if entry.get("pull"):
            pulled.add(gv.get(entry["pull"].strip("{} \"'"), entry["pull"]))
    for var in ("vllm_image", "webui_image", "caddy_image", "prometheus_image",
                "grafana_image", "node_exporter_image", "nvidia_exporter_image"):
        assert gv[var] in pulled, f"{var} is run but never pulled: {gv[var]}"


def test_head_only_images_are_not_pushed_to_workers():
    """Grafana and Open WebUI run only on the head; a worker holding them wastes disk
    on an image it can never run — the same reasoning as per-node model weights."""
    gv = _group_vars()
    placement = {e.get("pull", e.get("build")): e.get("hosts", "all")
                 for e in gv["container_images"]}
    for var in ("webui_image", "caddy_image", "prometheus_image", "grafana_image"):
        assert placement.get("{{ %s }}" % var) == "head", f"{var} should be head-only"
    for var in ("vllm_image", "node_exporter_image", "nvidia_exporter_image"):
        assert placement.get("{{ %s }}" % var) == "all", f"{var} should be on all nodes"


def test_the_panel_is_tested_against_the_fastapi_it_deploys():
    """tests/test_control_panel.py loads the panel's main.py under the HARNESS
    environment, so if pyproject declares a floor while requirements.txt pins an exact
    version, the panel is validated against a FastAPI it will never run. DEF-0005 is the
    standing reminder that one FastAPI minor can break a working app outright."""
    import re
    req = (ROLES / "control-panel/files/app/requirements.txt").read_text()
    pyproject = (PLAYBOOKS.parent / "pyproject.toml").read_text()
    for pkg in ("fastapi", "starlette"):
        shipped = re.search(rf"(?m)^{pkg}==(\S+)$", req)
        tested = re.search(rf'"{pkg}==(\S+?)"', pyproject)
        assert shipped and tested, f"{pkg}: pin both, exactly (req={bool(shipped)} dev={bool(tested)})"
        assert shipped.group(1) == tested.group(1), (
            f"{pkg}: panel deploys {shipped.group(1)} but tests run {tested.group(1)}")


def test_derived_images_are_not_gated_on_the_tag_existing():
    """A `build:` entry's tag never changes, so "the image is present" says nothing
    about whether it came from the CURRENT Dockerfile. Gating the build on presence let
    an edited Dockerfile be silently ignored — on 2026-08-08 a broken WAR survived the
    deploy meant to fix it, and the fix's own build-time assertion never ran because the
    build never ran. Docker's layer cache makes an always-run build cheap."""
    tasks = list(walk_tasks(ROLES / "images/tasks/ensure_one.yml"))
    # the build step *and* the block that wraps it — a guard on either one skips it
    guards = [str(t.get("when", "")) for t in tasks
              if "build" in str(t.get("name", "")).lower()]
    assert guards, "no build task found in ensure_one.yml"
    offenders = [g for g in guards if "image_present" in g]
    assert not offenders, (
        "the docker build step is gated on the image already existing — an edited "
        f"Dockerfile would be ignored. when: {offenders!r}")


def test_derived_images_patch_without_letting_the_resolver_loose():
    """A vendor image is a carefully-resolved dependency set. Installing one package
    without `--no-deps` lets pip "fix" the rest: on 2026-08-08 patching xgrammar pulled
    transformers back to v4, which vLLM 0.24.0 removed support for, and every engine
    died at import. Surgical or not at all — a needed dependency goes in explicitly."""
    loose = []
    for dockerfile in sorted((ROLES / "images/files").glob("*/Dockerfile")):
        body = dockerfile.read_text().replace("\\\n", " ")
        for line in body.splitlines():
            if line.lstrip().startswith("#") or "pip install" not in line:
                continue
            if "--no-deps" not in line:
                loose.append(f"{dockerfile.parent.name}: {line.strip()[:90]}")
    assert not loose, ("these pip installs can re-resolve the vendor's dependency set:\n  "
                       + "\n  ".join(loose))


def test_engine_env_is_rendered_and_passed_to_the_container():
    """`engine_env:` is worthless unless BOTH halves exist: a file rendered per engine,
    and the unit actually passing it to docker. It was added for DEF-0014, where a model
    needed an env var (`VLLM_USE_DEEP_GEMM=0`) that no serve flag could express."""
    role = ROLES / "vllm"
    tasks = (role / "tasks/main.yml").read_text()
    unit = (role / "templates/vllm@.service.j2").read_text()
    assert (role / "templates/engine.docker-env.j2").exists()
    assert "src: engine.docker-env.j2" in tasks
    assert "%i.docker-env" in unit, "the unit never passes the file to docker"
    assert "--env-file" in unit


def test_engine_env_file_is_rendered_even_when_empty():
    """docker fails on a missing --env-file, and most engines declare no env — so the
    template must not be conditional on `engine_env` being present."""
    tasks = (ROLES / "vllm/tasks/main.yml").read_text()
    block = tasks[tasks.index("engine.docker-env.j2"):]
    guard = block[:block.index("loop:")]
    assert "when:" not in guard, "rendering the container env file must be unconditional"
