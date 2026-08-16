"""measure — how good, how fast, how far? (ADR-0027)

The top tier: the outer loop (`suite`, `runner`, `suitectl`), the instruments (`bench`,
`evals`, `coding`, `soak`), the coding sandbox (`sandbox`, `reference`), and the record
(`store`, `report`, `scoreboard`, `tools`). This tier drives everything below it —
activating models, running sanity checks, reading the trend store.

May import `foundation`, `verify` and `serve`. Nothing imports it back: it sits at the top,
so a lower tier reaching up to it would invert the whole stack. Only the CLI, above every
tier, wires it to the operator.
"""
