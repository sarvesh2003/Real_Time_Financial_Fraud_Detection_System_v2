---
name: grade-fault-investigation
description: Semantically judge whether an incident-investigation agent (or a Claude Desktop transcript) correctly diagnosed an injected fault in this fraud-detection eval project, using verified ground truth rather than keyword matching. Use when asked to grade, judge, or evaluate fault investigation quality, produce an LLM-judge report, or sanity-check eval/grade.py's mechanical scores. Takes one or more fault numbers as args.
---

# Grade Fault Investigation

Independent semantic second opinion alongside `eval/grade.py`'s mechanical
phrase-matching score. grade.py checks whether specific phrases from
`eval/rubric.yaml` appear in a run's summary/reasoning text — fast and
reproducible, but it has real, observed failure modes: a bare disqualifier
phrase can zero out a correct answer that mentions the word in passing, and
a phrase can mechanically match while the underlying causal claim is wrong
(a run can correctly use the word "restart" while blaming the wrong cause
entirely). This skill judges semantic correctness directly instead.

## What to do

Given one or more fault numbers as args:

1. For each fault number, read every `eval/runs/fault<NN>/*.result.json`
   file (zero-padded to 2 digits, e.g. `fault06`, `fault09`, `fault13`).
2. For any run whose `env_label` starts with `claude-desktop-`, also read
   the matching `eval/runs/fault<NN>/claude_desktop_transcript_<model>.md`
   file if it exists — it has more signal than the distilled `summary`
   field alone.
3. Look up the fault's real, verified mechanism in the Ground Truth table
   below. Do not substitute `eval/rubric.yaml`'s phrase lists for this —
   they exist for the mechanical grader, not for your judgment. If a fault
   number isn't in the table, read its actual injection script under
   `fault-injection/<family>/<NN>_<name>.sh` yourself before judging it —
   never guess at the mechanism.
4. For each run, judge:
   - **verdict**: `correct` / `partial` / `incorrect` — does it actually
     name the true mechanism and target, not just use an adjacent word?
   - **confidence_calibration**: `well-calibrated` / `overconfident` /
     `underconfident` / `n/a` — compare `model_self_reported_confidence`
     (and `confidence`, which reflects any code-level downgrade already
     applied) against how well-supported the conclusion actually was.
   - **insight**: 1-2 plain-language sentences a human skimming a report
     would want — what it got right/wrong and why, not a restatement of
     the verdict.
5. Write (or overwrite) `eval/llm_judge_report.md`: a top-level heading, a
   one-paragraph explanation of what this is (same framing as above), then
   one `## Fault <N>` section per fault with one bullet per run:
   `- **<env_label>** - verdict: \`<verdict>\` (<verdict_reason>) | calibration: \`<confidence_calibration>\` (<calibration_reason>)` followed by an indented insight line.
6. Also write (or overwrite) `eval/llm_judge_results.csv` with columns
   `fault_id,env_label,verdict,verdict_reason,confidence_calibration,calibration_reason,insight`
   — one row per run, so the output is structurally identical whether it
   came from this skill or from `eval/llm_judge.py` (the API-based version
   of the same judgment, for automated/repeatable runs).
7. Do NOT touch `eval/results.csv`, `eval/summary.md`, or `eval/grade.py`'s
   output — this is a second opinion, not a replacement. Do NOT edit any
   `result.json` file.

## Ground truth (verified against each fault's actual injection script)

- **Fault 6 — kafka/rebalance_churn**: restarts `go-enricher` (all 3
  replicas) 6 times, 5 seconds apart, forcing Kafka to repeatedly rebalance
  partition assignment instead of settling. Correct: names go-enricher
  restarts / consumer-group rebalance churn. A confident claim of an
  *organic* problem (memory leak, crash bug) instead of a deliberate
  repeated-restart pattern is WRONG even if it correctly notices
  go-enricher was restarting.
- **Fault 9 — network/latency_injection**: adds a 200ms `tc netem` delay to
  `go-server`'s `eth0`. No error lines are produced anywhere — the only
  signal is a shift in latency/duration metrics and the resulting
  throughput drop. Redis and CPU are both healthy and unaffected. Correct:
  names go-server (or its network link) and network-level latency/delay.
  Blaming Redis, CPU throttling, or leaving the affected service unnamed
  are wrong or incomplete.
- **Fault 13 — config/client_timeout_too_low**: sets `go-server`'s Redis
  client env vars (`REDIS_DIAL_TIMEOUT_MS`, `REDIS_READ_TIMEOUT_MS`,
  `REDIS_WRITE_TIMEOUT_MS`) to 1ms and recreates the container. Redis
  itself is completely healthy — it's purely a client-side timeout on
  go-server. Correct: names go-server's Redis client timeout config.
  Blaming Redis itself, a server-side connection limit, or a retry/backoff
  policy are all wrong.

For any other fault number, read `eval/rubric.yaml`'s entry for that
`fault_id` for orientation (family, script path) and then read the actual
script in `fault-injection/` before judging — never grade against the
rubric's phrase lists as if they were the ground truth themselves.
