# V2 diagnostic experiment protocol

Status: **pilot protocol, no model runs collected.** Version `2.1-draft` (`PROTOCOL_VERSION` in `experiments/runner.py`). The larger matrix sketched in the archived [rebuild plan](../archive/pre-simplify/PROJECT_REBUILD_PLAN.md) is a target, to be frozen only after a valid pilot.

## Pilot question

Can a tool-using model distinguish a healthy pipeline from a stopped Redis master, paused ingestion, a stopped Kafka broker or a paused enricher, name the affected component, cite live observations, and abstain when evidence is insufficient?

Run each case with retrieval off first. The agent receives a neutral trigger, the five observation tools, a 15-call budget including two scripted baseline calls, and no fault label or change ledger. Retrieval-on trials require an index whose manifest proves the tested fault was held out (`corpus/build_index.py --exclude-fault-id N`). Keep model ID, `ModelSettings` and the traffic profile fixed within a comparison; the manifest records all three.

These four faults directly expose a stopped or paused container, so a fixed container-state triage rule given the same observations is the baseline to beat before claiming the model adds value.

## Run validity

A run is eligible only if it used a freshly created isolated stack whose clean-state marker it consumed; all required services and the producer were healthy and traffic was flowing before injection; the fault was live before and after the agent; the agent completed with parseable, untruncated output and a trace containing its prompts, tool schemas and replies; the target recovered; **and enriched output resumed afterwards**. The runner enforces these mechanically and records failures in `validity_errors`.

A human reviewer must also check prompt and tool content for answer exposure, whether the fault was actually observable, and environmental contamination. Invalid attempts stay in the **attempted** count with their reasons and are excluded from accuracy.

## Scoring

For valid fault runs, score the exact root cause and affected component as `correct`, `wrong` or `abstain`; naming only "Redis" or "the network" is not correct. For healthy controls, a reported fault is a false alarm. Reviewers fill `experiments/human_labels.csv` (`reviewer`, `run_validity`, `validity_reason`, `diagnosis_label`, `evidence_label` ∈ `supported`/`unsupported`/`inconclusive`) after checking citations against the saved trace. `python -m experiments.score` recomputes counts, Wilson intervals and per-case tables from manifests and labels only. An agent's own `confirmed` label is never a score. Report case-level counts, not just a pooled percentage.

Runs made with `experiments/fake_model.py` test orchestration only. Keep them in a separate `--runs-dir`; they are never labelled or scored.

## Pipeline measurements

Agent diagnosis and pipeline performance are separate results. `experiments/pipeline_probe.py` reconciles a controlled batch of acknowledged requests with raw, enriched and DLQ records on an empty stack; it is a correctness check, not a throughput benchmark. A gRPC acknowledgment proves raw delivery only. No TPS or P99 claim is valid without a sustained, reconciled load run that saves its raw output, hardware, image digests, offered rate and end-to-end latency.

## Current blockers

A model endpoint (and credentials) is needed for the pilot. Fault mechanics and processing recovery were re-checked on a fresh fixture stack during the 2026-10-27 cleanup; whether each fault is *observable enough* for diagnosis is still a reviewer judgment per run.
