"""verify — did serving come up right? (ADR-0027)

The sanity checks `activate` runs in-process at the end of every activation: `text_sanity`
(a benign multiturn conversation comes back coherent), `vision_sanity` (a model claiming
vision can see an image), and `smoke`, which aggregates them plus the tool-call shape into
the one pass/fail the activation gates on. These ask "is the output garbage", never "how
good is this model" — that is `measure`, a tier above.

May import `foundation`. May NOT import `serve` or `measure`: a check that came up depending
on the runner or the deploy engine would be a check that cannot run before them.
"""
