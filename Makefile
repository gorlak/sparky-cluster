# Repo-root convenience.
#
#   make download REPO=<hf-repo> [DEST=<name>]   — stage a model into the inbox
#                                                  (uv provisions hf; no local install)
#   make deploy PROFILE=… / check / teardown / status / logs-*   — delegated to ansible/
#
# `download` runs here (the script lives in scripts/); everything else delegates.
.PHONY: download
download:
	@uv run --script scripts/download.py $(REPO) $(DEST)

# Delegate every other target to ansible/Makefile — no target list to maintain.
%:
	@$(MAKE) -C ansible $* $(MAKEOVERRIDES)
