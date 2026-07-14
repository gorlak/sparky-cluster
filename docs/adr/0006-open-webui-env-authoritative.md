# ADR-0006: Open WebUI env-authoritative configuration

**Date:** 2026-05-24
**Status:** Accepted

## Context

Open WebUI stores its config in a SQLite database inside its data volume.
By default, anything changed in the Admin Panel persists across container
restarts. This conflicts with the goal of profile switching: when a deploy
changes which vLLM engines are serving, Open WebUI must be re-pointed at
the new endpoints automatically, with no manual Admin Panel clicks.

## Options considered

**A. Manual UI reconfiguration per profile switch**
Operator clicks through Admin Panel after every `make deploy PROFILE=<name>`.
Fragile, error-prone, and defeats the purpose of declarative profiles.

**B. API-driven config (Open WebUI admin API)**
Could POST updated connection settings via curl after deploy. Requires
reverse-engineering the internal API, is not officially documented, and may
break across Open WebUI versions.

**C. `ENABLE_PERSISTENT_CONFIG=false` (env-authoritative)**
Setting this env var causes Open WebUI to read its config from environment
variables on every startup, ignoring the SQLite-stored values. The compose
template asserts all config from the active profile on every deploy, so a
profile switch re-points Open WebUI automatically when the container
recreates.

## Decision

`ENABLE_PERSISTENT_CONFIG=false` (option C).

## Consequences

- Profile switches automatically reconfigure Open WebUI with no manual steps.
- The Admin Panel is effectively read-only for config: any setting changed
  there is reset on the next deploy (the container is recreated and config
  reloads from env). To change a setting durably, set its env var in
  `group_vars/all.yml` or the profile.
- User data (accounts, chats, uploaded files) is stored in the data volume,
  not in config, and is unaffected by container recreation.
- Gap: any Admin Panel setting with no corresponding env var cannot be set
  declaratively and won't persist if changed in the UI. This is a known
  shortcoming documented in the README.
- Sign-up flow: a from-scratch install must briefly re-enable
  `webui_enable_signup` to create the first admin account, then disable it.
  Subsequent deploys keep sign-up closed.
