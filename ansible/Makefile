# Cluster automation — thin wrapper over ansible-playbook.
#
# Source of truth: this repo (~/Projects/DGX-Spark-Setup/ansible), under git.
# deploy/check/teardown first PUBLISH this source to the live location
# (/opt/cluster/ansible — what the deploy user reads), then run Ansible there as
# deploy. Publishing is automatic; it's not a step you run separately.
#
# Requires the `cluster` group (log in once after bootstrap-deploy.sh) so you can
# write /opt/cluster. Ansible runs as deploy via sudo (your password is the gate);
# a dashboard running as User=deploy invokes the same targets with no prompt.
#
#   make deploy   PROFILE=step-flash
#   make check    PROFILE=step-flash    # dry run (--check --diff)
#   make teardown                       # stop vLLM on both nodes
#   make status / make logs-head / make logs-worker

PROFILE       ?= step-flash
EXTRA         := -e @profiles/$(PROFILE).yml
LIVE          := /opt/cluster/ansible
DEPLOY_KEY    := /home/deploy/.ssh/id_ed25519
SNOOPY        := deploy@10.0.200.13

# Empty when already running as deploy (e.g. a dashboard); else `sudo -u deploy`.
RUN_AS_DEPLOY := $(shell [ "$$(id -un)" = deploy ] || echo sudo -u deploy)

.PHONY: publish deploy check teardown teardown-all ping status logs-head logs-worker

# Mirror repo source -> live deploy location. No-op when run from $(LIVE).
# -rlc (not -a): copy content by checksum but DON'T preserve owner/group/perms —
# the live tree is deploy-owned and you can't chgrp files you don't own. Access is
# handled by the cluster group (setgid dir + default ACL g:cluster:rwx).
publish:
	rsync -rlc --delete --no-perms --no-owner --no-group \
	    --exclude='.git' --exclude='__pycache__' --exclude='*.retry' --exclude='.ansible' \
	    ./ $(LIVE)/

deploy: publish
	cd $(LIVE) && $(RUN_AS_DEPLOY) ansible-playbook site.yml $(EXTRA)

check: publish
	cd $(LIVE) && $(RUN_AS_DEPLOY) ansible-playbook site.yml $(EXTRA) --check --diff

teardown: publish
	cd $(LIVE) && $(RUN_AS_DEPLOY) ansible-playbook teardown.yml $(EXTRA)

# Also stops Open WebUI (normally left running)
teardown-all: publish
	cd $(LIVE) && $(RUN_AS_DEPLOY) ansible-playbook teardown.yml $(EXTRA) --tags all,webui

ping:
	cd $(LIVE) && $(RUN_AS_DEPLOY) ansible all -m ping

status:
	cd $(LIVE) && $(RUN_AS_DEPLOY) ansible all -m shell \
	    -a 'systemctl --no-pager --lines=0 status vllm.service vllm-worker.service 2>/dev/null; true'

logs-head:
	sudo journalctl -u vllm.service -f

logs-worker:
	$(RUN_AS_DEPLOY) ssh -i $(DEPLOY_KEY) $(SNOOPY) 'sudo journalctl -u vllm-worker.service -f'
