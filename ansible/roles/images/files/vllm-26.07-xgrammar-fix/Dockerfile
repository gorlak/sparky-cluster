# WAR (2026-08-08) for DEF-0010 (docs/defects.md): NVIDIA's nvcr.io/nvidia/vllm:26.07-py3
# ships **xgrammar 0.2.0** while the vLLM 0.24.0 it also ships **requires
# `xgrammar >= 0.2.1`** (vllm requirements/common.txt). This is not a loose-floor
# mismatch — the image is below vLLM's own declared minimum, and a `pip check` inside it
# would say so.
#
# The symptom: vllm/tool_parsers/structural_tag_registry.py does
#   from xgrammar import StructuralTag, normalize_tool_choice
# and `normalize_tool_choice` does not exist in 0.2.0, so EVERY tool-calling request
# raises ImportError and returns HTTP 500. Measured blast radius:
#   tool_choice: "none"     -> 200
#   tool_choice: "auto"     -> 500   <- what Open WebUI sends on ORDINARY chats
#   tool_choice: "required" -> 500
# So this breaks normal chat in the UI, not merely tool workflows. Plain completions
# without a `tools` array are unaffected, which is why the engine looks healthy.
#
# This image satisfies vLLM's own requirement so the 26.07 profiles can serve tools.
#
# REMOVE WHEN: NVIDIA ships a 26.07+ image whose xgrammar has `normalize_tool_choice`
# (i.e. >= 0.2.1). Then repoint the profiles at the stock digest and drop this entry
# from container_images.
#
# It replaces `vllm-26.06-fastapi-fix`, whose defect (DEF-0005, fastapi 0.137) 26.07
# fixes. Worth noting the pattern rather than the individual bug: two consecutive NGC
# images have each shipped an internally inconsistent dependency pair, so the derived
# image is not a transient embarrassment — it is the mechanism that lets us stay on the
# newest container (ADR-0013), and what it patches will keep changing.
#
# --no-deps IS LOAD-BEARING. The first attempt (2026-08-08) used a plain
# `pip install 'xgrammar>=0.2.1'` and broke the image: xgrammar depends on
# `transformers`, so pip re-resolved it and installed **transformers v4**, which vLLM
# 0.24.0 removed support for. Every engine then died at import with
#   ImportError: Support for Transformers v4 is deprecated and was removed in vLLM v0.24.0
# A vendor image is a carefully-resolved dependency set; patching one package must be
# SURGICAL or pip will happily "fix" the rest of it. If a future patch genuinely needs a
# newer dependency, add that dependency explicitly and pinned — never by letting the
# resolver loose inside someone else's image.
#
# BUILT BY: the `images` role on every node, from this Dockerfile. FROM is pinned to the
# same 26.07-py3 digest the role pulls as a base, so the build resolves locally with no
# registry round-trip and is byte-reproducible. Bump both together when 26.07 moves.
# The two asserts ARE the acceptance test, and they run at build time so a WAR that
# stops working fails the DEPLOY rather than every engine minutes later. Versions come
# from importlib.metadata, not `pkg.__version__` — xgrammar exposes no `__version__`, and
# reaching for one cost a whole deploy cycle on 2026-08-08 when the patch itself was fine.
FROM nvcr.io/nvidia/vllm@sha256:95c498a475142c20c989c65e5d223348c09fed83ba17ddf44f117610c0bd3268
# Ownership mark. `docker image prune` is otherwise DAEMON-WIDE and would reclaim
# dangling layers belonging to anyone else using this box; the images role prunes
# with `--filter label=net.flummoxed.sparky-cluster=true` so only orphans of OUR builds
# qualify. Pulled images cannot carry this, which is why the tagged-image reclaim
# scopes by repository instead.
LABEL net.flummoxed.sparky-cluster="true"
RUN pip install --no-cache-dir --no-deps 'xgrammar>=0.2.1' \
 && python3 -c "import xgrammar; from importlib.metadata import version; \
    assert hasattr(xgrammar, 'normalize_tool_choice'), 'xgrammar still lacks normalize_tool_choice'; \
    assert version('transformers').startswith('5'), 'transformers got downgraded to ' + version('transformers'); \
    print('WAR ok: xgrammar', version('xgrammar'), 'transformers', version('transformers'))"
