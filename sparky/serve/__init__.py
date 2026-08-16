"""serve — get a model running, or converge the fleet (ADR-0027).

`fleet` (the allowlist and what it implies per node), `activate` (the request + the
reconciler trigger, and the smoke gate it runs on the way up), `ansible` (the `deploy`
engine). This is the tier that CHANGES the cluster.

May import `foundation` and `verify` — activation runs the sanity checks as it brings a
model up. May NOT import `measure`: serving must not depend on the thing that measures it,
or a measurement bug could take activation down with it.
"""
