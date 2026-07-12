# Repo-root convenience.
#
#   make download REPO=<hf-repo> [DEST=<name>]   — stage a model into the inbox
#                                                  (uv provisions hf; no local install)
#   make test                                    — sparky harness unit tests (ADR-0010), via uv
#   make deploy PROFILE=… / check / teardown / status / logs-*   — delegated to ansible/
#
# `download` + `test` run here (scripts/, sparky/); everything else delegates.
.PHONY: download test
download:
	@uv run --script scripts/download.py $(REPO) $(DEST)

test:           ## sparky harness unit tests — no hardware (ADR-0010)
	@uv run pytest

# Delegate every other target to ansible/Makefile — no target list to maintain.
%:
	@$(MAKE) -C ansible $* $(MAKEOVERRIDES)
