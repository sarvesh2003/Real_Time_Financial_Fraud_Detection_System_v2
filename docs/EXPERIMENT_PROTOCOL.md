# V2 diagnostic experiment protocol

Status: **pilot protocol, no model runs collected**. Version `2.0-draft` in `experiments/runner.py`. The full matrix remains a target in `PROJECT_REBUILD_PLAN.md` and will be frozen only after a valid pilot.

## Pilot question

Can a tool-using model distinguish a healthy pipeline from a stopped Redis master, paused ingestion, stopped Kafka broker or paused enricher, identify the affected component, cite live observations, and abstain when evidence is insufficient?

Run each case with retrieval off first. The agent receives a neutral trigger, the five observation tools, a 15-call budget including two scripted baseline calls, and no injected-fault label or change ledger. Retrieval-on trials require a held-out corpus manifest that excludes the tested fault ID. Keep model ID, inference settings and traffic level fixed in each paired comparison.

## Run validity

A pilot result is eligible only if a dedicated experiment stack was initialized from empty Kafka topics and Redis, all required services plus traffic producer were running before injection, the expected fault was observed before and after the agent, the agent completed, and the target recovered. One scored run consumes one clean-stack marker; the isolated stack must be recreated before the next run. Each run saves a manifest with source commit/dirty state, fault script hash, stack container IDs, pre/post Kafka offsets and Redis key counts, injection/agent/recovery timestamps, logs and the agent result/trace. The trace header records the system prompt and tool schemas; investigation events record the agent-visible prompt and model response. The run is invalid if these artifacts are missing, the model call is truncated without successful retry, the observation tool failed in a way that prevents diagnosis, or answer information is exposed to the agent.

The runner enforces some of these checks mechanically. A human reviewer must also inspect prompt and tool content, fault observability, and environmental contamination before accepting a run. Invalid attempts remain in the denominator of **attempted runs**, with reasons, but are excluded from diagnostic accuracy.

## Scoring

For valid fault runs, score exact root cause and affected component as correct, wrong, or abstained. Merely naming Redis or the network is insufficient. For healthy controls, score false alarm versus appropriate healthy/inconclusive report. The reviewer fills `run_validity` (`valid` or `invalid`) and a reason for invalid runs in `experiments/human_labels.csv`. For valid runs, they fill `diagnosis_label` (`correct`, `wrong`, or `abstain`) and `evidence_label` (`supported`, `unsupported`, or `inconclusive`) after checking citations against the saved tool transcript. A correct label with unsupported evidence is separately counted. A human reviewer is the authority until any semantic judge is calibrated on blind labels. Report case-level counts, not only an overall percentage. Paired retrieval differences should be compared by case and run, with failures and abstentions visible.

## Pipeline measurements

Agent diagnosis and pipeline performance are separate results. A fresh-stack 100-request correctness probe is saved as `experiments/pipeline_reconciliation_100.json`; it reconciled all 100 acknowledgments with raw and enriched records and found no DLQ or duplicate output IDs. It does not establish sustained throughput or latency. For a pipeline load test, save the raw load-generator output and reconcile acknowledged gRPC requests, raw Kafka events, enriched events, DLQ records, duplicate IDs and errors. Capture hardware, image digests, duration, offered rate and end-to-end latency. A gRPC acknowledgment is evidence of raw Kafka delivery only; an enriched record proves later processing. No TPS or P99 claim is valid before this reconciliation.

## Current blockers

The old Compose stack contains Kafka and Redis history. The dedicated `fraud_v2_experiment` Compose project uses separate ports and a separate subnet, preserving that history. A RunPod URL and model credentials are required for the model pilot. The four pilot faults still need live observability and restoration checks before they can support an accuracy claim; a broad claim requires a larger, independently reviewed matrix.
