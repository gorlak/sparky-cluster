# ADR-0019: A bounded image probe — answering container questions without a `docker` grant

**Date:** 2026-08-08
**Status:** Accepted (implemented 2026-08-08)

## Context

ADR-0018 split the system into `deploy` (privileged, human, password-gated) and
`activate` (unprivileged, agent-drivable), and retired geoff's `NOPASSWD` grants —
including `docker`, on the grounds that a `docker` grant *is* a root grant
(`docker run -v /:/host`). That boundary has held and is worth keeping.

It left one job on the wrong side of the line. **Evaluating a model means asking
questions of the container**, and every one of them needs `docker`:

- does this vLLM build know `Mistral3ForConditionalGeneration`?
- what NCCL does `26.07-py3` actually ship?
- is `xgrammar` above the floor vLLM itself declares?
- does `Step3VLProcessor` carry `_get_num_multimodal_tokens` yet (DEF-0006)?

These are cheap, read-only, and *frequent* — the `model-evaluation` and
`version-discovery` skills both open with them, because their entire economy is
"failures here are cheap, failures after a full deploy are expensive." In practice
each one became a request for geoff to paste a `sudo docker run …` line. Over the
2026-08 sessions that became the single most common non-deploy interruption, and it
inverted the cost gradient the checklists depend on: a probe that should cost seconds
cost a human round-trip, so the tempting shortcut was to skip it and let a failed
activation be the test. On 2026-08-08 that is exactly what happened — the
`mistral-medium-3.5-128b-nvfp4` profile was written against an **unverified** architecture
because verifying it was more expensive than trying it.

Two non-answers were considered first, because they are the obvious ones:

- **Give the agent a sudo'd shell, via a PID or a FIFO** (`sudo sh -c 'while read c;
  do eval "$c"; done < /tmp/fifo'`). This works, and it is precisely the hole ADR-0018
  closed, in a worse form: no validation, and `sudo` logs *one* invocation regardless
  of how many commands are piped through, so the audit trail goes dark. A boundary a
  pipe walks around is theatre, the same way one a `docker run` walks around is.
- **Put the agent in the `docker` group.** Root-equivalent by construction; ADR-0018
  explicitly refused this for the `activator` identity.

## Decision

**Add a second bounded grant, of the same shape as the reconciler, restricted to
read-only introspection of images this cluster has already deployed.**

`/usr/local/sbin/vllm-probe`, root-owned, reachable through a single-command sudoers
entry for the `activate` group. It is *not* a docker wrapper: no caller-supplied part
of the command line reaches `docker` uninterpreted.

| Input | Constraint |
|---|---|
| **image** | must appear verbatim in `/opt/vllm/images/allowlist` — root-owned, written per-node by the `images` role each deploy |
| **probe** | a key into a fixed dict of Python programs inside the script. There is no "run this code" mode |
| **arguments** | `^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$`, at most 24, passed as **argv** to the probe program — never interpolated into its source |
| **docker flags** | constants: `--rm --network none --cap-drop ALL --security-opt no-new-privileges --entrypoint python3`. No bind mounts, no `--gpus`, no `--privileged` |

Five probes ship: `versions`, `archs`, `pip`, `attr`, `quant`. Each is the generalised
form of a question this cluster has actually had to answer, and each maps to a defect
or a checklist step rather than to a hypothetical.

The decision function `validate()` is pure, so the containment claim is unit-tested
rather than asserted in a comment — the same reasoning that made the reconciler's
`plan()` pure (ADR-0011 Layer 3). The tests that matter are negative: an undeployed
image, an unknown probe, a missing allowlist (fails **closed**), and a battery of
arguments shaped like docker flags, paths, and shell metacharacters.

## Consequences

**The agent can evaluate a model end to end without a human.** Sizing, `config.json`,
memory math and container capability all become self-service; the operator is needed
only for `deploy`, which is the thing that genuinely changes the machine. That is the
autonomy ADR-0016's sweep runner assumes and did not previously have.

**"Probing something new is a deploy."** An image absent from `container_images` cannot
be probed, which keeps provisioning firmly on geoff's side — at the cost of one round
trip when evaluating a container we have never pulled. That cost is accepted: it is the
same shape as the profile allowlist, and it is the property that makes the grant
bounded rather than general.

**A second grant is a real widening, and is treated as one.** The deploy's assertion
that geoff holds no unexpected passwordless sudo now allowlists *two* programs by name,
exhaustively; a third appearing — however reasonable it looked — still fails the deploy.
The residual risk is running a vendor image we already run *with* GPUs and host cgroups,
in a strictly weaker sandbox: no network, no devices, no filesystem, no capabilities. It
is a smaller hole than the engines themselves and enormously smaller than the
`sudo docker` line it replaces.

**What it deliberately cannot do:** load a model, allocate GPU memory, reach the
network, write anything, or run a container that never went through a deploy. Anything
needing those is provisioning, and provisioning is `deploy`.

## Alternatives rejected

- **Widen `activate` to take a docker subcommand.** Fails the ADR-0018 test — the
  invocable thing must do exactly one thing.
- **Run probes as an unprivileged `docker` user.** No such thing; docker group access
  is root.
- **A long-lived probe daemon.** More surface, more state, and the questions are
  naturally oneshot. The reconciler's "oneshot script, not a daemon" reasoning applies
  unchanged.
- **Ship probe programs as files the caller names.** A path argument is a way to
  execute chosen code; the fixed dict is the whole point.
