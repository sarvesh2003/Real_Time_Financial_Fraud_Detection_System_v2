# MASTER.md — The LLM incident-investigation subsystem

Written from source and data files only. Every claim is checkable against the file path
and line number cited inline.

---

## 1. In one page

There is a fraud-detection pipeline running as Docker containers. This subsystem does
not run it — it *watches* it, breaks it on purpose, and measures how well a language
model works out what broke.

Three parts:

1. **Shell scripts that break the pipeline in a controlled way.** Each takes `inject` or
   `restore`. `inject` does one specific, reversible thing — stops a Redis container,
   caps its memory, adds 200 ms of network delay to one container. `restore` undoes it.
   Every call appends a line to a change-log file.

2. **A read-only investigator.** A program hands a language model a vague complaint
   ("Something seems off with the fraud pipeline, can you investigate?") plus six
   functions for inspecting the live system: container states, logs, metrics,
   configuration, and that change log. The model picks which to call, one at a time, up
   to 15 calls, then writes an incident report — a root-cause label, a summary, and a
   confidence level. Every turn is written to a file.

3. **A scoring harness.** A script checks each recorded report against a hand-written
   answer key for that fault and awards 2 (right), 1 (partly), 0 (wrong), or −1 (honestly
   said "I don't know"). A second script rolls those up into accuracy, calibration, cost,
   and retrieval statistics.

**What it produces:** recorded investigations under `eval/runs/`, one graded row each in
`eval/results.csv`, and a roll-up report — currently 38 graded investigations over 7
faults and 6 model configurations.

**Why:** the expensive on-call mistake is not "I don't know" but a confident wrong answer
that sends someone to restart the wrong thing. This measures how often that happens, on
faults whose true answer is known because the subsystem caused it.

**The pipeline is context only:** a Go gRPC ingest service (`go-server`) writing to
Kafka, a Go consumer (`go-enricher`) reading from it, a Python load generator, a 3-node
Redis cluster, Kafka + ZooKeeper, and two metric exporters, all in `docker-compose.yaml`.

---

## 2. Concepts you need

**MCP (Model Context Protocol).** A standard way to hand a language model a set of
functions it may call. You write ordinary Python functions; an MCP *server* publishes
their names, arguments, and docstrings; the model reads that list and decides which to
call and with what arguments. Here it is how the model reaches logs and metrics: it never
runs `docker` itself, it asks for a tool by name and the server runs the command and
returns text. The server is `mcp-server/server.py`, speaking over standard input/output
to whichever process launched it (`:513`).

**LangGraph.** A small Python library for writing a program as a labelled graph. You
define named steps ("nodes"), each an ordinary function taking a shared state dictionary
and returning updates to it, plus edges saying what runs next. An edge can be conditional
— a function inspects the state and returns the next node's name. It is a state machine
with a shared scratchpad. Four nodes are used here: `baseline`, `investigate`,
`tool_call`, `conclude` (`agent/graph.py:466-478`).

**The agent loop.** The cycle at the centre of it: show the model everything gathered so
far → it picks one function → run it → append the result → show it again. It stops when
the model says it is done or the budget runs out. "Agent" here means only "a loop that
lets a model choose its next tool call."

**Vector retrieval (and Chroma).** Looking things up by meaning rather than keyword. Each
stored document becomes a list of numbers (an *embedding*) such that documents about
similar things land numerically close together; a query is converted the same way and you
return the nearest stored documents. "Distance" is that gap — smaller means more similar.
Chroma is the local database holding the numbers and doing the search. Here it holds five
past investigation write-ups at `corpus/chroma_db/`, 384 numbers per document, using
Chroma's default embedding model. Example: a query built from "redis2 exited, throughput
collapsed" returns `redis/node_stopped` at distance 0.77 and `network/latency_injection`
at 0.99 — the first is much the better match.

**Prometheus and PromQL.** Prometheus scrapes numeric counters from every service every
few seconds and keeps them as time series — a named number with labels, sampled
repeatedly. It scrapes every 5 seconds here (`prometheus/prometheus.yml`). PromQL is its
query language: name a metric, optionally filter by label, optionally wrap it in a
function. `redis_evicted_keys_total` is the running count of keys Redis has thrown away;
`rate(fraud_server_transactions_total[1m])` is transactions per second averaged over each
one-minute span. The subsystem asks for a *range* — the value at up to 60 evenly spaced
points across a window — so trends are visible.

**tc / netem.** `tc` is the Linux traffic-control tool; `netem` is its network-emulation
mode. Together they attach a rule to a network interface that deliberately degrades
traffic: add a fixed delay, or drop a percentage of packets. Two faults use this to make
one container's network slow or lossy without touching the application.

**Wilson interval.** "4 of 7 right" gives 57%, but from seven trials the true rate could
plausibly be quite different. A Wilson interval converts "successes out of trials" into a
plausible range for the underlying rate, and behaves sensibly at the edges — unlike the
naive formula it never runs below 0% or above 100%, and does not collapse to zero width
at 0-for-7 or 7-for-7 (`eval/aggregate.py:24-31`). Fewer runs, wider range.

**Hit@1 and MRR.** Two ways to score a ranked list of search results. **Hit@1** is the
fraction of searches whose very first result was correct — pass/fail on the top slot.
**MRR** (mean reciprocal rank) is gentler: score 1 if the first correct result is at
position 1, 1/2 at position 2, 1/3 at position 3, 0 if absent, then average. A system
that reliably puts the right answer second scores Hit@1 = 0 but MRR = 0.5
(`eval/aggregate.py:258-272`).

---

## 3. The setup

**A "fault"** is a deliberate, scripted, reversible change to the running stack that makes
the pipeline misbehave in one known way. Each is a shell script under `fault-injection/`,
run as `bash <script> inject|restore`.

Every script sources `fault-injection/common.sh` for a `compose` wrapper pinned to the
repo's `docker-compose.yaml`, a `redis_cli` helper, and `log_change` (`:33-39`), which
appends one JSON line to `fault-injection/changelog.jsonl` — UTC timestamp, family,
script name, inject or restore. That file is the only record of deliberate change the
investigator can see, and what it gets shown is anonymised: a hash of the fault's name,
not the name itself.

Reversion is usually the simple inverse (`compose start`, `compose unpause`); some faults
self-resolve and need none. Two are harder and say so in comments: fault 12 must recreate
the container because `docker update` cannot clear an already-set CPU cap
(`network/12_cpu_throttle.sh:20-30`), and fault 3 falls back to restarting Redis because
the connection cap it set leaves no slot for the restoring command
(`redis/03_connection_pool_exhaustion.sh:20-30`).

| # | Fault | What `inject` does |
|---|---|---|
| 1 | redis/node_stopped | `compose stop redis2` |
| 2 | redis/eviction_storm | redis1 `maxmemory=5mb` + `allkeys-lru`, floods 200 000 keys to force eviction |
| 3 | redis/connection_pool_exhaustion | redis1 `maxclients=5` — new connections refused |
| 4 | redis/slow_commands | `DEBUG SLEEP 10` on redis1, blocking its single-threaded event loop |
| 5 | kafka/consumer_scaled_down | `go-enricher` 3 → 1 replica, then a 60 000-request burst via `ghz` |
| 6 | kafka/rebalance_churn | Restarts `go-enricher` 6 times, 5 s apart, so the group never settles |
| 7 | kafka/partition_imbalance | 500 transactions for one UUID user id — all hit one partition |
| 8 | kafka/malformed_event | Publishes one plain-text (non-protobuf) message onto `raw_transactions` |
| 9 | network/latency_injection | 200 ms `tc netem delay` on go-server's `eth0` via a `netshoot` sidecar |
| 10 | network/packet_loss | Same sidecar, `tc netem loss 10%` on go-server's `eth0` |
| 11 | network/container_paused | `compose pause go-server` — frozen, but still shows as up |
| 12 | network/cpu_throttle | `docker update --cpus=0.1` on go-server |
| 13 | config/client_timeout_too_low | Recreates go-server with Redis dial/read/write timeouts at 1 ms |
| 14 | config/pool_size_too_small | Recreates go-server with `REDIS_POOL_SIZE=2` |
| 15 | config/bad_retry_backoff | Recreates go-server with 20 retries, zero backoff — latent, no symptom alone |
| 16 | config/wrong_maxmemory_policy | redis1 capped at 5 MB with `noeviction`, then flooded — writes rejected |

The config faults export an environment variable and run
`compose up -d --force-recreate go-server`; the compose file interpolates it into the
container, defaulting to empty when unset.

**Code/data disagreement:** fault 5 has a script but appears in neither the answer key
(`eval/rubric.yaml` lists 1–4 and 6–16, 15 entries) nor the harness's fault table
(`eval/run_fault_eval.sh:34-50`, the same 15). It cannot be selected and has no grading
entry.

---

## 4. Architecture

```
   eval/run_fault_eval.sh  ──inject──▶  fault-injection/<family>/NN_*.sh
            │                                       │
            │                                       ▼
            │                          ==== Docker stack ====
            │                     go-server, go-enricher, kafka,
            │                     redis1/2/3, prometheus, exporters
            │                             ▲            ▲
            └──launch──▶ agent/run.py     │ docker CLI │ HTTP
                              │           │            │
                              ▼        mcp-server/server.py  ◀── changelog.jsonl
                     agent/graph.py       (6 read-only tools, stdio)
                baseline ▶ investigate ⇄ tool_call         ▲
                                │                          │
                                ▼           corpus/chroma_db/ (past cases)
                            conclude
                                │
                                ▼
            eval/runs/faultNN/*.trace.jsonl + *.result.json
                                │
                 eval/grade.py ─▶ eval/results.csv ─▶ eval/aggregate.py ─▶ summary.md
```

`eval/run_fault_eval.sh` copies the live change log aside as `changelog_prior.jsonl` and
blanks it, so no residue from the previous fault is visible (`:69-75`); it injects, then
launches `agent/run.py`. That starts the MCP server as a child process over standard
input/output (`agent/run.py:204-215`), opens the Chroma index (`:217-218`), and builds
the graph. `baseline` runs a fixed sweep of tool calls for free; `investigate` shows the
model the whole evidence log and asks for one action; `tool_call` runs it and loops back;
`conclude` asks for the final report. Two files are written — `<RUN_TAG>.trace.jsonl`
(one JSON line per event) and `<RUN_TAG>.result.json` (a flat row).

---

## 5. The tools

All six are in `mcp-server/server.py`.

| Tool | Arguments | Runs underneath | Returns |
|---|---|---|---|
| `query_logs` | `service`, `since`, `level?`, `grep?` | `docker compose logs --no-color --since=<since> -- <service>` (`:102`) | Matching lines, newest last, capped at the newest 500; a distinct message when a filter matched 0 of N lines |
| `get_metrics` | `promql`, `window` | GET `{PROMETHEUS_URL}/api/v1/query_range` (`:199`), step `max(5, window/60)` | Per label combination: labels, then up to 60 `timestamp:value` pairs. On an empty result it re-queries over ≥6 h to tell "never existed" from "stopped recently" (`:213-254`) |
| `list_metric_names` | `job?`, `contains?` | GET `/api/v1/label/__name__/values` (`:295`) | One metric name per line, sorted |
| `get_container_state` | *(none)* | `docker compose ps --all --format json` (`:325`) | Per container: service, state, status string, health |
| `read_config` | `service` | redis1/2/3: `docker compose exec -T -- <svc> redis-cli CONFIG GET <key>` for five fixed keys (`:27`, `:360`). Else `docker compose ps -q` + `docker inspect --format '{{json .Config.Env}}'` (`:376-392`) | Redis: those five settings. Apps: env vars starting `REDIS_`/`KAFKA_`/`GRPC_`, minus any name containing KEY/SECRET/PASSWORD/TOKEN/LICENSE (`:29-30`, `:406-409`) |
| `list_recent_changes` | `window` | Reads `fault-injection/changelog.jsonl` (`:487`) | `<timestamp> \| change=CHG-<8 hex> \| inject\|restore`. The ID is `sha256("<family>/<fault>")[:8]` (`:502-505`) — stable per fault, and it never says *what* changed |

The docstrings are unusually long on purpose, warning about traps: `query_logs` notes
that most lines carry no severity prefix, so `level="ERROR"` can return nothing while
things are badly broken (`:73-82`); `get_metrics` notes that a misspelled metric name and
a real metric with no data return the identical empty result (`:169-180`).

**Can any tool modify the target system? No.** What I checked: every external call in the
file. There are exactly two `subprocess.run` sites — the shared `run_compose` helper
(`:45`) and one `docker inspect` (`:384`) — plus three `requests.get` calls and one file
read. `run_compose` is only ever invoked with `logs` (`:102`), `ps --all --format json`
(`:325`), `ps -q` (`:376`), and `exec -T -- <redis-node> redis-cli CONFIG GET <key>`
(`:360`). That last is the only write-capable channel, but the command is hard-coded to
`CONFIG GET`, the key comes from a fixed five-element list (`:27`), and the service must
be redis1/2/3 (`:26`, `:443`). The three HTTP calls are `GET` against Prometheus read
endpoints; the change log is opened read-only (`:487`). No write, delete, restart, or
config-set path exists in the file.

---

## 6. The agent

**Structure.** `START → baseline → investigate`; `investigate` branches conditionally to
`tool_call` or `conclude`; `tool_call → investigate`; `conclude → END`
(`agent/graph.py:471-478`).

**State.** One dictionary carried between nodes (`:47-54`): `trigger`, `evidence_log`
(append-only list of `{tool_name, args, result}`), `calls_used`, `pending_action`,
`conclusion`, `trace`.

**`baseline` — the free sweep.** Before the model gets a turn the graph runs a fixed set
of calls itself: `get_container_state`, `list_recent_changes(24h)`,
`get_metrics("up","1h")`, then `read_config` for *every* running service (`:217-236`).
None count against the budget, so no run starts blind and every run starts from identical
evidence — differences come from what the model chose next.

**`config_diff` — a synthetic evidence entry.** After the sweep, `peer_config_diff` groups
services whose names differ only by a trailing digit (redis1/2/3) and appends any setting
that is not identical across the group to the evidence log, as though a tool named
`config_diff` had returned it (`:159-180`, `:233-236`). This does the "compare identical
peers" step for the model. It is not an MCP tool and cannot be called, but it does count
as a "distinct tool" in the corroboration rule below.

**`investigate` — one turn.** Builds a single prompt holding the trigger, the numbered
evidence log (each result truncated to 2 000 characters), any retrieved past-case names,
and the remaining budget (`:243-291`). This is **not** a growing chat transcript: each
turn is a fresh two-message conversation (system prompt + one rebuilt user message). The
evidence log *is* the memory.

**Retrieval.** The search text is the trigger plus the first 300 characters of each of the
last three evidence results; it fetches the three nearest past cases, keeps those with
distance ≤ 1.0, and shows only family and name — never the stored root cause (`:250-256`).
`RETRIEVAL_MODE=off` skips it entirely. `corpus/build_index.py` embeds each record's
symptoms and investigation path but excludes its `root_cause`, since indexing the answer
would make retrieval circular (`:36-39`).

**Limits.** Budget is 15 tool calls (`:22`), checked *before* the model's action is
honoured, so the 16th request is discarded and the run concludes (`:344-350`). An
identical `(tool_name, args)` pair returns `"DUPLICATE: identical call already made"`
without re-running anything, and still costs a call (`:355-362`). A reply cut off
mid-emission (`finish_reason == "length"`) or a completely empty one is retried once
rather than read as "the model chose to conclude" (`:304-324`).

**`conclude`, and the logic that overrides the model.** The model must return JSON with
`root_cause_id`, `summary`, `confidence` (`confirmed`/`best_guess`/`inconclusive`),
`reasoning`, and `cited_evidence_indices` (`:60-73`), under a grounding rule: every
specific name, number, or timestamp must appear verbatim in the evidence log, and "a
correct vague report scores better than a specific wrong one" (`:405-411`). The code then
**overrides the model's own confidence** (`:435-448`):

- `calls_used >= 15` and the model said `confirmed` → `best_guess`, reason
  `budget_exhausted`. A run that ran out of road did not stop because it was satisfied.
- Otherwise, `confirmed` while the cited evidence spans fewer than 2 *distinct* tools →
  `best_guess`, reason `corroboration`. Two `query_logs` calls do not corroborate each
  other; `get_metrics` plus `read_config` do.

Both values are kept (`confidence`, `model_self_reported_confidence`) so the gate's effect
is measurable. Note what it does not check: correctness. Unparseable final JSON is
recorded as `root_cause_id: "PARSE_FAILURE"` with confidence `inconclusive` rather than
crashing (`:416-433`).

**The system prompt** (`agent/system_prompt.md`) is operational, not documentation. It
states the topology, lists four alarming-but-normal things (the generator's
`HIGH VELOCITY` alerts, a cosmetic Go format-verb bug, a one-shot init container exiting
0, the ~4 tx/s baseline), and gives seven investigation rules — chiefly: survey before
narrowing, treat an empty result as a missed query rather than proof of health, diff
identical peers, and time-check evidence against the latest change-log entry so a stale
incident from an earlier test is not mistaken for the current one.

**Model plumbing and derived measures.** `agent/run.py:27-33` hardcodes an
Ollama-compatible OpenAI endpoint; sampling is `temperature=0.2`, `timeout=120`,
`max_retries=2`, `max_tokens=8192`, plus `reasoning_effort="none"` for any "qwen" model id
(`:191-201`). `build_result` (`:104-171`) counts duplicate calls, tool errors, calls to
nonexistent tools and empty results, and computes `cited_baseline_fraction` and
`evidence_groundedness` — the fraction of digit-containing tokens longer than three
characters in the summary that also appear in the evidence text (`:130-139`). A crude
hallucination check: it catches invented numbers, not invented reasoning.

---

## 7. A worked example

**Run:** `fault2_kaggle-qwen14b_ragoff`, 2026-09-10 23:07:23 UTC — fault 2 (Redis eviction
storm), model `qwen2.5:14b-instruct`, retrieval **off**. Files:
`eval/runs/fault02/fault2_kaggle-qwen14b_ragoff.{trace.jsonl,result.json}`. The trigger
was the standard one, with no hint of what or where.

**The free sweep** produced 16 evidence entries; three mattered. `get_container_state`
showed everything running, with one oddity: every container "Up 8 hours" except `redis2`,
"Up 2 hours". `list_recent_changes(24h)` returned one line:

```
2026-09-10T20:56:38Z | change=CHG-9a260cd9 | inject
```

An inject with no matching restore — something is deliberately broken right now, and the
tool refuses to say what. (`CHG-9a260cd9` is `sha256("redis/02_eviction_storm")` truncated
to 8 hex characters; I recomputed it to confirm.) Then `read_config` ran against all 12
running services and `config_diff` fired:

```
maxmemory differs across redis1, redis2, redis3: redis1=5242880, redis2=0, redis3=0
maxmemory-policy differs across redis1, redis2, redis3: redis1=allkeys-lru, redis2=noeviction, redis3=noeviction
```

Three identical nodes, one different. The answer is essentially sitting in the free sweep.

**The model then spent 10 of its 15 calls.** A real one, its 6th:

```json
{"event": "action_selected", "action": {"type": "tool_call", "name": "get_metrics",
 "args": {"promql": "redis_evicted_keys_total", "window": "1h"}}}
```

The response, abbreviated (the real line carries 12 points):

```
__name__="redis_evicted_keys_total", instance="redis-exporter:9121", job="redis"
 -> 1789080786:312445, 1789080846:312445, 1789080906:312445, 1789080966:312511,
    1789081026:312682, ... 1789081446:313534
```

Flat at 312 445 for three samples, then climbing continuously — eviction is happening now,
not historically. The other eight calls were weaker: log sweeps returning routine save
chatter, one `level="ERROR"` filter that came back
`"(0 of 18689 log lines ... this is a filter miss, not silence.)"`, and two repeats of
earlier calls that returned `"DUPLICATE: identical call already made"` — two of fifteen
burned for nothing.

**The conclusion,** verbatim from the trace:

> **root_cause_id:** `redis1-maxmemory-misconfigured`
> **summary:** "redis1 has a maxmemory setting of 5MB, while redis2 and redis3 have no
> maxmemory limit. This misconfiguration causes redis1 to evict keys aggressively, leading
> to high redis_evicted_keys_total counts. The other nodes have no such eviction,
> indicating redis1 is the outlier."
> **confidence:** `confirmed` — **cited evidence:** `[15, 21]` —
> **distinct supporting tools:** `["config_diff", "get_metrics"]`

Two distinct tools and budget unspent, so the confidence gate did not fire and `confirmed`
stood. Grading gave it **2**. It cost 479.6 s wall clock, 12 model calls, 106 314 input
tokens and 2 085 output tokens; half the cited evidence came from the free sweep, and
`evidence_groundedness` was 1.0.

---

## 8. How a run gets scored

### The rubric and the mechanical scorer

`eval/rubric.yaml` holds one entry per fault, listing phrase sets: `mechanism_any_of`
(*what went wrong*), an optional `mechanism_and_any_of` (a second required idea),
`target_any_of` (*where*), `disqualifiers` (phrases marking a specific wrong diagnosis),
and an optional `partial_credit_any_of`. `eval/grade.py` concatenates the run's `summary`
and `reasoning` and does case-insensitive literal substring matching (`:38-42`, `:46`).
Scores (`:75-98`):

| Score | Meaning | Condition |
|---|---|---|
| **2** | Correct | mechanism hit, plus `mechanism_and` if defined, plus target hit, no disqualifier |
| **1** | Partial | only mechanism, or only target, or a partial-credit phrase |
| **0** | Wrong | a disqualifier fired, or nothing relevant matched |
| **−1** | Honest abstention | `confidence == "inconclusive"` **and** no root cause claimed |

−1 is tracked apart from 0 deliberately: an abstention is not a wrong answer and must not
count as a false positive (`eval/rubric.yaml:15-17`).

Disqualifier lists are narrower than they look, and the file says why. Fault 2's
wrong-node disqualifier is not bare `"redis2"` — every correct answer names redis2 as the
healthy peer it diffed against — but phrases like `"redis2 is the outlier"` (`:61-66`).
Fault 1 goes the other way: `redis1`/`redis3` are removed from valid targets, because the
script always stops redis2, so naming another node is a *wrong* instance rather than an
acceptably vague one (`:35-37`).

Each fault also carries `split` (`indexed` / `held_out_with_neighbor` /
`held_out_no_neighbor` — whether a similar case is in the corpus), `liveness`
(`continuous` vs `transient`), and `baseline_sufficiency` (`sufficient` / `hinted` /
`absent` — whether the free sweep alone holds the answer).

### Ambiguous cases and human correction

Four situations are never silently scored: the run did not complete, the JSON failed to
parse, a manual template was left unfilled, or a disqualifier fired *at the same time* as
a real mechanism or target hit (`eval/grade.py:75-90`). Each sets `needs_review` and
writes the row to `eval/grading_queue.csv` **blind** — the reviewer sees the model's
summary and reasoning, the disqualifier's short id, and the auto-score, but not the
fault's identity or the rubric's answer phrases (`:166-177`).

To correct a score, add a line to `eval/score_overrides.csv` (`run_id,score,note`) and
re-run `grade.py`. Overrides apply after auto-scoring, clear the review flag, and are the
only correction that survives, since `results.csv` is rebuilt from scratch each time
(`:109-117`, `:153-157`). The five on file each carry a rationale; two *lower* a
mechanically-passing score. Separately, `eval/llm_judge.py` is an independent second
grader: it sends each run's summary, reasoning and transcript to a Claude model with a
hand-verified description of the fault's real mechanism and asks for a verdict plus a
calibration judgement. It covers faults 6, 9 and 13 only (`:46-77`) and never feeds
`results.csv`.

### Every metric the aggregator computes

From `eval/aggregate.py`, computed per environment-and-retrieval-mode group, never pooled.

| Metric | Question it answers | How to read it |
|---|---|---|
| **Raw accuracy** | Share of runs scoring 2 | Wilson 95% interval; abstentions count as failures |
| **Coverage** | Share ending in `confirmed` | How often it commits at all |
| **Selective accuracy** | Of confirmed runs, share correct | How often you'd be right acting only on confident answers |
| **False-confidence rate** | Share of *all* runs confident and wrong | The number the design targets; abstentions excluded |
| **Confirmed-recall** | Of correct answers, share flagged confident | Catches over-hedging; denominator is every score-2 row (`:86-87`) |
| **Reliability table** | Per confidence tier: n and n correct | Labels are useful only if accuracy falls down the table |
| **Downgrade ablation** | Effect of the confidence gate | Rate with vs without it, split corroboration / budget-exhaustion / untracked (`:107-118`) |
| **Contrast-pair accuracy** | Can it tell look-alike faults apart? | Strict — both faults in the pair must score 2 |
| **Retrieval by split** | Did the lookup find the right past case? | Hit@1 and MRR, per split (`:224-233`). On `indexed` faults ~100% means leakage; on `held_out_no_neighbor` finding nothing is correct, so a hit is a false positive |
| **Cost-weighted skill score** | Better than always abstaining? | `1 − (system cost / always-abstain cost)`; 0 = no better, 1 = perfect, negative = harmful |
| **Sensitivity sweep** | Were the weights chosen to flatter? | Recomputes at confirmed-wrong 5×/10×/20×; stable ranking = no |
| **Scripted-baseline-only accuracy** | What the free sweep alone solved | Of `baseline_sufficiency: sufficient` faults, share scoring 2 citing only baseline evidence (`:131-134`) — the floor to beat |
| **Efficiency set** | Working economically and honestly? | Mean calls-to-resolution (correct answers only), cited-baseline fraction, evidence groundedness, per-run duplicate / tool-error / hallucinated-call rates |
| **Cost set** | What the run cost | Mean wall clock, tokens in/out, tokens per second |

Cost cells are engineer-minutes-equivalent: confirmed-correct 0, best_guess-correct 1,
inconclusive 3, best_guess-wrong 5, confirmed-wrong 10 (`eval/cost_matrix.yaml:20-25`);
only ratios matter. A crashed run is priced at 1.5× inconclusive (`:196-197`) — worse than
abstaining, because it leaves no answer *and* no evidence.

---

## 9. What was actually varied

Read from `eval/results.csv` and the trace headers.

| Variable | Values present |
|---|---|
| Fault | 1, 2, 3, 4, 6, 9, 13 |
| Model / environment | `kaggle-qwen14b` (qwen2.5:14b-instruct), `kaggle-qwen32b` (qwen2.5:32b-instruct), `local-qwen9b` (qwen3.5:9b), `claude-desktop-opus5`, `-sonnet5`, `-haiku4.5` |
| Retrieval | `on` / `off` on every automated run; blank on all Claude Desktop rows |
| Host | Kaggle notebooks behind six different ngrok URLs, plus one LAN address `http://100.103.190.22:11434/v1` |

**Held constant:** the trigger text (one distinct value across all 38 rows); the tool set,
baseline sweep, 15-call budget, 2-distinct-tool threshold, 1.0 distance cutoff, and system
prompt (constants at `agent/graph.py:22-26`); sampling parameters; the five-document
corpus; and the per-fault change-log rotation.

**Confounded:** model size and host. The 9B model ran only on the LAN address, the 14B/32B
only on Kaggle. Only the 14B model covers all seven faults — 32B covers 6/9/13, the 9B
covers 1/2/3 — so no two models were compared on the same fault set.

**Harness/data mismatches.** The harness runs the manual leg for two Claude models
(`eval/run_fault_eval.sh:128`), yet the data holds `claude-desktop-opus5` rows for faults
1 and 2. `eval/env_manifest.json`, meant to pin each environment's configuration before a
sweep, is unfilled — `written_at` is still `"REPLACE_BEFORE_RUN"` — and lists two
environments out of six. And the harness launches one automated environment per
invocation, so multi-environment coverage came from repeated runs, not one sweep.

---

## 10. The dataset as it stands

**38 graded runs**, all `status: completed`; no crashes and no parse failures among them.

| Outcome | n | | Enforced confidence | n |
|---|---|---|---|---|
| 2 — correct | 12 | | confirmed | 17 |
| 1 — partial | 4 | | best_guess | 11 |
| 0 — wrong | 12 | | inconclusive | 10 |
| −1 — abstention | 10 | | | |

Confident **and** correct: 9 rows. Confident and wrong (score 0 or 1): 8 rows —
`fault1_local-qwen9b_ragoff`, `fault1_local-qwen9b_ragon`,
`fault2_claude-desktop-haiku4.5`, `fault2_local-qwen9b_ragon`,
`fault3_local-qwen9b_ragoff`, `fault3_local-qwen9b_ragon`, `fault4_kaggle-qwen14b_ragon`,
`fault6_claude-desktop-sonnet5`. (The aggregator reports this per group, not pooled.)

| Environment | n | 2 / 1 / 0 / −1 | | Fault | Family | Runs |
|---|---|---|---|---|---|---|
| kaggle-qwen14b | 14 | 4 / 2 / 4 / 4 | | 1 node_stopped | redis | 7 |
| kaggle-qwen32b | 6 | 0 / 1 / 0 / 5 | | 2 eviction_storm | redis | 7 |
| local-qwen9b | 6 | 0 / 0 / 6 / 0 | | 3 connection_pool_exhaustion | redis | 4 |
| claude-desktop-sonnet5 | 5 | 4 / 0 / 1 / 0 | | 4 slow_commands | redis | 2 |
| claude-desktop-haiku4.5 | 5 | 2 / 1 / 1 / 1 | | 6 rebalance_churn | kafka | 6 |
| claude-desktop-opus5 | 2 | 2 / 0 / 0 / 0 | | 9 latency_injection | network | 6 |
| | | | | 13 client_timeout_too_low | config | 6 |

By family: redis 20, kafka 6, network 6, config 6. Retrieval: 13 on, 13 off, 12 blank.
Five rows are flagged `needs_review` and unresolved; eight others were closed by an
override. Three of those eight are the zero-tool-call contamination rows described in
§12; `local-qwen9b` scores 0 across all six of its runs once they are removed.

**Excluded run directories.** 56 result files exist under `eval/runs/`; 38 are graded.
`eval/grade.py:131` globs `fault[0-9][0-9]/*.result.json` — literally "fault" plus two
digits, one directory level deep — and its comment explains why: a looser `fault*` would
sweep in archive folders left by re-runs and silently double-count. Excluded by the suffix
after the two digits: `fault02_contaminated_run1/` (4 files),
`fault02_pre_doctrine_fix/` (4), `fault03_pre_readconfig_fix/` (4), `fault09_v2/` (2),
`fault13_v2/` (2). Excluded for being nested a level deeper than the glob reaches:
`fault09/_contaminated_stackrestart/` (2). The names encode the reason (contaminated
environment, or a run predating a fix), but nothing in the code records a per-directory
justification, so the *stated* reason is unclear from code alone.

---

## 10a. What can actually be claimed

Section 10 describes the dataset as collected. This section describes what survives the
defects in section 12. The short version: no pooled accuracy figure over all 38 rows
means anything, but the dataset is not empty either. Stratifying is more informative than
suppressing, so the exclusions are listed explicitly and the remainder is reported with
its limits attached.

### The uncontaminated subset

Removing every row with a named validity defect:

| Excluded because | Rows |
|---|---|
| prompt was truncated, run never received its inputs | 6 |
| answer present in the free baseline sweep (faults 1, 2, 3) | 12 |
| hand-transcribed, different scaffold, no system prompt or sweep | 12 |
| scored with zero tool calls | 3 |
| flagged `needs_review` and never resolved | 5 |

Rows carry more than one reason, so these do not sum. **24 of 38 rows are excluded,
leaving n = 14**, covering faults 4, 6, 9 and 13 on the two hosted Qwen environments.

| Outcome | n |
|---|---|
| 2, correct | **0** |
| 1, partial | 2 |
| 0, wrong | 3 |
| −1, abstained | 9 |

**The agent solved none of the faults that were not contaminated.** That is the honest
headline, and it is consistent with the earlier per-family reading: every correct answer
in the full dataset sits on a fault whose answer was reachable without investigating.

The one genuinely positive result is the abstention rate. **9 of 14 runs, 64%, declined to
name a cause rather than naming a wrong one.** The confidence machinery was built to make
a wrong confident answer expensive, and on the clean subset it behaves that way: the
system fails safe. Note the limit of that claim. It says the agent knows when it has
nothing; it says nothing about whether it can find something.

### The null model

The six truncated runs are not only an invalid arm. They are an accidental control.
Having received the system prompt and nothing else, they made zero tool calls and answered
from prior knowledge alone. That is a no-work floor, and it was run against the same
rubric as everything else.

| Grading | Result |
|---|---|
| as the rubric originally scored it | **2 of 6 correct** |
| after the section 12 corrections | 0 of 6 |

**A model that did no work scored 33% under mechanical grading.** That number is the floor
any real result has to clear, and it is the strongest single argument that the original
accuracy figures were measuring prior knowledge of Redis rather than investigation. It is
also the one figure contamination cannot inflate, since a null model is already fully
exposed to every leak in the system.

### The grader itself

`eval/score_overrides.csv` is a record of a human reading a run and disagreeing with the
mechanical score. Treated as an instrument characterization rather than a correction log,
it gives the grader's error rate directly:

**7 disagreements over 38 runs, about 18%, in both directions.** Two runs the rubric
wrongly zeroed, where a disqualifier fired on a confounder the model had explicitly ruled
out. Five it wrongly credited, on prompt recitation, a phrase matched inside a negation,
and an incidental service name on an unrelated subsystem. Each case carries its reasoning
in the `note` column.

This is what keeps the numbers above honest: they rest on an instrument whose error rate
is stated rather than assumed.

---

## 11. How to run it

**Build the retrieval index** (only if `corpus/traces.jsonl` changed; it deletes and
recreates the collection each time — `corpus/build_index.py:49-53`):

```bash
python corpus/build_index.py
```

**Run one fault end to end:**

```bash
export KAGGLE_OLLAMA_URL="https://<current-ngrok-id>.ngrok-free.app/v1"  # required, must end in /v1
export KAGGLE_MODEL="qwen2.5:14b-instruct"    # optional, default qwen2.5:32b-instruct
export KAGGLE_ENV_LABEL="kaggle-qwen14b"      # optional, default kaggle-qwen32b
./eval/run_fault_eval.sh 2
```

It rotates the change log, injects, runs both retrieval legs (in parallel for continuous
faults; re-injecting before each leg for transient ones), blocks on `read -p` while you do
the manual Claude Desktop leg, then restores. It deliberately avoids `set -e` so a failing
leg cannot skip the restore and leave the fault live for every later fault (`:14-20`).
Output lands in `eval/runs/fault<NN>/`.

**Run the agent alone:**

```bash
OLLAMA_BASE_URL="https://.../v1" OLLAMA_MODEL="qwen2.5:14b-instruct" \
RUN_TAG=myrun RESULTS_DIR=/tmp/out ENV_LABEL=local RETRIEVAL_MODE=on \
FAULT_ID=2 FAULT_FAMILY=redis FAULT_NAME=eviction_storm \
agent/venv/Scripts/python.exe agent/run.py "optional trigger text"
```

`OLLAMA_BASE_URL` is the only required variable (`agent/run.py:32-33`); `.env` at the repo
root is loaded first. The MCP server is launched automatically via
`mcp-server/venv/Scripts/python.exe` (`:204-213`) and reads `PROMETHEUS_URL`, default
`http://localhost:9090` (`mcp-server/server.py:21`).

**Call one tool by hand; grade; aggregate; optionally judge** (the judge needs
`ANTHROPIC_API_KEY`, and `LLM_JUDGE_MODEL` overrides its default `claude-sonnet-5`):

```bash
mcp-server/venv/Scripts/python.exe mcp-server/test_client.py query_logs '{"service":"go-server","since":"10m"}'
venv/Scripts/python.exe eval/grade.py       # -> eval/results.csv (+ grading_queue.csv)
# resolve queued rows via eval/score_overrides.csv, then re-run grade.py
venv/Scripts/python.exe eval/aggregate.py   # -> eval/summary.md
agent/venv/Scripts/python.exe eval/llm_judge.py 6 9 13
```

Three virtual environments are referenced: `agent/venv`, `mcp-server/venv`, and a
repo-root `venv` for the eval scripts.

---

## 12. Limitations visible in the code

**The system prompt contains the answer to a graded fault.** `agent/system_prompt.md:16`
states outright that with `cluster-require-full-coverage=yes` and zero replicas, losing
any single Redis node takes the whole cluster down with `CLUSTERDOWN`. That is fault 1's
entire mechanism. The rubric for fault 1 (`eval/rubric.yaml:27-44`) accepts
`mechanism_any_of: "down"` and `target_any_of: "redis cluster"`, both of which are
answerable from that one prompt line with no evidence at all. The eval was therefore
partly measuring what the agent had been told rather than what it found.

Three runs prove it happened rather than merely being possible. All three have
`calls_used: 0` and an empty `tool_call_sequence`, meaning the agent never probed the
system, yet all three were auto-scored above 0:

| Run | Auto | Why it scored |
|---|---|---|
| `fault1_local-qwen9b_ragon` | 2 | reasoning opens "The system architecture explicitly states that with 'cluster-require-full-coverage=yes'..." and concedes the node failure is only "implied" |
| `fault1_local-qwen9b_ragoff` | 2 | same recitation, and names **redis1** as the failed node when the script always stops redis2 |
| `fault2_local-qwen9b_ragoff` | 1 | answered fault 1's CLUSTERDOWN story on the eviction-storm fault, matching `maxmemory` on the substring inside "rather than a misconfiguration of maxmemory" |

The `wrong_node` disqualifier did not save the second case: it lists `"redis1 is down"`,
and the run wrote `"(e.g., redis1)"`. All three are now overridden to 0 in
`eval/score_overrides.csv`, which drops `local-qwen9b` to 0 correct across all six of its
runs and raises confident-and-wrong from 6 rows to 8.

Two things follow. First, any Redis accuracy figure computed before those overrides is
inflated, and fault 1 is the fault most affected, so per-family comparisons that treat
Redis as the agent's strong family should be read with that in mind. Second, **nothing in
the harness checks for this automatically.** It was found by noticing that a run could
score full marks with an empty tool-call sequence, then reading the prompt and the rubric
side by side. A check that fails when any rubric accept-phrase also appears in the system
prompt or an MCP docstring does not exist and would be the correct fix.

**The retrieval corpus leaks the answers too, and nobody had checked it.** The five
documents in `corpus/traces.jsonl` were written as past-incident write-ups for the same
fault families the eval injects, so they state the mechanisms in the rubric's own
vocabulary. Running `grade.py`'s matcher over each document against every fault's accept
set:

| Corpus document | Faults whose full score-2 accept set it contains |
|---|---|
| `latency_injection` | 1, 2, 3, 8, 9, 14, 15, 16 |
| `container_paused` | 2, 3, 7, 9, 11, 14, 16 |
| `malformed_event` | 6, 8, 11, 14 |
| `eviction_storm` | 1, 2, 14, 16 |
| `node_stopped` | 1, 2, 16 |

Every document is a complete answer key for several faults, including faults it was not
written about. The consequence is that the retrieval ablation never measured retrieval:
turning the corpus on turns an answer key on. Any run that retrieved a document and then
scored is unattributable between "the corpus helped it investigate" and "the corpus told
it the answer," and the two are indistinguishable from the artifacts. **No retrieval
result from this dataset is claimable**, which supersedes the earlier reading that the
ablation merely showed no lift.

This is also the clearest single illustration of why the free-context scan has to cover
every source, not just the obvious one. The system prompt was found first because it is
the thing you think of; the corpus is the thing you build precisely so the model can read
it, which is exactly what makes it easy to forget.

The same class of leak exists once more at HEAD and is not in the graded data:
`mcp-server/server.py:439-441` tells the model that "millisecond-scale timeouts below a
real network round trip make a perfectly healthy dependency look like it is failing,"
which is fault 13's mechanism. It was added in commit `30df3e0`, after every graded run,
and the arm that used it was abandoned in `1a7f68c`. No result in `results.csv` saw it,
but it is still in the tool docstring and should be removed before any re-run.

**Grading is literal substring matching.** `eval/grade.py:38-42` runs
`re.search(re.escape(phrase), text, IGNORECASE)`; it cannot tell a phrase used as the
claim from the same phrase used as a ruled-out confounder. Three of the eight entries in
`eval/score_overrides.csv` correct exactly that, in both directions — including
`fault6_kaggle-qwen14b_ragon`, which scored 1 because "go-enricher" appeared incidentally
while the actual claim was "the Redis cluster appears to be down" on a Kafka fault.
Disqualifier lists are likewise phrase-shaped: fault 6's has `"redis is down"` and
`"redis failure"` but not `"redis cluster ... down"`, so it did not fire
(`eval/rubric.yaml:112`).

**The corroboration gate checks breadth, not correctness.** `agent/graph.py:438` only
counts distinct tool names among cited evidence, so a wrong answer citing three tools
keeps `confirmed`; `fault6_claude-desktop-sonnet5` is that case, and its own
`_grading_note` says so. `config_diff` also counts toward the gate despite being
synthesised by the graph rather than fetched by the model — the §7 example passed on
`config_diff` + `get_metrics`, one of which the model never chose.

**Retrieval is scored on runs that never retrieved.** `eval/aggregate.py:253-256`
increments the `checked` denominator before testing whether anything came back, so a
retrieval-off group reports Hit@1 = 0/n rather than being excluded.

**Contrast-pair accuracy is near-empty here.** Of the six pairs at
`eval/rubric.yaml:287-293`, only `[13, 4]` has both members present, and only in the two
`kaggle-qwen14b` groups; every other group reports 0/0. `eval/aggregate.py:179` also keys
a dictionary by fault id, so a group holding two rows for one fault keeps only the last.

**The change ID is not opaque.** It is `sha256("<family>/<script_name>")[:8]`
(`mcp-server/server.py:502-505`) over a 16-element known set — I recomputed and matched
every ID in the traces. Anyone with the repo can reverse one in a line of Python.

**The corpus is five documents,** covering faults 2, 1, 8, 9, and 11 — the five `indexed`
faults. On any other fault the nearest neighbours necessarily come from the wrong fault,
and the 1.0 distance cutoff rarely removes them: recorded distances cluster between 0.68
and 0.99, and only `fault4_kaggle-qwen14b_ragon` had the cutoff trim the list at all — to
one entry, `network/container_paused`, for a Redis slow-command fault.

**Claude Desktop rows are hand-transcribed** into `eval/claude_desktop_template.json`;
`calls_used` is a human count and the confidence mapping is applied by hand. Four of the
five graded Claude Desktop faults have a row still flagged `needs_review`, and three rows
have an empty `calls_used`, so timing, token, and call-count metrics are blank for them.

**An empty model response was scored as a decision to conclude, in recorded data.** The
guard at `agent/graph.py:304-324` retries an empty reply, but it postdates the dataset.
`eval/runs/fault09/fault9_kaggle-qwen32b_ragoff.trace.jsonl` shows the failure it was
written for: a second turn with `llm_ms: 107124`, `input_tokens: 0`, `output_tokens: 0`,
`finish_reason: null`. The graph read that as "the model chose to conclude," ended the run
after 1 tool call, and it was graded −1. Both fault-9 qwen32b legs show this, and neither
was re-run after the guard was added.

**Budget accounting is asymmetric.** The baseline sweep is 15–16 free tool calls, while a
duplicate returning nothing new costs a full call — a run can make ~30 real tool
invocations while reporting `calls_used: 15`. At the other extreme, all six `local-qwen9b`
rows have `calls_used: 0` and a single model turn: they concluded straight off the
baseline sweep, and three of them auto-scored above 0 by reciting the system prompt (see
the top of this section) before being overridden to 0. Any "calls to resolution" figure
including these rows measures the sweep, not the model.

**A single provider is hardcoded.** `agent/run.py:27-33` refuses to start without
`OLLAMA_BASE_URL`; the `claude-desktop` rows exist only because a person ran those models
by hand and typed the results into a template.
