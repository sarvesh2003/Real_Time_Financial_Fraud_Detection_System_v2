"""
A portable validity gate for agent evaluations.

This package answers one question and refuses to answer any other: is this
evaluation measuring what it claims to measure? It says nothing about whether
the agent is good. It exists to gate publication of a number, because the
expensive mistake in agent evaluation is not a low score, it is a high score
that turns out to have been measuring the prompt, the grader, or two different
experiments averaged together.

Layout, and why it is split this way:

    manifest.py   the normalized contract. Every check reads this and only
                  this. It is the thing another harness has to produce.
    adapter.py    the ONLY file that knows about this repository. Swapping it
                  is the entire integration surface for a different harness.
    matching.py   the phrase matcher, kept in lockstep with the grader under
                  test so a check can never pass on a phrase the grader
                  would have accepted.
    registry.py   check registration and execution. Adding a check is one
                  decorated function.
    checks/       one module per failure class. Nothing in here may name a
                  model, an environment, a fault, or a threshold pulled out
                  of the air. A check states an invariant and derives its
                  comparison points from the manifest.
    run.py        entrypoint. Prints a report, writes eval/validity_report.md,
                  exits non-zero when an invariant is violated.
"""
