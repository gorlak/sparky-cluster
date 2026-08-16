"""measure/loop — the outer loop that RUNS a suite (ADR-0016, ADR-0021).

`suite` (naming, discovery, validation — what may be run), `runner` (the serial
activate→measure→resume loop and its exclusion/failure policy), `suitectl` (the trigger
that detaches a run into a systemd unit of its own). Orchestration only: every regiment it
drives is injected, so the loop owns order and resumption, never what a good measurement is.
"""
