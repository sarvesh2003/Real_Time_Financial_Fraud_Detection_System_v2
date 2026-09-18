# Human calibration, packet 1 of 2: investigation quality

**Complete this packet before packet 2, and before looking at any judge
output.** Packet 2 tells you which fault was actually injected. Once you know
that, you cannot label investigation quality the way the trace judge does,
because the trace judge is never told. Reading them out of order silently
destroys this comparison, and it cannot be undone.

You are NOT being asked whether the agent's conclusion was right. You are
being asked whether the conclusion was earned by the evidence recorded below.

For each run, write three labels into `human_calibration.csv`:

| column | question | labels |
| --- | --- | --- |
| `human_evidence_support` | Does the recorded evidence support the claim asserted? Base this only on the recorded tool output. An assertion in the summary that no recorded output backs counts for nothing. | PASS / PARTIAL / FAIL |
| `human_causal_reasoning` | Does the rationale connect the observations to the stated cause without unsupported leaps? | PASS / PARTIAL / FAIL |
| `human_investigation_quality` | Did the agent gather evidence relevant and sufficient for the strength of the claim it made? | PASS / PARTIAL / FAIL |

Notes on investigation quality, so your labels and the judge's mean the same
thing: there is no single correct investigation path, trace length and tool
call count are not merits, and an agent that correctly concluded it could not
tell -- having genuinely looked -- has not failed at investigating.

Leave any cell blank if you do not want to commit to a label. Blank is
excluded from the agreement figures explicitly; it is never read as a label.


Runs to label: 0
