"""foundation — the bottom tier: depends on nothing else in `sparky` (ADR-0027).

`api` (the vLLM HTTP client), `topology` (profiles + `current-topology.json`), `scope`
(the CLI's privilege vocabulary), `fleetlock` (the deploy↔run mutex). Every other tier may
import these; they import none of the others. The import-direction test enforces it, so a
foundation module reaching *up* — to `serve`, `measure`, the CLI — fails the suite rather
than quietly inverting the dependency that makes this the stable base.
"""
