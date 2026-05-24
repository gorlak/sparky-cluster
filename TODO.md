# Cleanup / deferred work

Low-priority tech-debt and polish. Not a roadmap — "maybe someday, maybe not."
Feature/roadmap items live in the README's *Future Work* section instead.

---

## Make node identity fully inventory-driven

Today the cluster is *mostly* config-driven: playbooks target the `head`/`worker`
groups and roles/templates pull per-host details (`vllm_host_ip`, `node_rank`)
from `ansible/inventory.yml`. But a few identity/IP/domain values are hardcoded
**outside** the inventory, so adapting the repo to a differently-named cluster
means editing several spots instead of just the inventory:

- `ansible/group_vars/all.yml` — `master_addr: 10.0.200.12` **duplicates** the
  head's IP; `web_domain: sparky.flummoxed.net` bakes in the head's domain.
- `ansible/Makefile` — `SNOOPY := deploy@10.0.200.13` (worker IP, used by
  `logs-worker`).
- `ansible/bootstrap-deploy.sh` — worker IP `10.0.200.13` and a
  `hostname == "sparky"` control-node assumption.

**Possible fix:**
- Derive `master_addr` from the head host:
  `master_addr: "{{ hostvars[groups['head'][0]].vllm_host_ip }}"` (removes the
  duplication outright).
- Keep `web_domain` as a single, clearly-labeled config knob.
- `Makefile` / `bootstrap-deploy.sh` are shell (can't easily read inventory) —
  leave each as a single top-of-file variable and add a short README
  "adapt to your cluster" section listing exactly what to change.

**Why:** makes the repo genuinely clone-and-edit-one-place for others with
similar hardware but different hostnames/IPs.
