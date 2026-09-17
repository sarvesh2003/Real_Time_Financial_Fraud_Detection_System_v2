# MASTER_Doc_Validation.md

Validation of `MASTER.md` against the code and data actually in this repository.

**Date:** 2026-09-15 · **Branch:** `harden-agent-and-eval` · **HEAD:** `fdc3b3f`
**Doc validated:** `MASTER.md` (781 lines, last modified 2026-09-13)
**MASTER.md was not modified.** Every other operation in this audit was read-only.

---

## What counted as evidence

Per the ground rules, **only code and the data files that code reads or writes** were
admitted as proof:

| Admitted | Not admitted |
|---|---|
| `.py`, `.go`, `.sh` source | `README.md`, `PLAN.md`, `Deployment.md`, `notes.md`, `INCIDENT_INVESTIGATOR.md`, `personal-notes/`, `trash/` |
| `rubric.yaml`, `cost_matrix.yaml`, `prometheus.yml`, `docker-compose.yaml` | Code comments and docstring prose, *as assertions about behaviour* |
| `results.csv`, `score_overrides.csv`, `grading_queue.csv`, `env_manifest.json` | `eval/summary.md`, `eval/validity_report.md` (generated prose) |
| `eval/runs/**/*.result.json`, `*.trace.jsonl`, `corpus/traces.jsonl`, `chroma.sqlite3` | `eval/grade-fault-investigation-SKILL.md` |
| `agent/system_prompt.md` — **admitted**: it is a runtime input read by `graph.py:18`, not documentation | |
| Git history, for "when did X change relative to the runs" | |

Docstrings *are* admitted where the doc's claim is "this text exists in the file and the
model sees it" — that is a claim about a string literal, which is checkable. They are not
admitted as evidence that the code does what they say.

Where a claim needed arithmetic, I recomputed it from the raw artifacts rather than
trusting either the doc or a generated summary. Two programs were executed, both verified
read-only first: `eval/check_contamination.py` (reads only) and my own throwaway scripts.
I did **not** run `eval/validity/run.py` (it writes `validity_report.md`) or any Chroma
query (the client may write to the store).

---

## Verdict by section

| § | Section | Verdict |
|---|---|---|
| 1 | In one page | **Accurate** |
| 2 | Concepts you need | **Accurate** (1 unverifiable example) |
| 3 | The setup | **Accurate** (1 overgeneralisation) |
| 4 | Architecture | **Accurate** |
| 5 | The tools | **Accurate** (1 omission) |
| 6 | The agent | **Accurate** |
| 7 | A worked example | **Accurate** (1 arithmetic slip) |
| 8 | How a run gets scored | **Outdated / partially wrong** — 2 real errors |
| 9 | What was actually varied | **Outdated / partially wrong** — 1 real error |
| 10 | The dataset as it stands | **Accurate** — verified to the row |
| 10a | What can actually be claimed | **Partially wrong** — 1 overstatement, and its premise is undermined |
| 11 | How to run it | **Accurate** |
| 12 | Limitations visible in the code | **Substantially outdated** — 4 material errors, 3 minor |
| — | Whole subsystems absent | **Missing** — see §Missing |

---

## §1 — In one page · **Accurate**

Every structural claim checks out.

- "six functions" — `grep -c "@mcp.tool()" mcp-server/server.py` → **6**. ✓
- "up to 15 calls" — `TOOL_CALL_BUDGET = 15`, [graph.py:22](agent/graph.py#L22). ✓
- "2 (right), 1 (partly), 0 (wrong), or −1" — [grade.py:75-98](eval/grade.py#L75-L98). ✓
- "38 graded investigations over 7 faults and 6 model configurations" — `results.csv` holds
  38 rows, 7 distinct `fault_id`, 6 distinct `env_label`. ✓
- "Every turn is written to a file" — [run.py:262-265](agent/run.py#L262-L265). ✓

*Trivial:* the pipeline sentence omits `prometheus`, `geoip-updater` and
`redis-cluster-setup`, which are in `docker-compose.yaml`. §4's diagram covers Prometheus,
so this is a summary elision, not an error.

---

## §2 — Concepts you need · **Accurate** (one unverifiable example)

- stdio transport at [server.py:513](mcp-server/server.py#L513). ✓
- Four LangGraph nodes at [graph.py:466-478](agent/graph.py#L466-L478). ✓
- Prometheus `scrape_interval: 5s`, [prometheus.yml:2](prometheus/prometheus.yml#L2). ✓
- Wilson interval at [aggregate.py:24-31](eval/aggregate.py#L24-L31). ✓
- Hit@1 / MRR at [aggregate.py:258-272](eval/aggregate.py#L258-L272). ✓
- `tc netem` used by exactly two faults (9 and 10). ✓
- **"five past investigation write-ups … 384 numbers per document"** — confirmed directly
  from the store, opened read-only: `collections` reports `dimension = 384`, and
  `embeddings` holds **5** rows. ✓

**Unverifiable:** the worked distance example — *"a query built from 'redis2 exited,
throughput collapsed' returns `redis/node_stopped` at distance 0.77 and
`network/latency_injection` at 0.99."* Reproducing it requires running a live Chroma
query, which I did not do (the client may write to the store). No recorded trace
corroborates those two numbers: across 214 recorded distances, `node_stopped` appears at
0.900 and 0.897 and `latency_injection` at 0.942 and 0.995 — but those come from different
query texts, so they neither confirm nor refute the example. Treat 0.77/0.99 as
illustrative and unchecked.

---

## §3 — The setup · **Accurate** (one overgeneralisation)

I verified all 16 rows of the fault table against the scripts. Every one matches:

| # | Doc says | Code |
|---|---|---|
| 1 | `compose stop redis2` | `TARGET:-redis2`, `compose stop` ✓ |
| 2 | redis1 `maxmemory=5mb` + `allkeys-lru`, 200 000 keys | `LOW_MAXMEMORY="5mb"`, `FLOOD_KEYS:-200000` ✓ |
| 3 | redis1 `maxclients=5` | `LOW_MAXCLIENTS:-5` ✓ |
| 4 | `DEBUG SLEEP 10` on redis1 | `DURATION:-10` ✓ |
| 5 | 3→1 replica + 60 000-request `ghz` burst | `NORMAL=3`, `FAULT=1`, `BURST_COUNT:-60000` ✓ |
| 6 | 6 restarts, 5 s apart | `CYCLES:-6`, `INTERVAL:-5` ✓ |
| 7 | 500 transactions for one UUID user id | `BURST_COUNT:-500`, `BURST_USER` is a UUID ✓ |
| 8 | one plain-text message to `raw_transactions` | ✓ |
| 9 | 200 ms `tc netem delay`, netshoot sidecar | `DELAY:-200ms`, `IFACE:-eth0` ✓ |
| 10 | `tc netem loss 10%` | `LOSS_PCT:-10%` ✓ |
| 11 | `compose pause go-server` | ✓ |
| 12 | `docker update --cpus=0.1` | `LOW_CPUS:-0.1` ✓ |
| 13 | go-server recreated, timeouts 1 ms | `LOW_TIMEOUT_MS:-1`, three env vars ✓ |
| 14 | `REDIS_POOL_SIZE=2` | `LOW_POOL_SIZE:-2` ✓ |
| 15 | 20 retries, zero backoff, latent | `HIGH_RETRIES:-20`, both backoffs `0` ✓ |
| 16 | redis1 5 MB + `noeviction`, flooded | ✓ |

- `log_change` at [common.sh:33-39](fault-injection/common.sh#L33-L39), writing timestamp /
  family / fault / action. ✓
- Fault 12's recreate-to-restore and fault 3's restart fallback are both real, at the cited
  lines (±3). ✓
- **Fault 5 code/data disagreement — correct and worth keeping.** `rubric.yaml` has exactly
  15 entries (1–4, 6–16) and `run_fault_eval.sh:34-50` has the same 15. Fault 5 is
  unreachable and ungradable. ✓

**Overgeneralisation:** *"The config faults export an environment variable and run
`compose up -d --force-recreate go-server`."* True for 13, 14 and 15. **False for 16**,
which is a config-family fault that targets `redis1` via `redis_cli CONFIG SET` and a
`redis-benchmark` flood — no env var, no recreate. Say "faults 13–15" instead.

---

## §4 — Architecture · **Accurate**

- Changelog rotation at [run_fault_eval.sh:69-75](eval/run_fault_eval.sh#L69-L75). ✓
- MCP subprocess at [run.py:204-215](agent/run.py#L204-L215); Chroma at
  [run.py:217-218](agent/run.py#L217-L218). ✓
- Two output files, `<RUN_TAG>.trace.jsonl` and `<RUN_TAG>.result.json`. ✓
- `aggregate.py → summary.md` — written at [aggregate.py:350-351](eval/aggregate.py#L350-L351). ✓

*Data-state nuance (not a doc error):* `eval/summary.md` now opens with a
**"WITHDRAWN. Do not cite any number in this file."** banner. §4 and §11 present it as the
normal roll-up output without saying so; §10a effectively supersedes it. Worth one
cross-reference.

---

## §5 — The tools · **Accurate** (one omission)

All six tool rows verified against [server.py](mcp-server/server.py) line by line — every
command string, cap, and line citation is right, including the subtle ones:

- `query_logs` newest-500 cap and the "0 of N matched … filter miss, not silence" branch
  ([server.py:120-131](mcp-server/server.py#L120-L131)). ✓
- `get_metrics` step `max(5, window//60)` and the ≥6 h re-query at
  [server.py:213-254](mcp-server/server.py#L213-L254) — the doc's cited range is exact. ✓
- `read_config`'s five fixed Redis keys ([server.py:27](mcp-server/server.py#L27)) and the
  `REDIS_`/`KAFKA_`/`GRPC_` allowlist minus secret markers. ✓
- Change ID = `sha256("<family>/<fault>")[:8]`
  ([server.py:502-505](mcp-server/server.py#L502-L505)). ✓

**The safety audit in §5 is correct and I re-derived it independently.** Exactly two
`subprocess.run` sites (`run_compose` at `:45`, `docker inspect` at `:384`); exactly three
`requests.get` calls (`:199`, `:218`, `:295`); one file read (`:487`, default read mode).
`run_compose` is reached with only `logs`, `ps --all --format json`, `ps -q`, and
`exec -T -- <redis-node> redis-cli CONFIG GET <key>` — the last hard-coded to `CONFIG GET`
with a fixed five-key list and a redis1/2/3 service guard. **No write path exists.** ✓

**Missing:** the doc never mentions `MAX_SERIES = 25`
([server.py:19](mcp-server/server.py#L19)). `get_metrics` silently truncates to the first
25 series and prepends a `[showing first 25 of N …]` banner
([server.py:256-269](mcp-server/server.py#L256-L269)). That is a second, undocumented
truncation cap alongside the 500-line one the doc does cover.

*Trivial:* the doc writes `docker compose ps -q`; the code is `ps -q -- <service>`.

---

## §6 — The agent · **Accurate**

The highest-fidelity section in the document. Every citation I checked landed on the right
lines:

| Claim | Code | ✓ |
|---|---|---|
| State dict fields | [graph.py:47-54](agent/graph.py#L47-L54) | ✓ |
| Baseline sweep order + `read_config` per running service | [graph.py:217-236](agent/graph.py#L217-L236) | ✓ |
| Baseline calls don't touch `calls_used` | `baseline()` never increments | ✓ |
| `config_diff` groups by trailing digit, not an MCP tool, counts toward the gate | [graph.py:159-180](agent/graph.py#L159-L180), [:233-236](agent/graph.py#L233-L236), [:436](agent/graph.py#L436) | ✓ |
| Fresh two-message conversation each turn | [graph.py:288-291](agent/graph.py#L288-L291) | ✓ |
| 2 000-char evidence cap | `EVIDENCE_RESULT_CHAR_CAP` | ✓ |
| Retrieval: trigger + 300 chars × last 3, n=3, cutoff 1.0, family/name only | [graph.py:250-256](agent/graph.py#L250-L256) | ✓ |
| `root_cause` excluded from the embedding | [build_index.py:36-39](corpus/build_index.py#L36-L39) | ✓ |
| Budget checked *before* the action is honoured | [graph.py:344-350](agent/graph.py#L344-L350) | ✓ |
| DUPLICATE still costs a call | [graph.py:355-362](agent/graph.py#L355-L362), [:376](agent/graph.py#L376) | ✓ |
| `length` / empty retried once | [graph.py:304-324](agent/graph.py#L304-L324) | ✓ |
| Confidence override, budget before corroboration | [graph.py:435-448](agent/graph.py#L435-L448) | ✓ |
| Both confidence values kept | [graph.py:453-454](agent/graph.py#L453-L454) | ✓ |
| `PARSE_FAILURE` instead of crashing | [graph.py:416-433](agent/graph.py#L416-L433) | ✓ |
| Sampling params + `reasoning_effort="none"` for qwen | [run.py:191-201](agent/run.py#L191-L201) | ✓ |
| `evidence_groundedness` = digit-bearing tokens >3 chars | [run.py:130-139](agent/run.py#L130-L139) | ✓ |
| System prompt: 4 "normal" items, 7 rules | [system_prompt.md:22-25, 35-41](agent/system_prompt.md) | ✓ |

**Minor omissions:** `ask_for_json` carries two schema-coercion fallbacks — unwrapping a
single-key wrapper object and unwrapping a `properties` key
([graph.py:130-145](agent/graph.py#L130-L145)) — which makes "unparseable JSON" rarer than
§6 implies. And `conclude` silently drops out-of-range cited indices
([graph.py:435](agent/graph.py#L435)), so a model citing evidence #99 loses that citation
without any record.

---

## §7 — A worked example · **Accurate** (one arithmetic slip)

I reconstructed this run end to end from
`eval/runs/fault02/fault2_kaggle-qwen14b_ragoff.{trace.jsonl,result.json}`. Every figure
matches:

| Doc | Artifact |
|---|---|
| 2026-09-10 23:07:23 UTC | trace header `2026-09-10T23:07:23.445428+00:00` ✓ |
| 16 baseline evidence entries | `baseline_evidence_count: 16` ✓ |
| `2026-09-10T20:56:38Z \| change=CHG-9a260cd9 \| inject` | byte-identical ✓ |
| `CHG-9a260cd9` = `sha256("redis/02_eviction_storm")[:8]` | **recomputed → `9a260cd9`** ✓ |
| config_diff output (both lines) | byte-identical ✓ |
| "everything Up 8 hours except redis2, Up 2 hours" | ✓ |
| `read_config` across 12 running services | 12 distinct services after dedup; 3 + 12 + 1 = 16 ✓ |
| spent 10 of 15 calls | `calls_used: 10` ✓ |
| 6th call = `get_metrics(redis_evicted_keys_total, 1h)` | action #6 matches the quoted JSON exactly ✓ |
| flat at 312 445 ×3 then climbing | `…:312445, …:312445, …:312445, …:312511, …:312682` ✓ |
| `"(0 of 18689 log lines …)"` | byte-identical ✓ |
| two DUPLICATEs | `duplicate_calls: 2` (calls 8 and 10) ✓ |
| summary verbatim | byte-identical ✓ |
| `[15, 21]`, `["config_diff","get_metrics"]` | ✓ |
| gate did not fire | `downgrade_reason: null`, self-reported = enforced = `confirmed` ✓ |
| 479.6 s, 12 model calls, 106 314 in / 2 085 out | ✓ exactly |
| half cited from sweep, groundedness 1.0 | `0.5`, `1.0` ✓ |
| graded 2 | `results.csv` → score 2 ✓ |

**Arithmetic slip:** *"The other eight calls were weaker."* There were 10 calls and the doc
highlights one, so it should be **nine**.

*Trivial:* "showed everything running" — `redis-cluster-setup` was `exited (0)`, which the
system prompt itself flags as normal.

---

## §8 — How a run gets scored · **Outdated / partially wrong**

The mechanics are right. Two specific claims are wrong.

**Correct:** matcher is `re.search(re.escape(phrase), text, IGNORECASE)` over
`summary + reasoning` ([grade.py:38-42](eval/grade.py#L38-L42), [:46](eval/grade.py#L46));
the 2/1/0/−1 ladder at [:75-98](eval/grade.py#L75-L98); the four never-silently-scored
situations at [:76-90](eval/grade.py#L76-L90); overrides applied after auto-scoring and
`results.csv` rebuilt from scratch; the fault-1 and fault-2 disqualifier design notes
(`rubric.yaml:34-37`, `:61-66`); `split` / `liveness` / `baseline_sufficiency` on every
entry; `llm_judge.py` covering **only** faults 6, 9, 13 (`GROUND_TRUTH` keys at
[:46-77](eval/llm_judge.py#L46-L77)) and writing its own files, never `results.csv`. ✓

Every row of the aggregator metric table is correct, including the fiddly ones:
confirmed-recall's denominator at [aggregate.py:86-87](eval/aggregate.py#L86-L87), the
three-way downgrade split at [:107-118](eval/aggregate.py#L107-L118), and
scripted-baseline-only accuracy gated on `cited_baseline_fraction >= 0.999` at
[:131-134](eval/aggregate.py#L131-L134). Cost cells match
[cost_matrix.yaml:20-25](eval/cost_matrix.yaml#L20-L25) exactly. ✓

### ❌ Error 1 — the blind-grading claim is wrong

> "the reviewer sees the model's summary and reasoning, the disqualifier's short id, and
> the auto-score, **but not the fault's identity or the rubric's answer phrases**"

`grade.py` writes `mechanism_hit` and `target_hit` into the queue
([grade.py:167-177](eval/grade.py#L167-L177)), and those fields hold **the literal matched
rubric phrase** returned by `_any_hit` ([:41](eval/grade.py#L41)). The live
`grading_queue.csv` confirms it:

```
row1 … mechanism_hit=restart   target_hit=go-enricher   (fault 6)
row2 … mechanism_hit=latency   target_hit=(blank)       (fault 9)
row3 … mechanism_hit=latency   target_hit=go-server     (fault 9)
row5 … mechanism_hit=1ms       target_hit=go-server     (fault 13)
```

Only `disqualifier_hit` is reduced to an id. A reviewer seeing `1ms` + `go-server` can
identify fault 13 immediately. The blinding is partial, and the doc describes it as total.
(The code comment at `:163-166` makes the same wrong claim — which is exactly why comments
weren't admitted as evidence.)

### ❌ Error 2 — the override count is stale

> "The five on file each carry a rationale; two *lower* a mechanically-passing score."

`score_overrides.csv` has **eight** data rows, not five. Breakdown:

| Direction | n | Runs |
|---|---|---|
| Raises 0 → 2 | 2 | `fault1_claude-desktop-opus5`, `fault1_claude-desktop-sonnet5` |
| Lowers a passing score | 5 | `fault6_claude-desktop-sonnet5` (2→0), `fault6_kaggle-qwen14b_ragon` (1→0), `fault1_local-qwen9b_ragoff` (2→0), `fault1_local-qwen9b_ragon` (2→0), `fault2_local-qwen9b_ragoff` (1→0) |
| No score change | 1 | `fault2_kaggle-qwen14b_ragon` — its own note says "auto-score was already 0; override recorded only to close the review flag" |

So: eight on file, **five** lower a mechanically-passing score, two raise one.
§10a's "7 disagreements … Two runs the rubric wrongly zeroed. Five it wrongly credited"
is the correct version and **contradicts §8**. §8 is the stale one.

*Minor:* crashed-run pricing is cited as `:196-197`; the code is at
[aggregate.py:199](eval/aggregate.py#L199) (`:196-198` is the comment). The 1.5× claim is
correct.

---

## §9 — What was actually varied · **Outdated / partially wrong**

**Correct:** the fault list (1, 2, 3, 4, 6, 9, 13); all six env labels and their model ids;
retrieval on/off per automated run and blank on all Claude Desktop rows; **six distinct
ngrok URLs plus one LAN address** (I counted them in `results.csv` — exactly six ngrok
hosts + `http://100.103.190.22:11434/v1`); the constants at
[graph.py:22-26](agent/graph.py#L22-L26); the confounding of model size with host, and
"only the 14B covers all seven faults"; `CLAUDE_MODELS=(sonnet5 haiku4.5)` at
[run_fault_eval.sh:128](eval/run_fault_eval.sh#L128) versus `claude-desktop-opus5` rows
existing for faults 1 and 2; `env_manifest.json` still carrying
`"written_at": "REPLACE_BEFORE_RUN"` and listing 2 environments out of 6; one automated
environment per invocation. ✓

### ❌ Error — the trigger is not constant across all 38 rows

> "**Held constant:** the trigger text (one distinct value across all 38 rows)"

`results.csv` holds **two** distinct values:

| Value | n |
|---|---|
| `"Something seems off with the fraud pipeline, can you investigate?"` | 26 (all automated) |
| `""` (empty) | 12 (all `claude-desktop-*`) |

The harness *prescribes* the same sentence for the manual leg
([run_fault_eval.sh:146](eval/run_fault_eval.sh#L146)), and the transcripts may well show
it — but `claude_desktop_template.json` has no `trigger` field at all, so it was never
recorded. The doc already notes blanks for `retrieval_mode` on these same rows, so it is
inconsistent with itself here. Correct phrasing: "one distinct value across the 26
automated rows; not recorded on the 12 manual rows."

### Missing nuance worth adding here

**`mcp-server/server.py` at HEAD is not the version that produced any graded run.** I
verified this behaviourally, not just from git: every graded trace containing a truncated
log result carries the old banner `[showing last 500 matching lines …]`, and **no** graded
trace contains the current `[TRUNCATED: N of M lines matched …]` text emitted by
[server.py:135-140](mcp-server/server.py#L135-L140). §12 makes this point for the fault-13
docstring specifically; it generalises to the whole tool layer and belongs in §9's
"held constant" discussion.

---

## §10 — The dataset as it stands · **Accurate**

The most rigorously checkable section, and it survives every check. I recomputed all of it
from `results.csv`:

- 38 rows, **all** `status: completed`, **zero** `PARSE_FAILURE`. ✓
- Outcomes 2/1/0/−1 = **12 / 4 / 12 / 10**. ✓
- Enforced confidence confirmed/best_guess/inconclusive = **17 / 11 / 10**. ✓
- Confident **and** correct = **9**. ✓
- Confident and wrong = **8**, and **all eight run ids listed in the doc match exactly**. ✓
- Per-environment 2/1/0/−1 table — all six rows exact. ✓
- Per-fault counts 7/7/4/2/6/6/6 — exact. ✓
- By family redis 20, kafka 6, network 6, config 6 — exact. ✓
- Retrieval 13 on / 13 off / 12 blank — exact. ✓
- 5 rows `needs_review`, 8 closed by override, 3 of those the zero-tool-call rows. ✓
- `local-qwen9b` scores 0 on all six runs post-override. ✓
- **56 result files under `eval/runs/`, 38 matched by the glob.** ✓ The excluded-directory
  accounting is exact: `fault02_contaminated_run1` 4, `fault02_pre_doctrine_fix` 4,
  `fault03_pre_readconfig_fix` 4, `fault09_v2` 2, `fault13_v2` 2 (= 16), plus
  `fault09/_contaminated_stackrestart` 2 nested too deep. 56 − 18 = 38. ✓

*Nuance, not an error:* the doc says "nothing in the code records a per-directory
justification." Literally true — but three excluded directories do ship a `README.txt`
stating the reason (`fault09_v2`, `fault13_v2`, `fault09/_contaminated_stackrestart`).
Worth a clause, since it slightly softens the "stated reason is unclear" conclusion.

---

## §10a — What can actually be claimed · **Partially wrong**

Most of this section is exactly right, and I reproduced its central computation
independently.

**Verified:**

- The uncontaminated subset resolves to **n = 14**: rows on faults 4, 6, 9, 13 from the two
  Kaggle environments. Outcome **0 correct / 2 partial / 3 wrong / 9 abstained** — matches
  the doc cell for cell. ✓
- Abstention **9/14 = 64%**. ✓
- All five exclusion counts reconcile: 6 truncated (`local-qwen9b`), 12 baseline-sufficient
  automated rows on faults 1–3, 12 hand-transcribed, 3 zero-tool-call, 5 unresolved. ✓
- **Null model:** all six `local-qwen9b` runs report first-turn `input_tokens = 2050`,
  against 5361–6676 on comparable hosted runs — the 2048-context clamp is real and
  measurable. Original auto-scores were 2, 2, 1, 0, 0, 0 → **2 of 6 = 33%**. ✓
- **Grader error rate:** 7 disagreements / 38 = 18.4%, two upward and five downward. ✓

### ❌ Overstatement

> "every correct answer in the full dataset sits on a fault whose answer was reachable
> without investigating"

Two of the twelve score-2 rows fail this:

| Run | Fault | `baseline_sufficiency` |
|---|---|---|
| `fault9_claude-desktop-sonnet5` | 9 | **`absent`** |
| `fault6_claude-desktop-haiku4.5` | 6 | **`hinted`** |

Per `rubric.yaml`, `absent` means "the sweep has nothing, the model must actively probe."
The claim needs narrowing to the automated rows, or to "ten of twelve."

### ⚠ The premise of the "uncontaminated subset" does not hold

This is the most decision-relevant finding in the audit. §10a defines its clean subset as
faults **4, 6, 9, 13**. But the repo's own contamination checker — `eval/check_contamination.py`,
which mirrors `grade.py`'s matcher exactly — reports that **faults 6 and 9 are fully
answerable from free context**:

```
FAIL  fault 6 … every scoring component is present in agent/system_prompt.md
      mechanism='consumer group', target='go-enricher'
FAIL  fault 6 … every scoring component is present in mcp-server/server.py::query_logs()
      mechanism='rebalance', target='go-enricher'
FAIL  fault 9 … every scoring component is present in agent/system_prompt.md
      mechanism='latency', target='go-server'
FAIL  fault 9 … every scoring component is present in mcp-server/server.py::get_metrics()
      mechanism='latency', target='go-server'
```

So 8 of the 14 "clean" rows sit on faults the harness itself classifies as leaked. The
subset is *less* contaminated than the full set, not uncontaminated. The headline —
"the agent solved none of the faults that were not contaminated" — happens to survive
(it is 0-for-14 either way), but the framing is wrong and the 64% abstention claim rests
on a subset that would shrink to n = 4 (fault 4 and fault 13 only) under the harness's own
standard.

---

## §11 — How to run it · **Accurate**

- `build_index.py` deletes and recreates the collection,
  [:49-53](corpus/build_index.py#L49-L53). ✓
- `KAGGLE_OLLAMA_URL` required ([run_fault_eval.sh:29](eval/run_fault_eval.sh#L29));
  `KAGGLE_MODEL` defaults to `qwen2.5:32b-instruct` ([:31](eval/run_fault_eval.sh#L31));
  `KAGGLE_ENV_LABEL` defaults to `kaggle-qwen32b` ([:106](eval/run_fault_eval.sh#L106)). ✓
- Parallel legs for continuous faults, re-inject per leg for transient, `read -p` gate,
  then restore ([:158-187](eval/run_fault_eval.sh#L158-L187)). ✓
- Deliberate absence of `set -e` ([:14-20](eval/run_fault_eval.sh#L14-L20)). ✓
- `OLLAMA_BASE_URL` the only required var ([run.py:32-33](agent/run.py#L32-L33)); `.env`
  loaded first ([:23](agent/run.py#L23)); MCP venv path ([:204-213](agent/run.py#L204-L213));
  `PROMETHEUS_URL` default ([server.py:21](mcp-server/server.py#L21)). ✓
- `ANTHROPIC_API_KEY` + `LLM_JUDGE_MODEL` default `claude-sonnet-5`
  ([llm_judge.py:41](eval/llm_judge.py#L41)). ✓
- Three venvs referenced — `agent/venv`, `mcp-server/venv`, repo-root `venv`. ✓
  (Note: a fourth, `agent/venv_py314_blocked/`, exists on disk and is what
  `eval/validity/run.py` names in its usage string.)

---

## §12 — Limitations visible in the code · **Substantially outdated**

The weakest section. Its individual observations about `grade.py` and `aggregate.py` hold
up, but its central claim is false at HEAD and four of its retrieval/data claims are wrong.

### ✅ Still correct

- Fault 1's answer is in the system prompt. [system_prompt.md:16](agent/system_prompt.md)
  states the `cluster-require-full-coverage=yes` / zero-replica / `CLUSTERDOWN` mechanism;
  `rubric.yaml:33` accepts `"down"` and `:37` accepts `"redis cluster"`. ✓
- The three zero-call rows, their auto-scores (2, 2, 1), their empty `tool_call_sequence`,
  and the `wrong_node` disqualifier missing `"(e.g., redis1)"` — all verified. ✓
- Confident-and-wrong rising 6 → 8 as a result of those overrides — arithmetically correct. ✓
- The fault-13 docstring leak at
  [server.py:439-441](mcp-server/server.py#L439-L441), added in `30df3e0`, arm abandoned in
  `1a7f68c`. I confirmed **no graded result file was touched at or after `30df3e0`**, so
  "no result in `results.csv` saw it" is correct. ✓
- Literal substring matching, and fault 6's disqualifier missing the
  `"redis cluster … down"` phrasing (`rubric.yaml:112`). ✓
- The corroboration gate checks breadth, not correctness: `fault6_claude-desktop-sonnet5`
  has `distinct_supporting_tools = ["get_metrics","query_logs","list_recent_changes"]`,
  `confidence: confirmed`, final score 0 — and carries a `_grading_note` saying so. ✓
- `config_diff` counting toward the gate despite being synthesised. ✓
- Retrieval scored on runs that never retrieved
  ([aggregate.py:253-256](eval/aggregate.py#L253-L256)). ✓
- Contrast pairs: only `[13, 4]` has both members present, and only in the two
  `kaggle-qwen14b` groups; [aggregate.py:179](eval/aggregate.py#L179) keys by `fault_id`
  so duplicates collapse. ✓
- Change ID reversible over a 16-element set — **I recomputed and matched it.** ✓
- Corpus = five documents covering faults 2, 1, 8, 9, 11, which are exactly the five
  `split: indexed` faults. ✓
- Claude Desktop rows hand-transcribed; `claude_desktop_template.json` confirms `calls_used`
  is "Count of MCP tool invocations in the chat" and the confidence rule is applied by hand. ✓
- Budget asymmetry: `baseline_evidence_count` is 15 or 16 on every automated row. ✓
- Single provider hardcoded ([run.py:27-33](agent/run.py#L27-L33)). ✓

### ❌ Error 1 (major) — the check the doc says doesn't exist, exists twice

> "**nothing in the harness checks for this automatically.** … A check that fails when any
> rubric accept-phrase also appears in the system prompt or an MCP docstring **does not
> exist** and would be the correct fix."

Two implementations are in the repo at HEAD:

1. **`eval/check_contamination.py`** (214 lines) — built for precisely this. CHECK 1 walks
   every `rubric.yaml` fault and asks whether `mechanism_any_of` + `mechanism_and_any_of` +
   `target_any_of` are all satisfiable from free text, where free text =
   `agent/system_prompt.md` plus every `@mcp.tool()` docstring in `mcp-server/server.py`.
   CHECK 2 scans `results.csv` for rows scoring above 0 with `calls_used == 0`, treating
   override-covered rows as handled. I ran it: **exit code 1, 9 failures, 60 warnings.**
2. **`eval/validity/checks/leakage.py::answer_reachable_from_free_context`** — the generic
   form, reusing the grader's own matcher, reporting single-source and union leaks
   separately. Its companion `unrun_items_not_already_leaked` extends the same test to
   faults that have never been run. The adapter feeds it `agent/system_prompt.md` and every
   MCP tool docstring, extracted by AST at
   [adapter.py:190-203, 481-497](eval/validity/adapter.py).

This paragraph needs deleting, not amending.

### ❌ Error 2 (major) — the leak is much broader than "a graded fault"

§12 frames the prompt leak as affecting fault 1. The harness's own checker reports full
answer-key leaks on **six** faults:

| Fault | Leaked via | Components |
|---|---|---|
| 1 | `system_prompt.md` | `down` + `redis2` |
| 3 | `read_config()` docstring | `maxclients` + `redis1` |
| 6 | `system_prompt.md` **and** `query_logs()` docstring | `consumer group`/`rebalance` + `go-enricher` |
| 9 | `system_prompt.md` **and** `get_metrics()` docstring | `latency` + `go-server` |
| 10 | `get_metrics()` docstring | `drops` + `go-server` |
| 15 | `system_prompt.md` **and** `query_logs()` docstring | `retry` + `go-server` |

Faults 3, 6 and 9 are all in the graded dataset. This materially changes §12's
"fault 1 is the fault most affected" and, as noted above, undercuts §10a.

### ❌ Error 3 — the corpus-leak table is right; its stated consequence is not

I replicated the table exactly. Running `grade.py`'s matcher over each **whole
`traces.jsonl` record** reproduces all five rows verbatim (`latency_injection` → 1, 2, 3, 8,
9, 14, 15, 16; `container_paused` → 2, 3, 7, 9, 11, 14, 16; `malformed_event` → 6, 8, 11,
14; `eviction_storm` → 1, 2, 14, 16; `node_stopped` → 1, 2, 16). ✓

But the conclusion drawn from it — *"turning the corpus on turns an answer key on"* — is
contradicted by the agent code. **The model never sees the document text.**
[graph.py:255-256](agent/graph.py#L255-L256) renders retrieval as:

```python
retrieved_text = "\n".join(f"- {c['fault_family']}/{c['fault_name']}" for c in retrieved)
```

and `retrieve_context` ([:183-193](agent/graph.py#L183-L193)) copies only `fault_family`,
`fault_name` and `distance` out of the metadata. Symptoms, investigation path and
`root_cause` never enter the prompt. So what retrieval actually leaks is a **label** like
`redis/eviction_storm` — still a serious hint, arguably a sharper one than prose, but not
the mechanism the doc describes. "No retrieval result from this dataset is claimable"
remains a defensible conclusion; the reasoning behind it needs replacing.

*Related, unmentioned:* `build_index.py:62` stores `root_cause` in the Chroma **metadata**.
It is never embedded and never surfaced today, but it is one attribute access away from the
prompt.

### ❌ Error 4 — the distance range is wrong

> "recorded distances cluster between 0.68 and 0.99"

Across **214** recorded distances in the graded traces, the actual range is
**0.558 – 0.999**. The stated floor is too high by 0.12.

### ❌ Error 5 — the cutoff claim is wrong

> "only `fault4_kaggle-qwen14b_ragon` had the cutoff trim the list at all — to one entry"

The 1.0 cutoff trimmed the retrieved list in **9 of the 13 retrieval-on runs**, and trimmed
to a **single entry in 8** of them:

| Run | retrieval events | trimmed | trimmed to 1 |
|---|---|---|---|
| `fault1_kaggle-qwen14b_ragon` | 12 | 9 | 2 |
| `fault2_kaggle-qwen14b_ragon` | 14 | 13 | 6 |
| `fault3_kaggle-qwen14b_ragon` | 11 | 5 | 2 |
| `fault4_kaggle-qwen14b_ragon` | 10 | 6 | 5 |
| `fault6_kaggle-qwen14b_ragon` | 13 | 7 | 3 |
| `fault6_kaggle-qwen32b_ragon` | 5 | 2 | 1 |
| `fault9_kaggle-qwen14b_ragon` | 12 | 2 | 0 |
| `fault13_kaggle-qwen14b_ragon` | 10 | 6 | 4 |
| `fault13_kaggle-qwen32b_ragon` | 6 | 3 | 1 |

The fault-4 detail the doc gives (trimmed to one entry, `network/container_paused`, on a
Redis slow-command fault) is correct — but it is one case among many, not the only one.
The cutoff is doing considerably more work than the doc credits.

### ❌ Error 6 — only one fault-9 leg shows the empty-response failure

> "**Both** fault-9 qwen32b legs show this, and neither was re-run after the guard was added."

Verified from the traces:

| Run | turn 2 `input_tokens` | `output_tokens` | `finish_reason` |
|---|---|---|---|
| `fault9_kaggle-qwen32b_ragoff` | **0** | **0** | **null** |
| `fault9_kaggle-qwen32b_ragon` | 7522 | 208 | `stop` |

The **ragoff** leg matches the doc's description exactly (`llm_ms: 107124`, zeros, null
finish reason). ✓ The **ragon** leg is a normal, non-empty response with content and no
tool call — a genuine decision to conclude, which is behaviour the guard deliberately
preserves ([graph.py:301-303](agent/graph.py#L301-L303) scopes the retry to empty content
only). Both legs do end after 1 tool call and grade −1, which is probably how they got
conflated. `empty_retries` is `null` on both, confirming the field postdated them. ✓

### ❌ Error 7 (minor) — two Claude Desktop counts are wrong

> "Four of the five graded Claude Desktop faults have a row still flagged `needs_review`,
> and three rows have an empty `calls_used`."

- Flagged rows cover faults **6, 9, 13** → **three** of the five Claude Desktop faults
  (1, 2, 6, 9, 13), not four.
- Rows with an empty `calls_used`: `fault6_haiku4.5`, `fault6_sonnet5`, `fault9_haiku4.5`,
  `fault9_sonnet5`, `fault13_sonnet5` → **five**, not three.
- The follow-on point is if anything understated: timing and token metrics are blank on
  **all twelve** Claude Desktop rows, not just those.

*Minor:* "Three of the eight entries in `score_overrides.csv` correct exactly that
[substring matching]" — arguably four, if `fault2_local-qwen9b_ragoff` (credited on
`maxmemory` matched inside a negation) counts. Defensible as written, since that row's
primary defect is the truncated prompt.

---

## Missing — code does this, the doc doesn't mention it

### 1. `eval/validity/` — an entire subsystem, ~3,240 lines, absent from the doc

The single largest omission. The only occurrence of the word "validity" in MASTER.md is
the phrase "named validity defect" in §10a, which refers to defects, not to this package.

| File | Lines | Role |
|---|---|---|
| `adapter.py` | 658 | Builds a `Manifest` from this repo's artifacts; AST-extracts MCP tool docstrings |
| `comparability.py` | 475 | 5 checks |
| `manifest.py` | 423 | Harness-agnostic data contract |
| `run.py` | 281 | CLI; writes `validity_report.md`; **exits 1 on any violation** |
| `leakage.py` | 224 | 2 checks |
| `metric_soundness.py` | 202 | 2 checks |
| `context_integrity.py` | 194 | 2 checks |
| `registry.py` | 187 | Decorator registry, 4 statuses, skip ≠ pass |
| `behavioral_evidence.py` | 186 | 3 checks |
| `scoring_integrity.py` | 162 | 1 check |
| `matching.py` | 104 | Shared matcher |
| `stimulus_validity.py` | 87 | 1 check |

**16 registered checks** across seven failure classes (`stimulus_validity`,
`context_integrity`, `leakage`, `behavioral_evidence`, `scoring_integrity`,
`comparability`, `metric_soundness`).

What makes the omission acute: several of these checks are the automated form of defects
§12 describes as un-automated.

| Check | §12 defect it automates |
|---|---|
| `answer_reachable_from_free_context` | the prompt/docstring leak |
| `conclusion_without_investigation`, `credit_without_investigation` | the three zero-tool-call rows |
| `credit_rests_on_free_evidence` | correct answers reachable from the baseline sweep |
| `prompt_size_not_clamped` | the 2050-token truncation |
| `ranking_key_identifies_item` | `aggregate.py:179` collapsing rows keyed by fault id |
| `stimulus_live_during_observation` | transient faults self-resolving before observation |

### 2. `eval/check_contamination.py` (214 lines) — absent from the doc

Zero mentions. Directly refutes §12's "does not exist" claim, and its output broadens the
leak finding from one fault to six. Read-only; exits 1; currently **9 failures**.

### 3. Smaller gaps

- **`MAX_SERIES = 25`** — a second undocumented truncation cap in `get_metrics`.
- **HEAD ≠ the code that produced the data** — no graded trace contains the current
  `query_logs` truncation banner. Documented for the fault-13 docstring only; it is general.
- **`ask_for_json` coercion fallbacks** ([graph.py:130-145](agent/graph.py#L130-L145)) and
  **silent dropping of out-of-range cited indices** ([graph.py:435](agent/graph.py#L435)).
- **`root_cause` lives in Chroma metadata** ([build_index.py:62](corpus/build_index.py#L62)).
- **The LLM-judge arm produced nothing.** `llm_judge.py` writes `llm_judge_report.md` and
  `llm_judge_results.csv`; **neither file exists** on disk. §8 describes the second grader
  without noting it has apparently never been run to completion here.
- **Project lineage is never stated.** Git shows the inheritance boundary cleanly at
  `09d22eb` (merge from `Real_Time_Financial_Fraud_Detection_System_v2`). Inherited:
  `go-server/`, `go-enricher/`, `python-producer/`, `proto/`, `pb/`, `load-tests/`,
  `docker-compose*.yml`. New in this project: `agent/`, `corpus/`, `eval/`,
  `fault-injection/`, `mcp-server/`, `prometheus/`, plus `metrics.go` in both Go services.
  The doc's "the pipeline is context only" scoping is *correct* and matches that boundary
  exactly — it just never says so, which is the one sentence that would let a reader judge
  what the project actually contributes.
- `eval/grade-fault-investigation-SKILL.md` exists as a grading-prompt artifact. Flagged
  for completeness; not used as evidence.

---

## Overall verdict

**Trustworthy in the front half; needs a targeted revision in the back half. Not a rewrite.**

This is a well-made document. Its citation discipline is real, not decorative: I checked
well over a hundred line references and the overwhelming majority land within a line or two
of the right place, including several that would have been easy to fake and hard to check —
the `sha256` change-ID derivation (recomputed, matches), the 16-entry baseline-sweep
arithmetic in §7 (3 + 12 + 1, reconstructed from the container list), the exhaustive
read-only proof in §5 (re-derived independently), the n = 14 stratification in §10a
(reproduced exactly), and the entire §10 dataset census (reproduced to the row, including
all eight confident-and-wrong run ids by name). §10 and §7 in particular are the work of
someone who actually opened the artifacts.

The failure is not sloppiness. It is **staleness in one direction**: the document describes
a repository that has since grown the very defences it says are absent. §12 — the section
carrying the doc's most load-bearing claims — was written before `eval/check_contamination.py`
and `eval/validity/` existed, and it now asserts the opposite of the code. A reader trusting
§12 would conclude the harness has no leakage gate, when it has two, one of which fails the
build today with nine findings.

Severity ranking of what needs fixing:

1. **§12's "no such check exists"** — false; two implementations at HEAD. Delete the
   paragraph, describe what was built instead.
2. **The leak's true breadth** — six faults, not one; three of them graded. This propagates
   into §10a, whose "uncontaminated subset" is not uncontaminated by the repo's own
   standard. Highest-consequence error in the doc.
3. **The missing `eval/validity/` subsystem** — 3,240 lines and 16 checks, unmentioned. On
   a document whose stated purpose is to describe this subsystem from source, this is the
   biggest gap by volume.
4. **§12's four retrieval/data errors** — distance floor (0.558, not 0.68), the cutoff
   firing in 9 of 13 runs rather than 1, "both" fault-9 legs when only one qualifies, and
   the corpus-leak mechanism contradicted by `graph.py:255-256`.
5. **§8's two errors** — blind grading is partial, not total (the queue leaks matched rubric
   phrases); eight overrides, not five, of which five lower a passing score.
6. **§9's trigger claim** and **§10a's "every correct answer"** overstatement.

Everything in §§1–7, 10, 11 can be relied on as written, with the small corrections noted
above. My estimate is that **roughly 20–25% of the document needs editing, nearly all of it
concentrated in §§8, 9, 10a and 12**, plus one new section covering the validity gate and
the contamination checker. The skeleton, the method and the evidence discipline are sound —
what it needs is to be brought forward to HEAD, not rebuilt.

One structural suggestion: the doc has now been overtaken by its own repository twice
(§12 versus `check_contamination.py`, and §8 versus its own §10a). A short
"generated-against commit `<sha>`" line at the top, plus running
`eval/check_contamination.py` and `eval/validity/run.py` before any revision, would turn
both of those from prose errors into build failures.
