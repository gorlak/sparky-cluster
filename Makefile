# Repo-root convenience.
#
#   make download REPO=<hf-repo> [DEST=<name>]   — stage a model into the inbox
#                                                  (uv provisions hf; no local install)
#   make test                                    — sparky harness unit tests (ADR-0010), via uv
#   make lint                                    — ansible syntax-check across profiles (ADR-0011 Layer 1)
#   make deploy PROFILE=… / check / teardown / status / logs-*   — delegated to ansible/
#
# `download` + `test` + `lint` run here (scripts/, sparky/, ansible/); everything else delegates.
.PHONY: download test lint
download:
	@uv run --script scripts/download.py $(REPO) $(DEST)

test:           ## sparky harness unit tests — no hardware (ADR-0010)
	@uv run pytest

lint:           ## ansible-playbook --syntax-check on site.yml (every profile) + teardown (ADR-0011 Layer 1)
	@cd ansible && set -e; \
	  for p in profiles/*.yml; do \
	    ansible-playbook site.yml -e @"$$p" --syntax-check >/dev/null; \
	  done; \
	  ansible-playbook teardown.yml --syntax-check >/dev/null; \
	  echo "lint OK — site.yml across $$(ls profiles/*.yml | wc -l) profiles + teardown.yml parse cleanly"

# Delegate every other target to ansible/Makefile — no target list to maintain.
%:
	@$(MAKE) -C ansible $* $(MAKEOVERRIDES)
