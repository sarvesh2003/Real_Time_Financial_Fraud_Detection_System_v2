# Project rebuild and experiment plan

Status: implementation in progress, 2026-10-10. Local pipeline fixes, tests, and a pilot runner are implemented. No new model or performance result is implied by this document.

## Goal and scope

Build a credible AI engineering flagship: an **operational diagnostic agent** that investigates a monitored transaction pipeline, identifies injected faults, cites observed evidence, and knows when to abstain. The transaction pipeline supplies a realistic, controllable system to diagnose. The V2 producer's `is_fraud` field is a synthetic label; this work does not claim to deploy a fraud classification model.

Success means another person can clone the repo, start a clean stack, reproduce the fault experiments, recompute every reported metric from saved artifacts, and follow the code without understanding the historical Claude sessions. A good experiment may show modest agent performance. The final claims will follow the data.

The existing run artifacts stay available as **historical, non-comparable material**. New measurements get a new schema/version and separate output directory. We will not silently blend old and new results.

## Work sequence and gates

| Phase | Work | Evidence required to move on |
| --- | --- | --- |
| 1. Freeze the contract | Inventory public APIs, data schemas, current faults, current metrics, and known failures. Write a short architecture diagram and define the diagnosis task, allowed observations, fault ground truth, and reporting vocabulary. Mark unsupported README claims as historical. | One-page system description; source-to-claim map; experiment protocol frozen before runs. |
| 2. Make the pipeline trustworthy | Simplify the Go modules and container builds; make local `go test ./...` work; repair Kafka acknowledgment/retry and consumer replay behavior; test Redis counter semantics; handle bad messages and GeoIP startup errors. Use a documented **at-least-once + idempotency** delivery contract unless stronger semantics are implemented and verified. | Unit and integration tests for broker failure, replay, duplicate ID, Redis time window, and invalid input; clean Compose boot; input/output reconciliation. |
| 3. Simplify observability and agent | Keep read-only MCP tools, but make their inputs, errors, output caps, and timestamps explicit. Organize the Python agent around a small config module, tool adapter, investigation loop, evidence store, and report schema. Count all observations consistently, including the scripted baseline. Validate citations and treat failed/empty tools as unavailable evidence. | End-to-end smoke run on a healthy stack and a known fault; complete trace; no leaked answer; deterministic parser/contract tests. |
| 4. Rebuild the fault harness | A run gets a unique ID and manifest. Verify a healthy baseline, inject one fault, prove it is live, start the agent, record injection/observation/restore times, restore in `finally`, verify recovery, and archive raw metrics/logs/tool transcript/config/model metadata. Isolate or reset state between runs. | A pilot run can be replayed from its manifest; interrupted runs are marked invalid and restored; no stale fault residue. |
| 5. Run pilot, then main experiment | Use the RunPod endpoint supplied later. First test model connectivity, context length, tool calling, and output parsing. Run the pilot; fix protocol defects; freeze the code and model settings; then run the main matrix. | Pilot validity gate passes before main runs. All main runs use the same image/config/protocol version. |
| 6. Analyze and publish | Blind human review of diagnoses and evidence; calibrate any LLM judge against those labels. Recompute metrics from raw artifacts and produce a claim sheet that maps each resume statement to code, tests, and experiment evidence. | Report includes denominators, uncertainty, failures, hardware/model/cost context, and exact reproduction commands. |

## Code organization target

Keep the existing technology stack where it serves the experiment. Move to a single, testable Go module for the two services and generated protobuf code; one small Python package for the MCP/agent runtime; one evaluation package with a versioned run schema; and one infrastructure area for Compose, Prometheus and fault scripts. Put old experiments under a clearly named archive or leave them in place with an explicit historical label. Avoid parallel legacy and new graders writing the same result name.

Each module should have one responsibility and an obvious entry point. Configuration should be typed and validated at startup. Error paths should return structured failures; no `print` plus continuing with nil dependencies. Dependencies should be pinned, with a short local setup and CI command. Refactor in small behavior-preserving slices, using characterization tests before replacing tricky Kafka/Redis logic.

## Experiment protocol, draft to freeze before collection

**Task.** Given a neutral trigger such as “Investigate the pipeline,” identify the active operational root cause and affected component, cite observations, or abstain when the evidence is insufficient. The agent never sees the injection script, fault name, run ID encoding the answer, or evaluator notes. Tool outputs should describe the system state, with timestamps, and exclude deliberate fault labels.

**Scenarios.** After testing observability and restoration, select about eight sustained faults across at least three families (Redis, Kafka, network/config), plus two controls: an ordinary healthy run and a benign change that should not trigger a fault diagnosis. Transient faults are a separate follow-up study with an observation-window protocol; they do not enter the first headline figure. Fault variants should include target and severity changes so the agent cannot solve the benchmark by memorizing eight scripts.

**Conditions.** Start with one pinned RunPod model, fixed inference settings, identical tool permissions and budgets, and two paired conditions: retrieval off and retrieval on. Retrieval documents must exclude the test fault and any exact target/answer; a leave-one-fault-out corpus build is the default. Record the corpus digest per run. A simple fixed-rule triage baseline can be included only if it receives the same observables and its rule set is frozen before scoring. Manual Claude Desktop sessions are a separate exploratory comparison, never pooled into the automated-agent accuracy.

**Repetitions.** Pilot: four sustained faults plus one control, two retrieval conditions, three repeat seeds = **30 runs**. Main target: eight faults plus two controls, two conditions, five repeat seeds = **100 runs**. Pair each retrieval-on/off trial by fault variant, seed, starting state and model configuration, and randomize execution order. If the pilot reveals instability or cost constraints, revise the matrix *before* main collection and log the revision.

**Run validity.** Save the exact agent-visible initial prompt, tool schema, every tool request/result, model response, final report, injection start/end, first/last observation, stack health, container digests, source commit, model ID, endpoint type, inference settings, token counts, wall time, and restore verification. A run is invalid if the fault was absent during observation, the stack was already unhealthy, the model prompt/tool schema was truncated, the run crashed, or answer information leaked. Invalid runs are reported with reasons; they do not vanish from the population table.

**Primary result.** Strict root-cause accuracy on valid fault runs, judged blind to retrieval condition against the actual injected fault and live-state evidence. A correct family alone is partial, not correct. Report abstentions and wrong diagnoses separately. Report a control false-alarm rate and an incorrect-among-confirmed rate because a confident wrong answer is costly.

**Secondary results.** Evidence support and causal reasoning, tool success/duplicate rate, diagnosis latency, model tokens and estimated run cost. For a meaningful retrieval comparison, report paired per-fault differences and uncertainty; do not treat repeated trials on one fault as many independent fault types. Show fault-level tables and Wilson intervals for proportions. Verify judge agreement on a human-labelled sample, and use human labels as the final authority if agreement is weak. Do not rank models from a single, small, mixed-environment dataset.

## Pipeline measurements to support system claims

Run a separate load and correctness experiment after the pipeline fixes. Save raw ghz output and an end-to-end reconciliation of acknowledged gRPC transactions, raw Kafka events, enriched Kafka events, duplicates, errors and dead-letter records. Use workloads with repeated users/IPs and unique transaction IDs, plus controlled retries and broker interruptions. Report ingress acknowledgment latency, end-to-end latency, sustained delivered throughput and loss/duplicate counts with the exact hardware, container limits and test duration. Never equate an enqueued Kafka message with a delivered enriched transaction.

The first performance result is a **baseline**, not a target number. Any future TPS or P99 resume claim needs a committed raw result and a reproduction command. Reliability claims need failure/recovery tests with timestamps and event reconciliation.

## Time and cost estimate

The 100-run main matrix is roughly 8–20 hours of model inference if a run takes 5–12 minutes, before fault setup, recovery, reruns and human review. This is a capacity estimate, not a promised runtime or RunPod bill. The pilot will measure actual per-run time, token use and endpoint price; then set a cost ceiling before the main experiment. Pipeline load tests have a separate budget because they use local CPU, Kafka and Redis more than model inference.

## Deliverables and resume gate

Deliver a readable README, architecture diagram, setup script/commands, passing tests, versioned experiment protocol, raw run manifests/traces, evaluation script, final report, and a short claim sheet. Resume bullets may state implemented engineering work once verified in code/tests. Numerical improvement, accuracy, throughput, latency, and reliability bullets require the final experiment evidence and must identify the measured population and conditions.

RunPod access is needed only after phases 1–4 and the pilot protocol are ready. Store its URL and credentials outside git; the endpoint itself is not ground truth or a performance guarantee.

## Implementation checkpoint, 2026-10-10

- Phase 1: a current README, source-to-claim map, architecture note and pilot protocol are present. The old README is preserved under `docs/LEGACY_README.md`.
- Phase 2: both Go services build from one module; ingress waits for Kafka delivery; enrichment commits after output delivery; Redis has an atomic two-hour window and replay test. Final images passed an isolated duplicate/invalid-input smoke test and a fresh 100-request reconciliation with no missing or duplicate output IDs. Sustained load and failure/recovery tests are still open.
- Phase 3: baseline observations count against budget; tool errors and duplicate observations cannot corroborate; the MCP server exposes five read-only tools and records prompts and tool schemas. A model pilot is still needed.
- Phase 4: the runner validates a clean-stack marker and has scripts for four pilot faults plus a healthy control. The fault scripts still need live validation. A broader matrix, independent human scoring, and post-run reproducibility review remain open.
- Phases 5–6: no RunPod endpoint or new model results yet. No new accuracy, latency, or throughput number is ready for a resume.
