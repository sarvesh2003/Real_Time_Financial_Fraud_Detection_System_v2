# Independent project review — October 14, 2026

## Verdict

Keep this project. It has enough technical substance to become a credible flagship for a new graduate applying to roles building LLM applications and agents. Its strongest contribution is an operational diagnosis and evaluation testbed: an LLM investigates a real, monitored transaction pipeline through read-only tools.

It is currently an implemented prototype with useful correctness checks and exploratory historical results. It has not yet demonstrated reliable diagnosis by the current agent, improvement over a simple baseline, production reliability, or fraud-classification quality. Styling the code will not establish any of those results.

The existing resume title, “Fault-Injection Testbed for LLM Diagnostic Agents,” is substantially more accurate than “real-time fraud detection system.” The current pipeline does not infer fraud: `buildEnriched` copies `txn.IsFraud` from the synthetic input (`go-enricher/processor.go:118`). A saved SDV traffic model is a data generator, not a deployed fraud classifier.

This assessment concerns the local working tree. It already contains a considerable uncommitted V2 rebuild, including untracked core source, tests, CI, and experiment files. Historical measurements must not be attributed to that changed implementation. The contents of the remote GitHub repository were not checked.

## What was inspected and independently checked

Reviewed the current Go services, Redis Lua, traffic producer, agent graph/configuration/tool adapter/reporting, MCP server, retrieval builder, Compose/Prometheus configuration, all 18 numbered fault scripts, experiment runner/scorer, CI and tests. Examined the historical evaluation's loaders, policies, judge inputs, metric calculations and stored artifacts. Surveyed 69 saved execution-log files, recounted the full 38-run canonical population and checked representative baseline observations against leakage decisions. Generated protobuf and installed dependency code were not audited line by line.

Verification during this review:

| Check | Result and practical meaning |
| --- | --- |
| Semantic-evaluation unit suite | 256 passed; validates evaluator behavior on fixtures, not the truth of model judgments |
| Agent/configuration, V2 scoring and stack contracts | 12 passed |
| Deterministic LangGraph contract | 1 passed |
| MCP metrics formatting | 1 passed |
| Production Redis window and token-bucket Lua in disposable Redis containers | 2 passed |
| Current Go source tested in the existing Linux build image, with source mounted read-only and network disabled | Both service packages compile; shared Redis-config tests pass; neither service package has Go test files |
| Additional fake-model probe: repeat a baseline call until exhaustion | Stops at 15 budgeted observations/attempts; 13 duplicate attempts produce no new observation |
| Additional fake-model probe: assert a fault contrary to two healthy observations | Can still finish as `confirmed`; corroboration is structural, not semantic |
| Additional mocked-log probe: newest log line contains the fault | Raw MCP output contains it; the agent-visible 2,000-character prefix loses it and marks truncation |
| Historical validity checks, invoked without overwriting the report | 10 fail, 2 warn, 1 skip, 1 pass |

Total: 272 Python tests passed, plus the Go checks and the three focused probes above. Most test volume belongs to the evaluator. Passing these checks does not establish end-to-end failure recovery or live model quality.

No paid model calls, fault injections into the project stacks, service restarts, load benchmarks, or new live agent evaluations were performed. Existing stack services were partly stopped at inspection. The saved V2 correctness and fault-mechanics results below were inspected, not rerun. Only this review document was added; application source and historical results were left intact.

## Engineering worth keeping

- The pipeline provides a real environment to investigate: gRPC ingress, Kafka input/output topics, Redis Cluster, enrichment, exporters and Prometheus. This supports concrete discussions about data flow and failure propagation.
- Ingress waits for a Kafka delivery event before acknowledging. Enrichment publishes output or a dead-letter record before committing the source offset. The documented at-least-once contract is consistent with those code paths; duplicate output remains possible.
- Redis updates use an atomic Lua script and common cluster hash tags. The two-hour aggregates now expire old entries, and tests exercise the production script's duplicate/expiry/statistics behavior. Deduplication is scoped to transaction ID plus IP for seven days.
- Five MCP tools expose logs, PromQL, metric names, container state and selected live configuration. The current agent has two counted baseline calls, duplicate detection, a 15-call observation budget, citation filtering, confidence downgrades, and saved prompts/tool schemas/responses.
- The new experiment runner separates its Compose project, ports and subnet from historical stacks, checks an empty-state marker, limits one scored run to each fresh stack, records a manifest and attempts restoration in `finally`. The V2 scorer requires human labels for completed runs.
- Historical evaluation distinguishes diagnosis correctness, evidence support, abstention and incorrect confirmed answers. Those are useful evaluation questions even though the current dataset does not establish general performance.

The runtime is not dominated by elaborate class hierarchies. The five agent Python modules total about 810 lines; Go application source is also fairly compact. Much of the perceived bulk is evaluation machinery, repeated documentation and historical artifacts. Simplification should make one supported execution path easier to follow while preserving the tricky delivery and state behavior.

## Findings that matter before a flagship claim

### 1. Current agent effectiveness is unmeasured

There are zero V2 run manifests under `experiments/runs/v2/`, and `experiments/human_labels.csv` has only a header. A current implementation exists, but there is no current evaluated agent result to quote.

The saved `experiments/pipeline_reconciliation_100.json` records 100 acknowledged requests, 100 raw records, 100 enriched records, zero DLQ records, and no missing or duplicate output IDs. Its probe code checks output IDs and topic counts. This is useful controlled correctness evidence, with no broker interruption or sustained workload.

The saved `experiments/fault_validation.json` records successful injection, a five-second sustained-state check, and target recovery for all four V2 faults. The README, architecture note and protocol still say these checks are pending. These documents are stale on that point. The saved check establishes fault mechanics, not diagnosis quality or full pipeline recovery.

### 2. The historical results reproduce arithmetically but remain provisional

Independent recount of saved artifacts:

| Population/result | Count |
| --- | --- |
| Canonical historical runs | 38 |
| Distinct fault IDs | 7: 1, 2, 3, 4, 6, 9, 13 |
| Reviewer-flagged leakage exclusions | 8 |
| Eligible under the semantic policy | 30 |
| Eligible with traces for investigation grading | 18 |
| Diagnosis labels | 12 correct, 1 partial, 10 incorrect, 7 abstain |
| Confirmed answers among eligible runs | 15: 10 correct, 5 incorrect |

The 12 trace-less eligible records are manually transcribed Claude Desktop results. Desktop records do not have the same budgets/instrumentation as automated runs. Six local-model runs show zero chosen tool calls and evidence of a context ceiling. Eight eligible runs target short-lived faults whose active windows were not recorded and may have ended before observation. Human judge-calibration labels are absent.

The semantic judge also receives each Desktop row's manually supplied root-cause slug and reasoning as though those fields were agent output (`eval/semantic_eval/artifacts.py:949`, `diagnosis_judge.py:144`). The legacy validity checker already identifies this provenance problem for the old grader. Moving to semantic judging does not automatically remove it. Clean evaluation should use verified model-authored text and distinguish human annotations.

The historical validity gate's 10 failures apply to that historical evaluation. They do not establish that the changed V2 agent is broken. Conversely, reproducing 12/30 or 5/15 does not validate the labels or turn the population into a controlled comparison.

### 3. The “evidence-quoting reviewer” claim exceeds the saved evidence

All 38 stored rows in `eval/semantic_eval/leakage_reviews.jsonl` have empty `evidence` arrays, including all eight `CONFIRMED_LEAK` rows. The implementation can require an extracted quotation to exist in its source, but these saved decisions do not retain those structured quotations. Their reasons describe answer exposure, and representative old baseline outputs do expose the relevant config/errors; that is weaker than a complete quotation-backed audit.

There is also a definition choice: automatically observing a bad live config is legitimate operational evidence. Providing it for free makes it difficult to claim the model independently investigated the incident. Describe the problem as pre-action answer exposure under this investigation benchmark's policy, and explain that policy. Do not imply that every informative baseline observation is inherently invalid.

Historical re-review currently reads the working-tree prompt, tool descriptions and fault scripts. Those can differ from the versions used during collection. Saved hashes alone cannot reconstruct missing source snapshots. Preserve the exact experiment code and agent-visible context for future runs.

### 4. The request-duration metric is incorrect

`go-server/server.go:58` uses:

```go
defer transactionDuration.Observe(time.Since(start).Seconds())
```

Go evaluates the deferred call's arguments immediately. This observes elapsed time near entry, rather than the time through Redis and Kafka acknowledgment. A deferred closure needs to calculate the duration on exit. This matters because diagnosis and latency-injection experiments depend on meaningful observations. Time spent waiting on network transport outside the handler is a separate measurement boundary as well.

### 5. Observation handling can hide useful evidence

`query_logs` retains the newest 500 matching lines in chronological order (`mcp-server/server.py:99`), but `observe` keeps only the first 2,000 characters (`agent/tools.py:29`). On busy services, the agent can therefore see the older portion of those recent logs and lose the newest failure. A mocked 100-line example reproduced this. Marking truncation is helpful but does not preserve the evidence.

Metric output also has a series cap and a character cap; narrow queries help, but the two-series formatting test does not prove that all fault-relevant series survive large responses. This should be addressed or explicitly controlled before collecting results.

### 6. The confidence gate is a useful heuristic, not proof

`agent/graph.py:379` filters valid citation indices and usable result prefixes, counts distinct cited tool names, and requires at least one usable model-requested observation. It does not verify that those observations support the claimed cause or provide independent causal evidence.

A deterministic probe fed it healthy container and log observations, then a model conclusion saying Redis was stopped. It still returned `confirmed` when the citations satisfied the structural gate. The resume can accurately describe the rule. It cannot claim that it guarantees grounded or correct diagnoses. “Confirmed” is a categorical label here, not a calibrated probability.

### 7. Fault-script count is not a validated fault matrix

The directory contains the original 16 scripts plus two new ones. The V2 runner registers four faults plus a healthy control. The original catalog contains mixed semantics:

- Faults 4 and 6 finish their blocking/restart behavior before the synchronous injector returns; the old driver then starts the agent.
- Faults 2 and 16 restore using `FLUSHALL` on all three Redis nodes. They remove application state, rather than restoring an identical pre-fault state, and are excluded from V2.
- Several restores hard-code defaults or recreate containers rather than restoring a captured prior configuration.
- Fault 15 claims zero retry backoff but writes zero values that mean defaults in the installed `go-redis/v9@v9.22.0`: `ClusterOptions.init` changes them to 10 ms minimum and one second maximum. Disabling backoff requires the library's `-1` sentinel, which the shared environment parser currently rejects. The script therefore does not implement its claimed zero-backoff mechanism in this version.
- Fault observability can depend on traffic level. A configured small pool or reduced consumer count is not by itself evidence that overload occurred.

It is fair to describe an implemented catalog. “16 reproducible faults with restore” requires a wider validation record than is present.

### 8. Reproducibility and measurement coverage remain incomplete

Many V2 source/test files are untracked; a source commit plus `source_dirty: true` does not archive their contents. Python dependencies and several image tags float. The public Compose file uses older prebuilt images and lacks the current observability configuration, so it is not an established reproduction path for this agent testbed.

There are no saved raw ghz load results under `load-tests/`. That harness measures gRPC responses rather than reconciled enriched output. Do not reuse the old TPS, P99, success-rate or exactly-once claims. Existing automated Go tests exercise shared configuration, not service delivery failures or recovery.

The V2 recovery check verifies the target container/dependency, not restored end-to-end processing. Before claiming reliability, reconcile records through an interruption and verify that the entire path resumes. Redis scans the live per-IP window on every update, and replay may publish a record with recomputed features; these are explainable prototype tradeoffs, not evidence of scalable throughput or immutable replay output.

## Resume claim decisions

| Supplied claim | Decision |
| --- | --- |
| Project title and listed technologies | Keep the diagnostic-testbed framing. The technologies are present. |
| 16 reproducible injectors with scripted restore | Qualify as an implemented catalog; only four V2 mechanisms have a saved validation check. Do not equate 16 files with 16 validated scenarios. |
| Prometheus instrumentation; scrape coverage from 1/3 to 3/3 replicas | Instrumentation and DNS discovery are implemented. Keep the mechanism; omit the historical measured coverage improvement unless before/after target evidence is archived. V2 preflight currently expects one enricher instance. |
| LangGraph, 15-call budget, duplicate detection, two-tool downgrade | Supported for the current custom agent. Clarify that baseline observations count and two distinct usable tool citations are a heuristic. The budget is for tool attempts/observations, not 15 model calls or a uniform Claude Desktop constraint. |
| LLM-judge evaluation over ground truth from 16 faults | Judge implementation and 16-entry historical catalog exist, but the measured historical population covers seven faults. Avoid implying all 16 were evaluated. |
| Six read-only MCP tools used unchanged by agent and Desktop | Six existed historically, including the change ledger. Current code exposes five. External Desktop configuration and exact schema parity were not verified. Update to five and omit “unchanged” unless tied to a demonstrated version. |
| Caught leakage in 8/38 using an evidence-quoting reviewer | Eight saved flags reproduce; the quotation arrays are empty. Say “flagged eight historical runs for pre-action answer exposure” with provisional context, or omit the number until manually audited. |
| Five of 15 confirmed diagnoses wrong | Arithmetic supported among 30 eligible historical runs; LLM-judged, uncalibrated and mixed environments. Suitable as a clearly scoped exploratory finding in a report; not evidence about current agent reliability. |
| 38 investigations, seven faults, six models | 38 records and seven faults supported. Six environment labels exist, but Desktop records store generic model IDs and manual labels. Do not present this as six reproducibly identified, equally configured model evaluations. |
| Compact MCP + LangGraph bullet | Keep after changing six to five and describing the confidence rule precisely. |

Conservative interim wording, subject to being able to explain and demonstrate it:

- Built a LangGraph diagnostic agent for a Dockerized Go/Kafka/Redis transaction pipeline, with five read-only MCP tools, a 15-call investigation budget, and saved tool traces.
- Implemented duplicate-call detection and a confidence rule requiring usable citations from two distinct tools, including a model-requested observation, before retaining `confirmed`.
- Built an isolated fault-experiment harness with preflight checks, injection/recovery manifests, and human-reviewed diagnosis and evidence scoring.

These describe implementation, not measured effectiveness. A later final resume should prioritize one engineering bullet, one agent/evaluation-design bullet, and one independently supported experimental result. There is no need to use every alternate bullet.

## A realistic three-day scope

This is a priority order, not a guarantee of mastering the repository or completing a statistically persuasive study in three days. A narrow, reproducible demonstration is achievable sooner than a broad benchmark.

**Day 1 — make one supported path trustworthy.** Agree on the operational diagnosis task and freeze the current execution path. Address the duration metric and observation truncation, align stale docs, preserve the source/configuration version, and confirm a healthy demo plus one fault with its saved trace and restoration. Restrict work to changes necessary for those acceptance checks; defer broad stylistic rewriting.

**Day 2 — collect a small honest pilot.** Use one fixed model, retrieval off, one budget, the four already checked sustained faults and a healthy control. Aim for three attempts per case (15 attempts) if runtime and model access permit. Preserve invalid attempts and reasons. Manually review final diagnoses and citations from the traces. Compare with a fixed container-state triage rule using the same observations: these four faults directly expose stopped/paused targets, so a simple baseline may already solve them. Present this as a pilot, and add a less directly exposed fault only after its stimulus and observation quality are verified. Do not claim model rankings or retrieval improvement from this pilot.

**Day 3 — practice ownership and finalize claims.** Trace one request and one investigation from entry to artifacts. Demonstrate a healthy case and a fault, show a failure/abstention honestly, and recompute the report. Write only bullets you can substantiate and explain. Resume-ready means you can answer the following without memorizing supplied prose:

1. What problem does the diagnostic agent solve, and why is this pipeline a useful test environment?
2. What makes an observation different from proof of a root cause?
3. Where do retries and duplicates happen, and why is the pipeline at-least-once?
4. How do the Lua window, cluster hash tags and dedup keys work? What happens on replay?
5. What does LangGraph control, what does MCP expose, and why is the budget enforced outside the model?
6. What exactly does the confidence gate check, and how can a wrong answer pass it?
7. How do you establish ground truth, a live fault, clean starting state and successful recovery?
8. Why are old results provisional, and how do you score wrong answers, abstentions and healthy controls?
9. What does the simplest baseline achieve, and what evidence shows the LLM adds value?
10. What concrete defect did you diagnose, what change did you choose, and how did you verify it?

Using a coding assistant does not remove the project's value. The practical interview requirement is ownership: explain the behavior, defend the choices, reproduce the evidence, and make a small change confidently. Understanding and checking a focused system is a better immediate investment than rewriting every file to look personally authored.
