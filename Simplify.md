# Simplify the project: autonomous execution instructions

Prepared October 26, 2026. This is the user's execution brief for Claude Code. Read it completely before acting. Follow applicable repository instructions and platform permissions. This brief authorizes the work described here; it does not bypass tool permissions or authentication.

## 1. Objective and scope

Make the maintained code easy for its owner to navigate, explain, test and modify. Complete a deliberate simplification of the supported transaction-pipeline, observation-tool, diagnostic-agent and experiment-harness flows. Preserve useful engineering behavior and historical evidence. Fix the confirmed defects identified below with regression checks.

Work autonomously through phases 00–08. Make routine implementation decisions yourself and record the reasons. Do not stop after making a plan or ask the user to approve every phase. Execute sequentially; separate branches are for review and recovery, not a requirement to run agents or developers in parallel.

The user's target is three days for this code cleanup. Prioritize the supported runtime, meaningful checks and understandable documentation. Do not spend the deadline expanding features, chasing test counts or rewriting already-clear code. If execution is interrupted, leave recoverable checkpoints and an accurate remaining-work report. A deadline does not turn an incomplete phase into a completed one.

This brief supersedes earlier suggestions to collect a new model benchmark during the cleanup. RunPod/GPU experiments, model selection, retrieval ablations, model rankings, throughput tuning and final numerical resume results are deferred. No paid resources or live external model calls are needed for this task.

## 2. Desired result

- One obvious supported setup and execution path.
- Straightforward functions and modules with clear responsibilities and explicit error handling.
- A transaction flow and an investigation flow that can each be explained from entry point to output.
- Meaningful tests of behavior, including the failure boundaries affected by the changes.
- A small set of current documents, with obsolete prose moved into an indexed archive.
- Historical experiment artifacts preserved without reinterpreting them as results from the changed code.
- A verified remote baseline before refactoring, reviewable phase branches and commits, and a tested integration branch containing the finished work.

“Simpler” does not mean fewer files at any cost. Prefer readable code over a generic framework, clever indirection, global mutable configuration, or one enormous file. Keep an abstraction when it isolates a real dependency or responsibility; remove it when it merely forwards arguments or hides control flow. Do not target an arbitrary line count or replace every class just because it is a class.

The project remains an operational diagnostic-agent testbed. Its synthetic `is_fraud` label is input data, not classifier output. Do not introduce a fraud model or stronger delivery guarantees as part of simplification.

## 3. Inspect the actual repository first

The following was observed when this brief was written. Recheck it rather than assuming it is still current:

- Branch: `harden-agent-and-eval`.
- Many tracked files are modified and important V2 source/tests are untracked. The present working version is not fully protected by existing Git commits.
- Primary remote `origin`: `https://github.com/sarvesh2003/Real_Time_Financial_Fraud_Detection_System_v2.git`.
- Secondary remote `incident-investigator`: `https://github.com/sarvesh2003/Incident_Investigator_Dummy.git`.
- The local shell is Windows PowerShell. Linux Go builds use Docker because the Kafka dependency uses CGO.
- Local `.env`, virtual environments, several raw run/log directories and corpus indexes are ignored. A Git checkpoint alone cannot restore all ignored files or Docker data.

Read `README.md`, this brief, the current architecture note, `docs/PROJECT_REVIEW_2026-10-14.md`, relevant tests and applicable `AGENTS.md` files. Use earlier audits as leads; source and executed checks establish current behavior. Do not treat a previous claim of passing tests as a test you just ran.

## 4. Phase 00 — Back up the full source before changing it

**Hard prerequisite: create and verify the baseline commit and remote backup before changing application code, dependencies, documentation layout or tests.** Reading files and Git-administrative backup preparation are allowed first. This instruction file itself should be part of the baseline.

1. Inspect the repository root, current branch/HEAD, index, status, tracked files, untracked files, ignored artifact locations and remotes. Preserve any pre-existing work. Do not reset, discard, stash away or selectively forget the current changes.
2. Create a unique backup branch such as `backup/pre-simplify-<YYYYMMDD-HHMMSS>`, carrying the present working changes. Do not change the checked-out source before capturing it.
3. Review an explicit staging list. Include all modified/deleted tracked project files and legitimate untracked source, tests, configuration, workflows, current documentation and small shareable evidence files. A tracked deletion is part of the snapshot. Do not blindly run `git add .` or `git add -A`.
4. Keep `.env`, real credentials, private local notes, caches, virtual environments and large ignored artifacts out of the remote commit. Check staged content for accidental secrets without printing their values. If a necessary source file is ignored, inspect why and add only that reviewed file; never force-add a whole ignored directory.
5. Preserve ignored historical runs/traces and other irreplaceable local artifacts in place. Record their locations and checksums in a private local inventory. Where a separate local copy is needed, use a uniquely named `.simplify-local/<run-id>/` directory excluded through the repository-local Git exclude rules before copying. Do not copy all environments, `.git`, Docker volumes or secrets. These are local artifact safeguards, not content to publish.
6. Commit the reviewed project snapshot with a message such as `chore: snapshot working implementation before simplification`. Record inherited issues without fixing the snapshot to make it prettier. A baseline commit is allowed to preserve known failing behavior.
7. Create a unique lightweight tag `pre-simplify-<run-id>` at this commit.
8. Verify that `origin` still names the primary repository above. Push the new backup branch and tag explicitly to `origin`. Do not push to the secondary remote, push an existing default branch, force-push, or change remotes.
9. Use `git ls-remote` to verify that the remote backup branch and lightweight tag both resolve to the baseline commit SHA. A successful-looking local commit is insufficient. Retain the exact baseline branch, tag and SHA for the final report.
10. Confirm that every intended nonignored project change has been captured. Remaining untracked/dirty files must have a documented reason; untracked runtime source is not an acceptable omission.

If identity, authentication, network access, remote mismatch or platform permissions prevent the verified backup, report the exact blocker and stop before refactoring. Do not fabricate an identity, expose a token, weaken permissions, assume a failed push worked or continue without the backup. Retry a transient failure reasonably; do not wait in an endless retry loop.

**Exit:** verified remote source snapshot; local historical artifacts preserved; baseline identity recorded. No application refactor has begun.

## 5. Branches and commits

Create `simplify/<run-id>/integration` from the verified baseline. Leave the original branch and default branch untouched. Publish the integration branch explicitly when ready; the finished result stays there for user review. Do not merge into the default/original branch or delete branches as part of this task.

Use the following branch boundaries. Phase 01 produces a contract commit that independent work can share. Execute phases in the listed order even when they have an independent base.

| Phase | Branch suffix | Semantic prerequisites | Primary file ownership |
| --- | --- | --- | --- |
| 01 | `01-map` | Verified baseline | Current-flow map, baseline checks, contracts, phase inventory |
| 02 | `02-pipeline` | 01 | `go-server/`, `go-enricher/`, `internal/redisconfig/`, relevant Go/Lua tests |
| 03 | `03-mcp` | 01 | `mcp-server/`, MCP contract tests; observation formatting |
| 04 | `04-agent` | 02 and 03 integrated | `agent/`, agent contract/graph/report tests; consumption of MCP observations |
| 05 | `05-traffic` | 01; integrate with current pipeline before accepting | `python-producer/`, traffic tests and its Dockerfile/requirements |
| 06 | `06-experiments` | 02–05 integrated | `experiments/`, supported fault scripts, harness/scorer tests |
| 07 | `07-docs-build` | 02–06 integrated | Prose archive, README/current docs, Compose/build wiring, dependency/build consistency, CI |
| 08 | `08-verify` | 07 integrated | Final verification artifacts, walkthrough and completion report |

Full branch names are `simplify/<run-id>/<suffix>`. Keep top-level source paths stable unless the flow map identifies a concrete reason to change them. Do not make a repository-wide rename first and leave the subsequent phases to repair it.

Phases 02 and 03 may start from the same accepted Phase 01 commit because they own separate source. Phase 05 can also share that base if its implementation does not need later code. For dependent phases, first integrate the prerequisite branches, then branch from that tested integration tip. This gives the new phase the files it needs without mixing the prerequisites into its own review diff. Git ancestry can contain earlier phases even when they are not semantic dependencies.

For each phase:

1. Record the base SHA and owned files in `docs/simplification/phase-NN.md` before editing production code. Check other active imports/callers first.
2. Work only on its branch. Use focused commits, separating mechanical moves from semantic changes. At least one completed checkpoint commit is required for every phase, including a phase that needs documentation only.
3. Demonstrate relevant tests and review `git diff --check`, changed paths and the diff from that phase's recorded base. Record inherited whitespace/test problems separately rather than silently attributing them to the phase.
4. Push the phase branch explicitly to `origin` after its checks pass. Keep the branch available for review. Do not create PRs or post comments/messages unless separately requested.
5. Merge it into the integration branch with a non-fast-forward merge, retaining its history. Test the prospective merge before committing/pushing the integration result where practical (`--no-ff --no-commit`). Resolve conflicts according to the documented contracts, not by selecting an entire side blindly.
6. Run checks relevant to the combined changes, then commit the merge and push the integration branch. A passing isolated phase is not proof that the merge works.
7. Verify remote tips against local tips. Do not proceed with dependent phases while a required merge is broken or unpublished.

Keep unrelated work out of a phase. If integration exposes a genuine cross-component defect, use a small named integration-fix branch and record the dependency. Do not use the final phase as a place to hide a broad unreviewed rewrite. Never force-push, rewrite existing commits, run `git clean`, or use a hard reset to resolve trouble.

For resume after interruption, inspect the actual local/remote branches and commit graph, read the phase notes, verify the current worktree and tests, and continue from the last accepted integration state. Do not trust an “accepted” sentence without the corresponding commits and verification. Notes may reference earlier implementation commits; do not create endless commits trying to put a note's own commit SHA inside itself.

## 6. Test-driven working rules

Use TDD for behavior changes and tricky refactors, not artificial tests of file layout or implementation details.

- Before refactoring a risky behavior, add or strengthen a characterization check for its meaningful existing contract. Run it on the baseline implementation before changing that behavior.
- For a confirmed defect, first demonstrate a focused failing regression check, then apply the smallest correction and show it passing. Distinguish the deliberate correction from behavior-preserving cleanup.
- Refactor behind passing checks, then exercise its integration boundary. Do not rely solely on mocks for Redis Lua or Kafka delivery/recovery semantics.
- Do not codify known bugs as desired behavior, rewrite expectations just to match changed code, remove useful tests to get green, or use skipped tests as evidence of success.
- Documentation moves require link/command/path checks, not a large new unit-test suite. Generated protobuf and installed dependencies are not handwritten refactor targets.
- Test fixtures may fake the model and tool responses. Record them as deterministic control-flow checks, never as measured LLM quality.
- Keep the accepted integration branch green. Red/green work can happen locally within a phase; merge the passing test and fix together. Do not knowingly publish failing test-only checkpoints as completed phases.

Each phase note must state: responsibility, entry points/callers, files changed, preserved contracts, deliberate behavior changes, checks actually run and outcomes, any limitations, and why the resulting control flow is easier to follow. Include the failing regression observation where applicable. Use short factual notes, not another long narrative log.

## 7. Phase 01 — Map the supported flow and establish baseline checks

Create a concise map of maintained code, experimental/historical code, generated code, assets and prose. Identify the actual entry points, package/import boundaries, configuration sources, calls, state changes and output schemas. Inventory every maintained handwritten source file with a one-line responsibility; do not merely list filenames.

Document these flows in the initial current architecture note:

1. Traffic request -> gRPC validation/rate limiter -> raw Kafka acknowledgment -> consumption -> GeoIP/Redis features -> enriched or DLQ delivery -> raw offset commit.
2. Agent configuration -> model/MCP setup -> counted baseline -> model-selected observation loop -> citation/confidence checks -> result/trace files.
3. Isolated stack -> readiness -> fault injection/live checks -> investigation -> restoration/recovery -> validity review/scoring.

Record the installed test/build entry points and run baseline checks. Do not install into the global Python interpreter when an appropriate local environment is available. Preserve the single root Go module. Identify baseline failures, environmental prerequisites and commands needing the Linux Docker toolchain.

Freeze external contracts for the cleanup: gRPC/protobuf fields, Kafka topics/key semantics, at-least-once behavior, Redis processing-time window/dedup scope, five read-only MCP tools, 15-call observation budget including baseline, trace/result fields, and current V2 human-label scoring. Keep existing confidence behavior as a documented heuristic; broader policy redesign and baseline-comparison research are deferred.

**Exit:** supported flow and ownership map; baseline check results; explicit known defects; no speculative new architecture framework.

## 8. Phase 02 — Simplify and check the Go pipeline

Keep understandable ingestion, processing and delivery functions. Make the Kafka/Redis/GeoIP lifecycle and error paths explicit. Retain the shared Redis configuration module when it removes real duplication. Introduce only small dependency seams needed to test a real failure boundary; do not create a framework of interfaces, factories and wrappers around every library call.

Preserve these contracts:

- Ingress success follows Kafka delivery acknowledgment; request cancellation can have an ambiguous broker outcome.
- Enrichment/DLQ output delivery precedes source-offset commit. Replay can emit duplicate output records.
- Redis window updates are atomic, use processing time and are idempotent for a transaction ID on the same IP for seven days. They do not provide exactly-once Kafka output.
- GeoIP/input/serialization failures have explicit outcomes; invalid Kafka input does not silently poison the stream.

Correct the request-duration bug: `defer transactionDuration.Observe(time.Since(start).Seconds())` evaluates elapsed time on entry. Calculate duration at exit and demonstrate the measurement with a meaningful regression check.

Verify any client-library defaults/sentinels before simplifying configuration. If supporting the retry-backoff disable sentinel is necessary for maintained fault scripts, allow it only for the relevant backoff settings and test it; do not make all numeric settings accept negative values. Otherwise clearly mark the unsupported historical injector and preserve it as historical evidence.

Check ingress invalid-input/rate-limit behavior, production Lua duplicate/expiry/boundary behavior, producer delivery errors, output-before-commit ordering, poison-message handling and relevant replay paths. Use Linux builds as needed. Keep generated protobuf unchanged unless an actual compatibility correction is demonstrated.

**Exit:** clear pipeline flow, corrected timing metric, passing focused behavior checks and builds; limitations documented without claiming unmeasured reliability.

## 9. Phase 03 — Simplify MCP observation tools

Keep the five existing read-only tool responsibilities clear: logs, PromQL, metric discovery, container state and selected live configuration. Make HTTP/Docker failures, partial failures, timestamps and truncation understandable. Keep explicit arguments rather than shell-built command strings. Preserve supported service/input validation and credential filtering.

Preserve current observations rather than retaining stale output prefixes. Logs should retain useful newest lines within the agreed response boundary; metrics should preserve current samples and indicate omitted series. Do not embed fault labels, injected answers or prescriptive fault diagnoses into neutral observation output.

Test malformed duration/query/filter inputs, unavailable dependencies, empty and partially failed responses, busy logs, multi-series output and read-only tool schemas. Verify intended Compose/environment forwarding. This phase owns the producer side of the observation contract; Phase 04 owns the agent's final cap/consumption and checks the complete path.

**Exit:** tool contracts are explicit and verified with controlled responses; no new tool types or agent-policy edits.

## 10. Phase 04 — Simplify the agent runtime

Make configuration, model/MCP initialization, investigation control, observation storage and report writing straightforward. Existing modules can remain if their responsibilities are already clear. Prefer normal package imports and explicit entry points over path manipulation; update callers/tests with any justified change.

Keep LangGraph if its small state machine remains useful and readable. Do not switch frameworks or replace it with a new home-grown framework to make the code look different. Remove repeated compatibility fallbacks only when the supported endpoint contract and tests make that safe.

Preserve the 15-call budget including the two baseline observations, duplicate detection, read-only tool set, citation resolution and existing confidence downgrade contract. Keep confidence described as a heuristic. Preserve result/trace compatibility or provide a small documented schema migration with fixture tests; never overwrite the old results to make them appear compatible.

Fix the observed end-to-end truncation defect: retaining 500 newest log lines and then taking only the first 2,000 characters can discard the newest failure. Test MCP formatting plus agent consumption together. Define the character/token/output boundary in one obvious place without pretending that a character cap guarantees a model context size.

Exercise baseline-only conclusions, budget exhaustion, duplicates, unavailable tools, empty/partial errors, invalid citations, long output, malformed final JSON, model timeout/failure, and trace persistence on failure. Distinguish logical model turns from retry attempts where telemetry is affected. Keep clean healthy/inconclusive outcomes possible.

Use fake models and controlled observations for these checks. No RunPod access, model tuning or accuracy claim is required.

**Exit:** owner can follow one investigation through the code; deterministic failure-path and observation tests pass; output contracts and limitations remain explicit.

## 11. Phase 05 — Simplify synthetic traffic generation

Keep the request generator and transport loop easy to understand. Make seed, workload rate, user/IP reuse, timeout and shutdown behavior explicit. Test request validity and deterministic aspects without requiring a running model endpoint.

Retain the SDV generator if it is useful and can be explained simply. Do not silently substitute a different distribution and call it behavior-preserving. If a lightweight deterministic fixture is needed for integration tests, make its role explicit and keep it separate from claims about SDV-generated traffic. Move optional heavy dependencies behind a documented optional path only when that actually improves setup and both paths are verified.

Do not delete saved generator assets or remove a dependency until its callers/runtime requirements have been checked. Keep generated protobuf imports/build paths working. Record any workload change because future experiments depend on the input distribution.

**Exit:** understandable producer, valid requests, reproducible test workload where claimed, verified image/import path and documented semantics.

## 12. Phase 06 — Simplify the maintained experiment path

Keep `experiments/` as the current orchestration/scoring path unless the Phase 01 map gives a concrete reason to change it. Keep the historical `eval/` implementation frozen by default; do not rebuild or merge multiple historical graders into a new framework.

Make stack settings, run lifecycle, manifests, fault restoration and human-label scoring explicit. Prefer a readable sequence of functions over a universal experiment engine. Preserve current cases and output schemas; adding the later four-fault benchmark or new baselines is outside this cleanup.

Verify clean-stack checks, model/tool metadata separation, fault-live checks, agent failure handling, partial-injection restoration, interruption cleanup, recovery and artifact creation. A running container is not by itself proof that the processing path recovered; add an appropriate small processing check for maintained recovery claims.

Test invalid/missing/duplicate labels, attempted-versus-valid accounting and scorer recomputation from small synthetic artifacts. Do not manufacture human labels or real-model traces. Do not overwrite the saved 38-run historical population, judge outputs, old reconciliation or fault-validation summaries.

Operate only disposable tests or a clearly identified fresh isolated test stack. Before starting or stopping it, verify its project name, container IDs, ports and data scope. Avoid reusing old `fraud_v2_experiment` containers merely because their name matches. Never stop/reset unrelated stacks, run global Docker cleanup, or use `FLUSHALL` on existing project state. If necessary, use a unique test project/subnet/ports and remove only resources created by this execution.

**Exit:** straightforward maintained harness; meaningful lifecycle/scorer checks; an offline fake-model path verifies orchestration without implying model-quality evidence.

## 13. Phase 07 — Archive obsolete prose and finish setup/build wiring

Create `archive/pre-simplify/` with an index identifying the baseline tag, original paths and why items were archived. Preserve original relative paths inside the archive. Move obsolete prose with `git mv` when tracked; preserve untracked baseline-captured prose with an equivalent tracked move. Archive is not a deletion bin.

Classify documents individually. Likely candidates include old rebuild plans, historical audits/status notes, duplicate interview guides, legacy README material and superseded planning under `Chatgpt_Documentation/`. Check references and content before moving anything. Extract still-useful limits/decisions into the current docs. Do not bulk-move all `.md` files or archive this controlling brief, applicable `AGENTS.md`/`CLAUDE.md`, runtime prompts, fixtures, schemas, generated assets or dependency manifests.

Leave historical `eval/` code/data in place and clearly label it unless its relocation is demonstrably self-contained and small. Historical records must remain locatable. Do not make the code-cleanup task depend on fixing the validity of an old benchmark.

Keep the current documentation small and purposeful:

- `README.md`: project purpose, honest status, supported setup, run/test/demo commands and limits.
- `docs/ARCHITECTURE.md`: component responsibilities and transaction/investigation/experiment control flow; one line per maintained source file or a concise linked file map.
- `docs/DEVELOPMENT.md`: prerequisites, configuration, tests, debugging and adding a supported fault/tool without a framework rewrite.
- `docs/simplification/phase-NN.md`: short phase-specific review notes.
- `docs/SIMPLIFICATION_REPORT.md`: final verification, phase refs, remaining limits and later evaluation prerequisites, completed in Phase 08.
- `archive/pre-simplify/README.md`: historical index.

Update links and commands after moves. Keep `Simplify.md` at the root for the execution and final handoff. Do not generate another family of overlapping “ultimate”, “rewritten” or “final-final” documents.

Complete Docker COPY paths, `.dockerignore`, Compose wiring, environment examples and runtime dependency pinning for the tested setup. Resolve the old public Compose path explicitly: verify and support it, or label/archive it as historical and point to the supported path. Do not silently retain two conflicting quickstarts.

Update CI to run the meaningful supported deterministic/unit/integration checks. Keep Linux Go build requirements explicit. A normal deterministic CI job must not need RunPod, a paid model or private MaxMind credentials; use an honest fixture/dependency seam where needed, and retain a separately documented real-GeoIP path. Pin tested compatible dependency versions; do not blindly freeze an unrelated global environment or claim cross-platform portability not checked.

**Exit:** current docs describe actual code; obsolete prose is indexed; setup/build/test wiring is consistent; applicable local equivalents of CI checks pass. Report remote CI status only if actually observed.

## 14. Phase 08 — Verify the integrated result and finish the handoff

Run the full relevant suite once against the final integrated source, including regression checks added during cleanup. Re-run after any corrective changes. Avoid repeating unchanged expensive checks without a reason.

At minimum verify:

- Go service compilation and meaningful unit checks in the supported Linux toolchain.
- Production Redis Lua against disposable Redis.
- Agent graph/config/report and tool contract checks with deterministic model responses.
- MCP-to-agent observation formatting, errors and current-evidence preservation.
- Maintained experiment/scorer/lifecycle tests and the frozen historical evaluator's existing checks if that code remains runnable and in the supported check inventory.
- A controlled transaction smoke/reconciliation check on a fresh isolated stack, including duplicate and invalid-input behavior, without touching existing stacks. Use credential-free fixtures where defined; distinguish that check from real GeoIP-data validation.
- Build/import/CLI/config checks, current documentation links and no stale references caused by moves.
- Setup/tests from a clean tracked checkout or disposable worktree using the final commits, not only an environment that has untracked files or installed leftovers. Keep private environment variables local; do not copy `.env` into a published tree.

Produce `docs/SIMPLIFICATION_REPORT.md` with:

1. Baseline branch/tag/SHA and verified remote; current final integration branch.
2. Phase branches, their base/implementation commit references, merge references and actual checks. Use Git to obtain references; do not invent SHAs or attempt to include a final commit's own SHA inside its contents.
3. Files/modules changed and reasons; deliberate behavior corrections separated from pure reorganization.
4. A short worked transaction walkthrough and a short worked fake-model investigation walkthrough, each tied to actual entry points and artifacts.
5. Verification results, environment and limitations. List omitted checks explicitly; historical results and fake-model tests cannot stand in for fresh model evaluation.
6. A short later-work list: real model endpoint admission, fresh model experiments after behavior/prompt/tool changes, human review and eventual evidence-backed resume bullets. Do not claim the cleanup validates agent accuracy.
7. Recovery instructions: how to inspect the baseline and create a new restore branch from its tag with a clean worktree. Recover through a new branch or revert a specific integration merge when needed; no destructive reset, deletion of ignored evidence or automatic force-push.

If a component needed no substantive rewrite, say so and show its inspected contract/checks. Completion does not require creating changes in every source file.

Commit and push the final phase, merge/test/push the integration branch, and verify remote tips. Ensure maintained source, tests, required fixtures and current docs are tracked and the final working tree is clean except for explicitly inventoried private ignored artifacts. Keep the backup and phase branches/tags. Do not merge to the original/default branch.

## 15. Check commands already present at the baseline

Confirm these entry points before using them; update them when an intentional package/path change requires it. Run dependency-heavy Python tests in the appropriate environment; do not assume the global interpreter has MCP/LangGraph/grpc installed.

```powershell
python -m unittest tests.test_agent_contract tests.test_v2_scoring tests.test_experiment_stack -v
python -m unittest tests.test_redis_window tests.test_rate_limit -v
.\agent\venv\Scripts\python.exe -m unittest tests.test_agent_graph -v
.\mcp-server\venv\Scripts\python.exe -m unittest tests.test_mcp_metrics -v
python -m unittest discover -s eval/semantic_eval/tests -t . -q
```

Existing Go build/check commands use Linux Docker images with `go test -tags musl` for `./go-server`, `./go-enricher`, `./pb/...` and `./internal/...`. The baseline README contains image-specific commands. A safe alternative is running the current source mounted read-only into an appropriate existing build image with a disposable writable build cache. Verify that the checked source is current; a stale image compiling its own old source is not evidence for the refactor.

Existing `tests.smoke_pipeline` and `experiments.pipeline_probe` are starting points for isolated checks, not permission to operate old stacks. Do not run the old evaluation CLI with write options against historical artifacts while only trying to validate it.

For Windows file operations, resolve archive/copy/cleanup targets and verify they remain within the intended workspace or the disposable directory created by this execution. Use one shell end-to-end and native literal-path operations. Do not compose recursive deletion across PowerShell and cmd. Prefer preserved files and Git moves over deletion.

## 16. Autonomous behavior, blockers and completion

Continue through routine design choices, ordinary fixes, targeted tests, phase commits, pushes and integration without asking the user to make low-level decisions. Give short progress updates showing completed phases, observed checks and next work. Do not wait for user replies merely because a phase ended.

Handle an interruption or genuine external blocker honestly. Backup/authentication failure, missing permission, unresolved merge conflict, unavailable required Docker verification, or missing credentials for a required real-data check cannot be solved by writing “passed.” Complete independent authorized work where the hard baseline gate permits it, checkpoint safely, and report exactly what remains. Do not bypass a tool approval, disable checks, create paid infrastructure, or print `All done` while required cleanup gates remain blocked.

Real-model/GPU evaluation is deliberately deferred and does not block completion of this simplification task. State that distinction in the final report. A required supported code/build/integration check is different from a deferred model benchmark.

Success requires all phases completed, required applicable checks passed, history/evidence preserved, verified baseline and phase/integration pushes, a clean tracked checkout and a factual handoff report. The final response should briefly name the integration branch, report path, checks and deferred model evaluation. Its final line must be exactly:

```text
All done
```

If those conditions are unmet, report `Blocked` or `Incomplete`, identify the last safe checkpoint and remaining work, and do not use the success phrase.

## 17. Starting prompt to paste into Claude Code

```text
Read ./Simplify.md completely and execute phases 00 through 08 autonomously.

I authorize the scoped source cleanup, archival moves, meaningful tests,
phase commits, and pushes to the existing primary origin remote described
there. First capture ALL legitimate current source changes, including
untracked V2 files, in a baseline commit on a new backup branch; push the
branch and baseline tag and verify their remote SHAs BEFORE refactoring.
Keep credentials and private ignored artifacts out of Git and preserve
historical evidence locally as instructed.

Use one reviewable branch per phase, follow the dependency/ownership plan,
and test then merge accepted phases into a separate integration branch.
Commit and push each completed phase and the tested integration result.
Do not merge into my original/default branch, rewrite history, or delete
the backup. Use regression tests for confirmed defects and characterization
checks for risky behavior-preserving changes. Explain the final flow in the
current documentation and archive obsolete prose with an index.

Make routine decisions yourself and keep going without phase-by-phase
approval. Do not rent a GPU, call paid models, or rerun/alter historical
model results. Respect actual permission/authentication blockers; never
bypass them or claim skipped checks passed. Resume from your Git checkpoints
if interrupted. Do not stop after presenting another plan.

When all required cleanup phases, checks, commits and verified pushes are
complete, give the concise handoff required by Simplify.md and end with
the exact line: All done
```
