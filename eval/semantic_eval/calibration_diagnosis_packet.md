# Human calibration, packet 2 of 2: diagnosis correctness

**Only open this after packet 1 is filled in.** This packet reveals the
injected fault. That is necessary here and disqualifying there.

For each run, write one label into `human_calibration.csv`, column
`human_diagnosis_label`:

| label | meaning |
| --- | --- |
| `CORRECT` | Identifies the injected mechanism and a materially correct affected target. Wording need not match; a target named only as vaguely as the evidence allowed still counts. |
| `PARTIAL` | Identifies a meaningful part of the right subsystem or mechanism but misses an important qualifier, target, or causal step. |
| `INCORRECT` | Attributes the incident to the wrong primary cause, or asserts a materially false root cause. |
| `ABSTAIN` | Explicitly declines to name a root cause, or reports insufficient evidence, without asserting a competing diagnosis. |

Judge only the claim against the injected fault. Do not reward or penalise
wording, length, or how confident it sounds -- the agent's confidence is
deliberately not shown to you, because it is crossed with these labels later
and would make that table circular.

Leave the cell blank rather than guessing. Blank is excluded explicitly.


Runs to label: 0
