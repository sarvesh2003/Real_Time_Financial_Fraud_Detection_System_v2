# Plan: harden-agent-and-eval branch

Read this before touching any file. It is the single source of truth for
this branch's remaining work. Do not deviate from the scope below without
checking back with the user first — this branch is deliberately narrow.

## Context

Branch `harden-agent-and-eval`, created off `main` commit `5829c9f`. The
project is an evidence-driven incident investigator: a LangGraph agent
(`agent/graph.py`) that calls read-only MCP tools (`mcp-server/server.py`)
against a live Docker fraud-detection pipeline to diagnose injected faults,
plus an eval harness (`eval/`) that scores its answers against a rubric.

20 rows have already been graded across faults 1-4 (all `redis` family).
Those results are frozen in `eval/results.csv` / `eval/summary.md` on `main`
and must not be altered or re-run under different code without being clearly
distinguished from new data.

Already done on this branch:
- Removed all non-Kaggle LLM provider paths (NVIDIA, Groq, local-Tailscale
  Ollama) from `agent/run.py` and `eval/run_fault_eval.sh`. The only
  supported path now is an Ollama-compatible endpoint (Kaggle T4x2 GPU
  notebook via ngrok), model `qwen2.5:32b-instruct`.
- `agent/run.py` now fails loudly (`RuntimeError`) if `OLLAMA_BASE_URL` is
  unset, instead of silently defaulting to an untested provider.
- Claude Desktop roster for new fault runs trimmed to 2 tiers: Sonnet 5 Max,
  Haiku 4.5 Extended (Opus 5 dropped going forward).
- All comments and non-functional docstrings stripped from `agent/graph.py`,
  `agent/run.py`, `mcp-server/server.py`, `mcp-server/test_client.py`. Code
  must read as self-explanatory through naming alone from here on.
  **Exception, do not remove these on any future edit**: the `@mcp.tool()`
  docstrings in `mcp-server/server.py` and the `Field(description=...)` /
  class-docstring text in `agent/graph.py`'s `ConcludeOut`/
  `ConcludeInvestigation` schemas. These are not comments — FastMCP and
  LangChain serialize them into the tool schema sent to the LLM at runtime.
  Deleting them would change what the model sees and degrade real behavior,
  not just cosmetics.

## Remaining work, in order

### 1. New fault-family eval runs (blocked on the user's Kaggle setup)

Run 1 fault from each of the 3 untested families, using
`eval/run_fault_eval.sh`:
- Fault 6 (`kafka/rebalance_churn`)
- Fault 9 (`network/latency_injection`)
- Fault 13 (`config/client_timeout_too_low`)

Each fault produces 2 automated legs (`kaggle-qwen32b`, retrieval on/off)
plus 2 manual Claude Desktop legs (Sonnet 5 Max, Haiku 4.5 Extended) that
the user runs by hand and hand-transcribes into
`eval/claude_desktop_template.json`-derived result files.

Do the coding fixes below (#2, #3, #4) **before** running these faults, so
the new data already reflects fixed behavior rather than being invalidated
by a fix applied mid-sweep.

### 2. Budget-exhaustion downgrade in `agent/graph.py`

Currently `conclude()` only downgrades `confirmed` -> `best_guess` via the
corroboration check (evidence must span >=2 distinct tool names). There is
no equivalent check for budget exhaustion: a run that uses all 15 tool
calls and self-reports `confirmed` is allowed to stay `confirmed` even
though it ran out of investigative room. This already produced one real,
observed wrong-but-confirmed outcome in the existing 20-row dataset
(`fault4_kaggle-qwen14b_ragon`, used 15/15 calls, self-reported confirmed,
scored 0).

Fix: in `conclude()`, alongside the existing corroboration-threshold check,
add: if `state["calls_used"] >= TOOL_CALL_BUDGET` and the model
self-reported `confirmed`, downgrade to `best_guess`. Record which rule (if
either) fired — add a `downgrade_reason` field to the conclusion dict
(`None`, `"corroboration"`, or `"budget_exhausted"`; if both would fire,
record `"budget_exhausted"` since it's the stronger disqualifier).

### 3. Fault-16 rubric disqualifier fix in `eval/rubric.yaml`

Fault 16's `wrong_node` disqualifier is still the bare list
`["redis2", "redis3"]` — the same false-positive pattern already fixed for
faults 1 and 2, where a correct answer naming a healthy peer node for
comparison would incorrectly trigger the disqualifier. Fault 16 hasn't been
run yet, so this hasn't caused a scoring error yet, but will if run as-is
and left unfixed. Apply the same fix pattern used for faults 1/2 (check
`eval/rubric.yaml`'s fault 1 and 2 entries for the corrected disqualifier
shape before editing fault 16's).

### 4. Freeze `eval/` in git with an honest note

`eval/rubric.yaml` and `eval/cost_matrix.yaml` both have header comments
claiming the ground truth / cost matrix were written and committed *before*
any run happened. That's not true in git history — `eval/` was untracked
until this branch's parent commit on `main`, well after faults 1-4 had
already run. Either:
- Update those header comments to state plainly that version-control
  tracking begins at commit `5829c9f`, after faults 1-4, and don't use the
  word "pre-registered" anywhere in the README this project produces, or
- If the user wants to preserve the "pre-registered" framing for the *new*
  faults (6, 9, 13) specifically, that's legitimate as long as it's scoped
  explicitly to those 3 faults and not applied retroactively to faults 1-4.

Confirm which framing the user wants before writing the README (#6).

### 5. Re-grade and re-aggregate

After #2-#4 land and the new fault runs (#1) complete, run `eval/grade.py`
then `eval/aggregate.py` to fold the new rows into `eval/summary.md`. Do
NOT touch the scoring logic for the existing 20 rows beyond what #3
requires (fault 16 isn't among them, so this should be a no-op for faults
1-4's already-graded rows — verify that's actually true before treating it
as safe).

### 6. README

Structure, once #1-#5 are done:
1. What it is (one paragraph).
2. Invariants table: rule / where enforced in code / evidence it fires
   (e.g. corroboration downgrade — `agent/graph.py` `conclude()` — fired
   once in 20 graded rows; budget-exhaustion downgrade — same function,
   once #2 lands — closes the fault4_kaggle-qwen14b_ragon gap).
3. Results, with `n` on every row, split cleanly between the original
   20-row Redis-only dataset and the new cross-family rows from #1.
4. Threats to validity, stated plainly, not apologetically: small n (2-4
   per condition), original 20 rows are 100% Redis-family, Claude
   Desktop's Sonnet legs used 16/23 tool calls with no budget cap (not
   comparable to the budgeted automated arm), `config` family has 0
   indexed retrieval traces (retrieval structurally unsupported there),
   cross-fault contamination between back-to-back runs on one shared
   live stack is only prompt-mitigated, not a real stack reset.
5. The existing self-correction section from the archived
   `trash/What I have now.md` §12 (the "first draft flattered itself in 6
   ways, here's what an adversarial re-check caught" writeup) — reproduce
   it verbatim, it's the strongest single artifact in the project.
6. What's not built / explicitly out of scope.

## Diagnosed during fault 9 execution (2026-09-11) — do not fix mid-sweep

Both automated `kaggle-qwen32b` legs for fault 9 (network/latency_injection)
concluded `inconclusive` after only 1 investigative tool call. A background
diagnostic agent read both trace files and found a real, reproducible bug,
not just a model-capability gap:

`agent/graph.py`'s `investigate()` only retries the LLM call when
`finish_reason == "length"` (truncated output). It does NOT retry when the
model returns prose with no tool call at all and a normal `finish_reason`
(e.g. `"stop"`) — that case is silently treated as "the model chose to
conclude," and the model's actual reasoning text at that point is never
logged. This is what happened on the `rag=on` leg (208 output tokens
generated, no tool call, silently treated as a decision to stop). The
`rag=off` leg's single extra call was a separate, unrelated infra glitch
(`finish_reason: null`, 0 tokens — an empty response from the Kaggle-hosted
Ollama backend), not a prompting or logic issue.

Also relevant if this fault family is revisited later: neither run ever
tried a latency/duration/percentile metric or `list_metric_names` before
concluding — the one extra tool call each run made was weak (`query_logs`
on a fault engineered to produce zero log lines; a near-duplicate
`list_recent_changes`). Retrieval did surface the correct
`network/latency_injection` corpus entry at high similarity, but only on
the round after the model had already quit, and was correctly treated as
advisory-only per the prompt, so it was neutral, not the cause.

**Decision (user, 2026-09-11): do not patch this mid-sweep.** Faults 6 and
13 will run under the exact same `investigate()` behavior fault 9 already
ran under, so all 3 new faults stay comparable to each other and to their
own already-collected data. This is a real, diagnosed, disclosed limitation
for the README's threats-to-validity section (#6.4) — it is NOT something
to silently fix later without re-running fault 9 too. If a future session
does fix it, fault 9's existing `kaggle-qwen32b` rows must be re-run or
clearly labeled pre-fix, not silently treated as comparable to post-fix
data from other faults.

## Diagnosed during fault 6 execution (2026-09-11) — real cross-attempt contamination, caught live

Fault 6 (kafka/rebalance_churn, transient) was injected 4 times this session
while debugging the Kaggle tunnel/crash issues (02:54:05Z, 03:03:35Z, an
unintended ~03:23:23Z run, and a final clean 03:27:35Z run). The changelog
was rotated before each attempt so `list_recent_changes` only ever showed
the latest one — but Prometheus metrics and container restart history are
NOT reset by a changelog rotation, so they retained all 4 attempts.

Claude Desktop Sonnet 5's manual run against the clean 03:27:35Z injection
queried a wide metrics window, correctly noticed the restart-cadence change
starting at 02:54 UTC (the FIRST injection attempt, not an organic event),
but explicitly reasoned the logged 03:27:35 injection was "30 minutes after
the crash loop began, so it isn't the trigger," and concluded a fabricated
"memory leak in go-enricher" instead — confidently, explicitly rejecting
the real cause. Haiku 4.5's run, by contrast, anchored to the single current
changelog entry and correctly tied the enricher halt to it (less thorough,
but not fooled).

This is a live-caught instance of the same contamination risk the original
project docs already flagged (Known Risk #6 — no full stack reset between
fault trials) but here it's cross-*attempt* contamination from re-running
the SAME fault multiple times in one debugging session, not cross-fault
contamination between different faults. It's also a concrete, real
demonstration that the corroboration downgrade rule (`agent/graph.py`
`conclude()`) only checks breadth of tool citations, not whether those
tools were looking at clean data — Sonnet cited 3 distinct tools and would
NOT have been downgraded by that rule despite being wrong.

Recorded honestly in `eval/runs/fault06/fault6_claude-desktop-sonnet5.result.json`
with `needs_review: true` and a full `_grading_note` explaining the
contamination. **Do not silently exclude or silently include this row when
computing aggregate accuracy/false-confidence stats** — flag it for the
README's threats-to-validity section (#6.4, alongside the fault 9
budget-exhaustion-retry gap) and let a human decide how to weight it. This
is a strong, honest artifact for the write-up, not just a data-quality
annoyance — it's a real example of the exact failure mode (confident,
well-corroborated, wrong) this whole project studies.

## Bug found and fixed during fault 13 execution (2026-09-11)

`agent/graph.py`'s `conclude()` correctly computes `downgrade_reason` (the
fix from earlier tonight) and puts it in the `conclusion` dict, and the
trace file's `conclusion` event correctly showed `"downgrade_reason":
"budget_exhausted"` for `fault13_kaggle-qwen32b_ragoff` (self-reported
confirmed, corroborated by 3 distinct tools, but downgraded because it used
all 15 calls). But `agent/run.py`'s `build_result()` never copied
`downgrade_reason` from the conclusion dict into the flattened
`result.json` schema at all - the key was silently absent, not null,
meaning any tool reading the result files would see `None` regardless of
what actually happened, hiding real downgrade data for every "self-reported
confirmed but budget-exhausted" case. Fixed: added `"downgrade_reason":
conclusion.get("downgrade_reason")` to both the success path and the
crash-path defaults in `build_result()`. Verified the fix by reading the
trace file directly (ground truth) and confirming the JSON output now
matches. Retroactively patched the one already-affected file
(`fault13_kaggle-qwen32b_ragoff.result.json`, now correctly
`"budget_exhausted"`) plus the 4 earlier Kaggle result files from faults 9
and 6 (all correctly `None` - none of those runs self-reported "confirmed"
in a way needing a downgrade, verified via an assertion before patching,
not assumed).

## Bug found and fixed during re-grade/re-aggregate (2026-09-11)

Two more real issues surfaced running `grade.py`/`aggregate.py` on the full
32-row dataset:

1. **Self-inflicted grading contamination**: `reasoning` fields I hand-wrote
   for `fault9_claude-desktop-haiku4.5` and (cosmetically)
   `fault6_claude-desktop-haiku4.5` described what the model *didn't* say
   using the exact ground-truth phrase ("go-server is never named"),
   which `grade.py`'s phrase matcher then credited as if the model had said
   it. Fixed by rewriting both to describe the gap without using the
   literal rubric phrase. Fault 9 Haiku's score correctly dropped from a
   false 2 to an honest 1 (partial) as a result; fault 6 Haiku's score
   didn't change (its own genuine words already legitimately satisfy the
   rubric's "restart" mechanism phrase independent of my commentary).
2. **`aggregate.py` crash**: `load_results()` skips casting a numeric CSV
   field to float when it's empty, but never explicitly sets it to `None`
   either — it stays a raw empty string. Every previous run in the 20-row
   dataset happened to have a real `calls_used` value on every
   `score == 2` row, so this never fired before. Tonight's Claude Desktop
   rows have `calls_used: null` (left null rather than fabricated, since
   the pasted transcripts don't show real tool-call blocks - see each
   row's `_grading_note`), and the first such row that also scored 2 broke
   `mean_calls_to_resolution`'s sum(). Fixed by adding an explicit
   `else: row[key] = None` branch in `load_results()`'s casting loop.

Two genuine judgment-call overrides also added to `eval/score_overrides.csv`
(not text bugs - real disagreements with the mechanical score):
- `fault9_claude-desktop-sonnet5` → 2: fault 9's `confused_with_12`
  disqualifier is still a bare `["cpu throttle","cpu starved","cpu quota"]`
  list (same class of bug as faults 1/2/13/16, not yet fixed in
  `rubric.yaml` - flagged instead of touching the rubric again this late).
  This run explicitly rules CPU throttling OUT, correctly names go-server
  + network latency as the cause.
- `fault6_claude-desktop-sonnet5` → 0: the contamination case (see the
  section above) - genuine phrase matches on "restart"/"go-enricher" but a
  confidently wrong causal claim (organic memory leak vs. the real
  deliberate restart-loop injection). Mechanical score would have been a
  false 2.

Final scores for the 12 new rows, post-fix: fault6 haiku=2, sonnet=0
(overridden), kaggle ragon/ragoff=-1/-1. fault9 haiku=1, sonnet=2
(overridden), kaggle ragon/ragoff=-1/-1. fault13 haiku=-1, sonnet=2,
kaggle ragon/ragoff=-1/1. `eval/summary.md` regenerated successfully
across all 32 rows - new `kaggle-qwen32b` environment shows 0% raw accuracy
on both rag arms (0/3 each) - a real, notable result for the README, not
an artifact of any bug above (all three underlying scores were re-verified
individually: 4 honest inconclusive, 1 wrong-but-overridden-from-2-to-0,
1 genuine partial credit).

## Independent audit round (2026-09-11) and fixes applied

An independent Opus-level audit (dispatched specifically to check this
session's own work skeptically, including the score overrides made by the
same person who'd be quoting these numbers) found real issues beyond what
this log had already caught:

1. **A third, undiagnosed instance of the grading-contamination bug**, in
   `fault9_claude-desktop-sonnet5`'s hand-written `reasoning` field: it
   literally said "ruling out both Redis (disqualifier confused_with_4) and
   CPU throttle (disqualifier confused_with_12) as the cause" - naming the
   rubric's own disqualifier phrase and ID directly. This, not the model's
   actual words ("not one that's CPU-throttled", hyphenated, never a
   substring match), is what forced the mechanical score to 0. The
   `score_overrides.csv` entry added earlier to fix this scored it
   correctly (2) but for the WRONG stated reason - it blamed an unfixed
   rubric disqualifier that was never actually the trigger. **Fixed**:
   rewrote the reasoning field to remove the literal disqualifier
   reference; the row now scores 2 mechanically with no override needed;
   the override line was removed from `score_overrides.csv`.
2. **`grade.py`'s `needs_review` flag was silently destroyed**: `row =
   {**result, **grading}` let the auto-computed `grading["needs_review"]`
   always overwrite a hand-set `true` from the source result.json. All 6
   hand-graded rows from tonight showed `needs_review=False` in
   `results.csv` regardless of what their own `_grading_note` said, so 4 of
   them (with real, disclosed limitations - narrated-transcript-only data,
   a failed Haiku session) never reached `grading_queue.csv` for a human to
   see. **Fixed**: `grading["needs_review"] = grading["needs_review"] or
   bool(result.get("needs_review"))` before the override check. Re-running
   `grade.py` now correctly surfaces 5 of tonight's rows into the review
   queue (fault6/9/13 haiku, fault9/13 sonnet) alongside the one pre-existing
   fault-2 item from the original 20-row dataset.
3. **`aggregate.py`'s "Corroboration-gate ablation" was mislabeling
   budget-exhaustion downgrades as corroboration** - it only ever checked
   `confidence != model_self_reported_confidence`, with no attribution to
   which rule actually fired. **Fixed**: split into
   `corroboration_gate_count` / `budget_exhaustion_gate_count` /
   `unattributed_gate_count` (for downgrades that predate the
   `downgrade_reason` field existing at all), using `downgrade_reason`
   where present and treating any claude-desktop downgrade as
   corroboration-only by construction (no budget concept exists for manual
   chats). Verified: `kaggle-qwen32b (rag=off)`'s 1 gate-fire is now
   correctly attributed to budget-exhaustion, not corroboration - this was
   the exact case the audit flagged as mislabeled.
4. **`grade.py`'s docstring still said "pre-registered"** after the YAML
   header fix earlier - missed the same fix in this file. Fixed.
5. **The fault-6 contamination decision, explicitly made now**: the
   contaminated `fault6_claude-desktop-sonnet5` row stays scored 0
   (correct on the merits - it's a confidently wrong answer) and stays
   INCLUDED in the aggregate `claude-desktop-sonnet5` numbers, not
   excluded. This is a deliberate choice, not the silent default the audit
   flagged: excluding it would cherry-pick away a real failure mode this
   project studies. **The README must disclose, next to any
   `claude-desktop-sonnet5` accuracy/false-confidence number, that 1 of
   that environment's 5 rows is a documented contamination case** - do not
   quote those numbers without that caveat sitting next to them.

Also disclosed by the audit but NOT code-fixed (framing-only issues for the
README to state directly, not bugs to patch):
- Fault 9's two automated "inconclusive" rows are labeled "honest abstain"
  by `aggregate.py`'s generic template text, but were actually killed by
  the known `investigate()` no-tool-call retry gap, not a reasoned
  abstention. Fault 6's two automated rows burned their full budget on
  repeated near-duplicate calls. State this plainly in the README rather
  than let "honest abstain" imply more deliberation than occurred.
  (Fault 13's ragoff `list_recent_changes`-repeat pattern needs the same
  framing if quoted.)
- Claude Desktop legs get only the bare trigger string - no
  `agent/system_prompt.md`, no baseline sweep, no budget, no downgrade
  gate. The existing threats-to-validity draft only mentions the missing
  budget; it should say plainly that Sonnet/Haiku vs. `kaggle-qwen32b` is
  not a same-scaffold model comparison at all.
- Fault 13's `confused_with_15` fix (compound phrases) is narrower than
  fault 15's actual vocabulary might turn out to be (fault 15 has never
  been run) - low impact now, worth a one-line note if fault 15 ever runs.

Post-fix scores for the 12 new rows (verified via a clean `grade.py` +
`aggregate.py` re-run, both exit 0, `results.csv` = 32 rows): fault6
haiku=2, sonnet=0 (override, contamination, kept), kaggle ragon/ragoff=
-1/-1. fault9 haiku=1, sonnet=2 (now unaided, no override), kaggle
ragon/ragoff=-1/-1. fault13 haiku=-1, sonnet=2, kaggle ragon/ragoff=-1/1.

A second, independent verification pass (fresh Opus agent, checking the
fixes above rather than re-auditing from scratch) confirmed fixes #1-3 and
#5 correct, but caught two things this pass had missed:
- Fix #4 was only substantively done, not literally - `grade.py`'s
  docstring still contained the literal word "pre-registered" (as part of
  a caveat pointing at `rubric.yaml`'s real header), so a plain grep for
  the word would still hit it. **Re-fixed**: reworded to remove the literal
  string entirely while keeping the same accurate pointer.
- `fault9_claude-desktop-sonnet5`'s `summary` field still contained a
  paraphrase ("CPU throttling... were also ruled out") instead of language
  matching the model's actual words ("not one that's CPU-throttled") -
  harmless today (doesn't match the "cpu throttle" disqualifier substring
  either way) but it's the same verbatim-drift pattern that caused the
  reasoning-field contamination bug three separate times tonight. **Fixed
  proactively**: reworded to track the transcript's actual phrasing
  instead of paraphrasing it, closing off a 4th potential instance before
  it could cause a problem.

Re-verified clean after both closing fixes: `grep -ci "pre-registered"
eval/grade.py` = 0; `fault9_claude-desktop-sonnet5` still scores 2 with
`disqualifier_hit` empty; full `grade.py` + `aggregate.py` re-run still
exits 0 on both, 32 rows.

## Explicitly not doing on this branch

- Splitting the `-1` score code into separate "abstained" vs. "crashed"
  codes, or any re-grading of the existing 20 rows beyond the fault-16 fix
  in #3. Real known gap (conflates honest abstention with a crash/parse
  failure), but re-grading changes numbers already reported elsewhere —
  do not touch without explicit user sign-off.
- The pytest invariant-test suite (16th call blocked, duplicate consumes
  budget, both downgrades fire, secret redaction holds). Valuable, cut for
  time — note as a "next steps" item in the README (#6.6), don't build it.
- Unleaking `grading_queue.csv` (currently exposes the answer-key phrase to
  a supposedly-blind human reviewer via `mechanism_hit`/`target_hit`
  columns). Real bug, out of scope for this branch.
- Moving the 1.5x crash-cost multiplier from `aggregate.py` into
  `cost_matrix.yaml`. Cosmetic/rigor cleanup, out of scope for this branch.
- Restoring "act on all requested tool calls per turn" (currently only
  `response.tool_calls[0]` is ever acted on). Known regression from an
  earlier rewrite. Do not fix on this branch — it would change agent
  behavior mid-eval-sweep and invalidate comparability with the existing
  20 rows.
- Anything outside `agent/` and `mcp-server/` unless explicitly listed
  above (e.g. `eval/`, which is explicitly in scope for items #3-#5).
- Any change to the underlying fraud-detection pipeline itself
  (`go-server`, `go-enricher`, `python-producer`, `docker-compose.yaml`,
  `fault-injection/`) — not in scope for this project's remaining work at
  all.
