---
name: development
description: Development workflow conventions for this repository. Read before making changes, committing, or pushing. Covers git commit ownership, staging, and collaboration style.
---

## Git: the user stages AND commits

**The user runs `git add`, `git commit` and `git push`.** Do not run any of them
unless explicitly asked. Leave changes in the working tree; they decide what
enters the index.

This changed on 2026-08-11. It used to be "prepare and stage, then hand off",
and the reason for tightening it is that **staging is where the review
happens**. An agent that stages has already made the include/exclude call, and
`git add -A` in particular sweeps up whatever else the session left lying
around. Handing over a pre-staged index invites approving a set nobody read.

When work is ready:
1. **Leave it unstaged.** Say what changed and why.
2. Suggest a commit message if asked.
3. Stop.

### When asked for a commit message, read the index first

**Do not describe the work you remember doing — describe what is actually
staged.** Those differ more often than they should: the user stages selectively,
a session may have touched files that were deliberately left out, and a long
session's memory of "what we did" drifts from the diff.

```bash
git status --short          # what is staged vs merely modified
git diff --cached --stat    # the shape of it
git diff --cached           # read it when the message makes claims
```

State the mismatch plainly if there is one — "the index has 12 files; the
retirement docs we discussed are not among them" — rather than writing a
message for work that is not there. A commit message that overstates its own
diff is worse than a terse one, because it is the record everyone trusts later.

## The user deploys; you take it from there

`deploy` is password-gated, so it is the one step that is **theirs** (ADR-0018). Everything
either side of it is **yours**, and the handover is where an agent most often stalls.

**"deploying now" / "deploy is going" is a cue to act, not to wait.** When you hear it:

1. **Watch for it to finish — success OR failure — and then READ THE LOG either way.** Do
   not go idle, do not ask "shall I proceed?", and do not hand the error back for the
   operator to paste. The next step was agreed when the change was written. But do not
   infer completion from artifacts either.

   ```bash
   tail -40 /opt/cluster/ansible.log          # log_path in ansible/ansible.cfg
   ```

   > **Do NOT watch the fleet lock to detect a deploy.** It is *free until the deploy takes
   > it*, so a watcher started the moment the operator says "deploying" exits immediately
   > with stale data and reports success for a deploy that never ran. That happened on
   > 2026-08-13. Poll `deployed_at` for a CHANGE instead.

   > **Never treat a side effect as a completion signal.** On 2026-08-11 a deploy was
   > declared "landed" because a model directory and an engine env file had appeared. Both
   > appear **mid-run** — `model` and `vllm` are roles 4 and 6 of 15 — and the deploy was
   > still going. The activation started on top of it collided with the last role
   > (`fleet-state`), which failed with *"another activation is in flight — refusing to
   > interleave."* The reconciler's mutual exclusion contained it and nothing was corrupted,
   > but the deploy died one task from the end.

   **The reliable signal is POSITIVE evidence from the deploy's LAST role.** `fleet-state`
   runs last and stamps `deployed_at` into `/opt/cluster/fleet.json`, so a `deployed_at`
   newer than the change you are waiting on proves the deploy ran *all the way through*
   after that change:

   ```bash
   python3 -c "import json;print(json.load(open('/opt/cluster/fleet.json'))['deployed_at'])"
   ```

   Compare it to the mtime of the profile or group_var you edited. Newer means done; older
   means still running, or it failed before the end.

   Then confirm the thing you actually care about landed — for a TP=2 flag change, on
   **both** ranks:
   ```bash
   ssh <worker> "grep -o 'VLLM_SERVE_ARGS=.*' /opt/vllm/engines/<engine>.env" | tr ' ' '\n' | grep -c -- --your-flag
   ```

   Weaker signals, and why: *a file appearing* proves nothing (`model` and `vllm` are roles
   4 and 6 of 15 — this is the mistake that caused the collision above). *The
   `ansible-playbook` process being gone* is better but still absence-of-evidence, and it
   can read as gone in the gap between plays; if you use it, check twice a few seconds
   apart. **A deploy and an activation must never overlap:** the deploy's `fleet-state`
   role drives the same reconciler `activate` does, and one of the two will lose.
2. **Verify the deploy did what it was for** — the profile is activatable, the flags
   rendered into the engine env file, the weights landed on both nodes. A deploy that
   half-succeeded is much cheaper to catch here than after an activation.

   **Read `skipped` and `changed` as results, not noise.** A task that skipped when it
   should have run (a `when:` that no longer matches, a host group that changed) is a
   silent no-op, and a task that changed when nothing should have changed means something
   is not converged. Both are findings on an otherwise green run.
3. **Take the next step immediately.** Usually that is `activate <profile>`, then read the
   startup log and the smoke gate. See [[operations]] for the how.
4. **Report the outcome**, not the intention.

If a deploy FAILS, diagnose it and fix the cause rather than handing the error back. A
failure whose fix is in the repo (a role that assumes an HF-layout checkpoint, a flag that
does not survive the env-file round trip) is ordinary work — do it, then say "re-run".

## Adversarially review your own hunks BEFORE asking for a deploy

A deploy costs the operator a password and a wait. Read your diff back as if someone else
wrote it and you are trying to find the flaw — *then* ask.

**The specific trap is scripted edits to YAML**, which produce **valid YAML that is
semantically wrong** — and neither `sparky lint` nor `ansible-playbook --syntax-check` can
see it, because both only prove the file parses. On 2026-08-13 three bugs shipped from this
one cause:

| bug | what the edit actually did |
|---|---|
| duplicate `mode:` | the insertion split an existing task, orphaning its keys onto the new one |
| a misplaced `loop:` | anchored on a `when:` line that appears in several tasks |
| `daemon_reload` in `vars:` | became a variable rather than a module parameter — a silent no-op, and the deploy then **failed** to start a unit whose file had changed |

So, after any scripted edit to a task file: **print the resulting task and read it.** Do not
trust that the replacement was right because the anchor string matched.

```bash
python3 -c "import yaml,sys; [print(t) for t in yaml.safe_load(open('ansible/roles/<r>/tasks/main.yml')) if 'thing' in str(t)]"
```

Worth checking in the same pass, because each has taken a deploy down: a **port** a new
listener binds is actually free (Caddy runs `network_mode: host`, so a clash costs the
whole web front end); every new `group_vars` key is both **defined and referenced** (a typo
renders empty, not an error); and config syntax that lint does not validate — Caddy's
`handle` takes exactly **one** matcher token, and `handle /a /b {` stops Caddy from
starting at all.

**Deploys are cheap; do not hoard them.** Batching several ready profiles into one deploy
is free and good. *Delaying* a change, parking a profile "until the next deploy", or
writing three paragraphs of justification to avoid a second one all trade a cheap thing for
an expensive one. Ship the best-supported guess and let the smoke gate be the test.

What stays expensive, and still deserves the care: **activations** — serialize them, one
variable at a time, and attend the first activation of anything in DEF-0004's
node-freeze territory — plus model downloads and anything that could take a node down.

## A bug found mid-feature: stash the feature, fix the bug, pop it back

**When a bug surfaces while a feature is in progress in the worktree: stash the feature,
fix the bug, commit the fix, then pop the feature back.**

```bash
git stash push -m "WIP <feature>"     # the feature, out of the way
# …fix the bug, run the tests, hand off for commit…
git stash pop                          # the feature returns to a clean base
```

The bug fix then lands as **its own commit against a clean tree**, which is the whole
point. Otherwise it arrives tangled with an unrelated half-finished feature, and the two
things that matter most about a fix are both lost: it cannot be read on its own, and it
cannot be reverted on its own. A fix buried in a feature commit is also a fix nobody finds
when they go looking for when the behaviour changed.

It has a second effect worth naming: the fix gets tested against the code that is actually
deployed, not against the feature's half-applied state. A fix that only works because an
in-progress refactor happens to be in the tree is not a fix.

This does not conflict with "big commits are fine" — that is about not prying *one*
shipped item into artificial pieces. A bug fix and an unrelated feature were never one
item.

## Suggested Commit Message Format

Follow the existing commit history style (concise imperative subject line, no
trailing period; clauses separated by semicolons). Check `git log --oneline -10`
before writing one, to match the tone and granularity of recent commits.

Bodies are rare in this log and should stay rare — but a large or
consequence-heavy commit earns one when the **findings** are not recoverable
from the diff (a measurement that changed a decision, a defect root-caused, a
claim in the docs corrected).

Example handoff:
```
Staged: 4 files (sparky/bench.py, sparky/store.py, tests/test_bench.py, docs/adr/0012-*.md).
Suggested message:

    Add benchmark regiment: multiturn quality check, SQLite storage, weekly timer
```

## What Belongs in a Commit

Each commit should correspond to one shipped item — a new feature, a new
profile, a new ADR, a bug fix, a role addition. Don't bundle unrelated
changes. If an ADR is written for a decision, commit the ADR in the same
commit as the implementation it documents.

## ADRs

Every significant architectural or operational decision shipped to the cluster
gets an ADR in `docs/adr/` — see [[documentation]] for when and how to write one.
Write the ADR alongside the implementation and commit both together.

## No Cleanup Commits

Don't create "cleanup", "fix typo", or "update comments" commits speculatively.
If a cleanup is needed as part of a real change, include it in that commit.
