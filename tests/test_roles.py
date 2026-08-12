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
    # An image is guaranteed present by EITHER a pull (upstream, digest-pinned) or a
    # build (derived, no registry digest to pin). Since 2026-08-10 `vllm_image` is the
    # derived 26.07 image, so a pull-only check would fail on the fleet's only container.
    # The invariant is unchanged — every runnable reference is guaranteed — but "pulled"
    # was only ever a proxy for it.
    guaranteed = set()
    for entry in gv["container_images"]:
        ref = entry.get("pull") or entry.get("build")
        guaranteed.add(gv.get(ref.strip("{} \"'"), ref))
    for var in ("vllm_image", "webui_image", "caddy_image", "prometheus_image",
                "grafana_image", "node_exporter_image", "nvidia_exporter_image"):
        assert gv[var] in guaranteed, f"{var} is run but never pulled or built: {gv[var]}"


def test_head_only_images_are_not_pushed_to_workers():
    """Grafana and Open WebUI run only on the head; a worker holding them wastes disk
    on an image it can never run — the same reasoning as per-node model weights."""
    gv = _group_vars()
    placement = {e.get("pull", e.get("build")): e.get("hosts", "all")
                 for e in gv["container_images"]}
    for var in ("webui_image", "caddy_image", "prometheus_image", "grafana_image"):
        assert placement.get("{{ %s }}" % var) == "head", f"{var} should be head-only"
    for var in ("node_exporter_image", "nvidia_exporter_image"):
        assert placement.get("{{ %s }}" % var) == "all", f"{var} should be on all nodes"
    # `vllm_image` is a BUILT image, so it appears under its literal name rather than as
    # a `{{ var }}` reference. Any node may run any engine, so it must still be everywhere.
    assert placement.get(gv["vllm_image"]) == "all", "vllm_image should be on all nodes"


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


def test_the_model_mirror_excludes_huggingface_download_metadata():
    """`hf download` leaves `.cache/huggingface/` inside the model dir, and the newer CLI
    writes `trees/*.json` as **0600 owned by `vllm`**. The mirror's sender runs as
    `deploy` and cannot read it, so rsync aborts rc 23 — after moving 75 GiB, which is how
    it failed for NVIDIA-Nemotron-3-Super on 2026-08-10.

    It is also just not model data: resume metadata for an interrupted fetch, which vLLM
    never reads."""
    tasks = (Path(__file__).resolve().parent.parent / "ansible" / "roles" / "model" /
             "tasks" / "main.yml").read_text()
    mirror = tasks[tasks.index("Mirror the models this node runs from the head"):]
    cmd = mirror[:mirror.index("delegate_to")]
    assert "--exclude=.cache/" in cmd, "mirror would abort on 0600 hf metadata"


# --- the runbook trigger's deployment (ADR-0021) ----------------------------

def test_the_runbook_grant_is_a_single_command_entry_like_its_siblings():
    """A third bounded grant, and it has to stay the same SHAPE as the other two: one
    fixed program, no wildcards, no directory. `NOPASSWD: /usr/local/sbin/` or a command
    with arguments would turn the boundary back into a family of commands."""
    sudoers = (ROLES / "activate" / "templates" / "sudoers-activate.j2").read_text()
    grants = [line for line in sudoers.splitlines()
              if line.startswith("%") and "NOPASSWD" in line]
    assert len(grants) == 3
    for grant in grants:
        program = grant.split("NOPASSWD:", 1)[1].strip()
        assert program.startswith("{{") and program.endswith("}}"), program
        assert " " not in program.strip("{} "), f"{program} takes arguments"
    assert any("runbook_bin" in g for g in grants)


def test_the_deploy_asserts_geoffs_grants_include_the_new_one():
    """The assertion is EXHAUSTIVE by design — every passwordless grant geoff holds must
    be one of the bounded programs. Adding a program without adding it there would make
    the next deploy fail, which is the check working; forgetting to add the program to the
    list instead would silently widen what the assertion tolerates."""
    tasks = (ROLES / "activate" / "tasks" / "main.yml").read_text()
    bounded = tasks[tasks.index("_bounded:"):tasks.index("_bounded:") + 200]
    for var in ("activate_bin", "probe_bin", "runbook_bin"):
        assert var in bounded, f"{var} missing from the exhaustive grant allowlist"


def test_the_panel_is_told_where_the_trigger_and_its_log_live():
    """The panel hardcodes nothing; every path is env. A missing one would silently fall
    back to a default that happens to be right today and wrong after any rename."""
    unit = (ROLES / "control-panel" / "templates" / "control-panel.service.j2").read_text()
    for var in ("RUNBOOK_BIN", "RUNBOOK_UNIT", "RUNBOOK_DIR", "RUNBOOK_LOG_DIR"):
        assert f"Environment={var}=" in unit


def test_the_harness_is_installed_where_the_trigger_looks_for_it():
    """A detached run needs an interpreter that exists. If these two disagree the trigger
    refuses to start anything, which is safe and completely opaque."""
    import importlib.machinery
    import importlib.util

    group_vars = (Path(__file__).resolve().parent.parent / "ansible" / "group_vars" /
                  "all.yml").read_text()
    harness_bin = [line.split(":", 1)[1].strip() for line in group_vars.splitlines()
                   if line.startswith("harness_bin:")][0]
    trigger_path = ROLES / "activate" / "files" / "vllm-runbook"
    spec = importlib.util.spec_from_loader(
        "vllm_runbook_role",
        importlib.machinery.SourceFileLoader("vllm_runbook_role", str(trigger_path)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert str(module.HARNESS) == harness_bin


def test_prometheus_scrapes_the_panel_for_what_no_exporter_knows():
    """Which profile is activated, and whether a runbook is driving the cluster. Neither
    is discoverable from an exporter — vLLM's `model_name` label is the model-agnostic
    stable alias, and a runbook is not an engine — so the panel exports them. Over
    loopback: the prometheus container is `network_mode: host` and the panel binds
    127.0.0.1, so the scrape never meets Caddy's basic_auth."""
    conf = (ROLES / "prometheus" / "templates" / "prometheus.yml.j2").read_text()
    assert "job_name: cluster" in conf
    assert "127.0.0.1:{{ control_panel_port }}" in conf
    compose = (ROLES / "prometheus" / "templates" / "docker-compose.yml.j2").read_text()
    assert "network_mode: host" in compose, "the loopback scrape would not resolve"


def test_no_duplicate_keys_in_any_yaml():
    """A duplicate mapping key silently DISCARDS the earlier value (2026-08-12).

    Editing `roles/caddy/tasks/main.yml` to add a task, the replacement matched only as far
    as `dest:` — so the new task was inserted mid-task and the original's `mode`/`owner`/
    `group` were orphaned onto it, producing `mode:` twice. Ansible's own reaction is a
    WARNING it prints mid-run and then carries on ("Using last defined value only"), and
    `sparky lint` stayed green because `ansible-playbook --syntax-check` does not treat it
    as an error. So the only thing standing between that edit and a silently wrong file
    mode was somebody reading deploy output carefully.

    PyYAML's SafeLoader also accepts duplicates silently, hence the custom constructor:
    the check has to be built, it is not free anywhere in the stack.
    """
    import glob
    import yaml
    from yaml.constructor import ConstructorError

    class Strict(yaml.SafeLoader):
        pass

    def no_dupes(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dupes)

    root = Path(__file__).resolve().parent.parent
    files = (glob.glob(str(root / "ansible" / "**" / "*.yml"), recursive=True)
             + glob.glob(str(root / "runbooks" / "*.yml")))
    assert files, "expected to find ansible/runbook YAML to check"
    problems = []
    for f in files:
        try:
            list(yaml.load_all(open(f), Strict))
        except ConstructorError as exc:
            problems.append(f"{Path(f).relative_to(root)}: {exc.problem} at {exc.problem_mark}")
        except yaml.YAMLError:
            pass          # Jinja-templated YAML that does not parse standalone; not our concern
    assert not problems, "duplicate keys silently drop values:\n  " + "\n  ".join(problems)
