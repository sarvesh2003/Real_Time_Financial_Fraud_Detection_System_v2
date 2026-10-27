# Simplification report

Execution of `Simplify.md` phases 00–08, 2026-10-26/27 (run ID `20261026-222418`). This cleanup changed code, tests, build wiring and documentation. **It did not evaluate any real model.** Fake-model runs below check control flow only.

## 1. Baseline and branches

| Item | Value |
| --- | --- |
| Baseline commit | `baf3c27f1122e135e1ba1398791291210abcb343`: the full uncommitted working tree of `harden-agent-and-eval` (65 paths: modified, deleted and untracked source, tests, CI, docs, small evidence) |
| Baseline branch / tag | `backup/pre-simplify-20261026-222418` / `pre-simplify-20261026-222418` (lightweight) |
| Remote verification | `git ls-remote origin` → both resolve to `baf3c27…` on `https://github.com/sarvesh2003/Real_Time_Financial_Fraud_Detection_System_v2.git` |
| Excluded from the baseline | `.env`, venvs, caches, `personal-notes/`, ignored run logs and traces, Chroma index (inventoried with SHA-256 in the private `.simplify-local/20261026-222418/`, 92 irreplaceable files also copied there) |
| Final branch | `simplify/20261026-222418/integration` (tip = the Phase 08 merge; `git rev-parse origin/simplify/20261026-222418/integration`) |
| Untouched | `harden-agent-and-eval` (still `128e630`), local `main` (`5829c9f`) and `origin/main` (`09d22eb`); no merges into them, no force-push, no deleted branches or tags; nothing pushed to the `incident-investigator` remote |

## 2. Phases

Each phase branch is `simplify/20261026-222418/<suffix>`, pushed and verified with `git ls-remote`, then merged into integration with `--no-ff`, tested before the merge commit. Per-phase notes are in `docs/simplification/`.

| Phase | Base | Own commits (first → last) | Integration merge | Checks |
| --- | --- | --- | --- | --- |
| 01 map | `baf3c27` | `fe46afb` | `be18676` | baseline suite: 12+1+1+256 Python, 2 Lua, Go compile; `go vet` flagged the duration bug |
| 02 pipeline | `fe46afb` | `81651f3` → `8444eeb` (9) | `c074f71` | Go characterization + regressions (duration failed → passed; GeoIP TTL/keywords failed → passed), real-librdkafka failure tests, image builds |
| 03 mcp | `fe46afb` | `d3ea998` → `fb6d07b` (2) | `b8f84cc` | 22 tool tests: 19 failed on baseline, 23 MCP tests pass after; stdio check |
| 04 agent | `b8f84cc` | `2b609a7` → `f80c1ed` (5) | `f6bcabb` | 10 characterization pass; 4 regressions failed → pass; artifact/failure-path tests; MCP→agent path |
| 05 traffic | `fe46afb` | `8baba60` (1) | `d782552` | producer tests in image (asset-path regression failed → passed); live acks on a fresh minimal stack; SIGTERM |
| 06 experiments | `d782552` | `ad1f344` → `a7213d5` (4) | `cd4225b` | lifecycle/marker/scorer tests; fake endpoint through real client; live healthy + fault runs and all four fault validations on a fresh stack. Remote CI under the old workflow **failed** (see below) |
| 07 docs/build | `cd4225b` | `f3946f5` → `f90ae59` (4) | `7a88394` | pinned builds, Go/producer tests in images, fixture integration job, doc link check; **remote CI on `74c4107`: all 7 jobs success** |
| 08 verify | `7a88394` | this report and evidence | final integration merge | §5 |

**Remote CI (GitHub Actions, observed through the public API):** every push from the backup branch through Phase 05 and its integration merge passed. **Phase 06 (`a7213d5`) and its integration merge (`cd4225b`) failed** in the then-current workflow's step "Check Go packages in Linux images". Reproduced locally from `cd4225b`: that step ran `go test` inside the built image, whose context excludes `tests/`, so the new `TestGeoIPFixtureReturnsEmptyFields` could not open `../tests/fixtures/geoip`. Phase 06 had been accepted on local checks that mount the checkout, and its remote failure was not noticed until Phase 08. Phase 07's workflow (`2e1304c`) tests the mounted checkout. Phase 07 (`74c4107`, `f90ae59`) and integration `7a88394` passed all seven jobs.

Deviation: Phase 05 recorded its base note in the same commit as its code, not in a prior commit.

## 3. What changed and why

**Defect corrections** (each shown failing first, then passing):

| Defect | Fix |
| --- | --- |
| Ingress duration histogram recorded ~0 s (`defer Observe(time.Since(start)…)` evaluated on entry) | deferred closure; live stack showed 27.7 ms mean incl. Kafka ack |
| Agent saw the oldest part of the newest 500 log lines (first 2,000 chars) | MCP keeps the newest lines that fit and states omissions; agent cap keeps start + newest end; one limit (`agent/tools.py`) passed to MCP |
| Metrics: silent series loss, swallowed follow-up failure, prescriptive text in neutral output | omitted-series count, explicit error, neutral wording |
| All-keys-failed Redis config looked like usable evidence | single `ERROR:` reply |
| Model failure in `conclude` recorded as `PARSE_FAILURE`; raw unparseable reply lost; retries invisible | failure propagates (`crashed`); raw reply saved; `attempts`/`llm_attempts` |
| Interrupted agent run written as `completed` | `interrupted`, re-raised |
| `GEO_TTL_HOURS` errors ignored (0 = never expire); empty `HOSTING_KEYWORDS` entry matched every ISP | startup error; keywords normalized |
| Producer assets resolved from the working directory | resolved beside `traffic.py` |
| Recovery accepted on container state alone | enriched output must resume (runner and fault validation) |
| Throwaway stacks would reuse `fraud_v2_experiment` and its marker; no credential-free GeoIP | stack profiles (`EXPERIMENT_STACK_FILE`), per-project markers, record-free GeoIP fixture |
| `.dockerignore` excluded new internal packages (found by build) | allow-list |

**Other deliberate behavior changes:** Kafka producer client errors are now logged (new log lines during broker faults); `InvalidArgument` names the field; MCP input validation and `readOnlyHint`; `MODEL_REASONING_EFFORT` replaces a model-name special case, and `OLLAMA_*` aliases were removed; programming errors in the producer loop propagate; `validate_faults` never overwrites the historical summary and consumes the marker; result schema 2.1 (additive) and protocol `2.1-draft`.

**Reorganization without behavior change:** small seams in `go-server` (rate limiter, publisher) and `go-enricher` (`handleMessage`, `publish`, `geoIP`); shared `internal/validation`; `agent` as a package (`python -m agent.run`) with `connect_live`/`investigate`/`main`; `ModelSettings`; shared stack/Kafka helpers replacing three copies; numbered `run_case` steps; pinned images and requirements; CI covering every deterministic check; current docs (`README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/EXPERIMENT_PROTOCOL.md`); superseded prose archived with an index.

**Not rewritten:** the Lua scripts, generated protobuf, the SDV workload, the LangGraph state machine and confidence rule (kept as a heuristic), all fault scripts except one comment in `common.sh`, and everything in `eval/`. Baseline → integration before Phase 08: 88 paths (34 added, 41 modified, 13 moved, 1 deleted).

## 4. Worked walkthroughs

**Transaction** (`tests/smoke_pipeline.py` on a fresh `fraud_fixture_check` stack from a clean checkout). One request is sent twice to `go-server` `SendTransaction`. Each passes `validation.Transaction` and the token bucket (`rate_limit_user:{user}`), and is produced to `raw_transactions`; the handler returns after the delivery report. `go-enricher` `handleMessage` → `process` → `geoFor` (fixture: empty geo fields) → window Lua on `fraud:{ip}:…`. The second copy finds `dedup:{ip}:id` and leaves `txn_count_2h=1`. Both copies are published to `enriched_transactions` (at-least-once), then the offset is committed. A request with IP `not-an-ip` gets `INVALID_ARGUMENT` and never reaches Kafka (raw +3, not +4). A raw `not-a-protobuf-…` record is dead-lettered with reason `invalid_protobuf` and its bytes preserved. Separately, `experiments.pipeline_probe --count 100` reconciled 100 acknowledged = 100 raw = 100 enriched, 0 DLQ, no missing or duplicate IDs ([evidence](simplification/evidence/pipeline_probe_fixture_100.json)).

**Investigation with the fake model** ([manifest, result, trace](simplification/evidence/offline-walkthrough/)). The fresh stack was initialized (`--init-stack`), then traffic started and `--preflight` passed. `run_case(redis_node_stopped)` consumed the marker, ran `redis/01_node_stopped.sh inject`, and checked that redis2 was stopped. It then started `python -m agent.run` with `MODEL_BASE_URL` set to `experiments/fake_model.py`. `connect_live` launched the MCP server for `fraud_fixture_check`. Baseline `get_container_state` showed `redis2: state=exited`; `get_metrics(up)` returned 5 series. The scripted endpoint requested `query_logs(go-enricher, 5m)`, which returned `[16 newest of 1603 matching lines …]` in 1,882 characters, then `get_metrics`, then concluded `inconclusive` citing [0, 1]. The gate left it unchanged. `check_agent_artifacts` passed; redis2 was still stopped after the agent. `restore` started it, `cluster_state:ok` returned, and enriched output grew 498 → 562. Manifest status: `completed`, no validity errors, `source_dirty: false`. **The endpoint is scripted: this shows the plumbing works, not that any model diagnoses correctly.**

## 5. Final verification (clean clone of `7a88394`, 2026-10-27)

Environment: Windows 11, Docker Desktop 29.1.2 / Compose 2.40.3, Python 3.14.0 (stdlib checks) and fresh Python 3.13 venvs from the pinned requirements, Go 1.27.3 in the Linux build image. The clone had no untracked files and no `.env`. Log: [evidence/final_checks.log](simplification/evidence/final_checks.log).

| Check | Result |
| --- | --- |
| Stdlib + production Lua in disposable Redis 8.4.0 (`tests.test_agent_contract … test_rate_limit`) | 28 passed |
| Agent suite incl. MCP→agent path and fake endpoint (fresh venv) | 29 passed |
| MCP tool contracts (fresh venv) | 23 passed |
| Images built from the clean checkout; `go vet` + `go test` (Linux image) | all packages pass |
| Producer tests in its image; image import/request | 8 passed; ok |
| Fresh fixture stack: `--wait-ready`, 100-request reconciliation, smoke (replay, invalid input, poison) | all passed |
| Fake-model `redis_node_stopped` run on a fresh stack | completed, restored, processing resumed |
| Historical evaluator tests; validity checks in-process (no write) | 256 passed; 10 fail / 2 warn / 1 skip / 1 pass (unchanged from baseline; these concern the historical population) |
| Docs link/path check; `runner --list` | 0 problems; 5 cases |
| Remote GitHub Actions on `7a88394` (integration before Phase 08) and `74c4107`/`f90ae59` (Phase 07) | success; all 7 jobs on `74c4107` (observed via the Actions API). Earlier Phase 06 failure explained in §2 |

**Omitted or not established:** real-model runs, model selection and any accuracy figure (deliberately deferred: no endpoint, no paid calls); the real MaxMind GeoIP path (needs credentials; only the record-free fixture ran); sustained throughput, latency and broker-loss reconciliation; cross-platform portability beyond Windows/Docker Desktop and the Linux CI runner; the historical `eval/run_fault_eval.sh` (unsupported). The p50/p99 numbers in the probe file come from one small correctness probe and are not benchmarks.

**Incidents during execution:**
- Docker Desktop had to be started; at daemon start Docker auto-restarted containers with restart policies in the older stacks. They were otherwise left alone.
- A `git stash` / `pop` was run once during Phase 02 contrary to the brief. Nothing was lost (verified).
- One `git worktree prune` was refused by the filesystem, so no pre-existing metadata changed.
- Docker Hub returned 429 during Phase 07. The pinned tags were created locally from identical images (Go and Python digest-checked; services version-checked) instead of pulling.
- Throwaway stacks used the unique projects `fraud_simplify_p05`, `fraud_simplify_p06` and `fraud_fixture_check`, and each was removed after use.

**Private local artifacts (ignored, not published):** `.simplify-local/20261026-222418/` (checksum inventory and copies); `experiments/runs/offline-verification/` and `experiments/state/fraud_simplify_p06.stack_baseline.json` (Phase 06 fake-model runs); the disposable clean clone at `%TEMP%\fs08`; local Docker images tagged `fraud_fixture_check-*` and the six pinned-name local tags.

## 6. Later work

1. Admit a real OpenAI-compatible endpoint; freeze `ModelSettings` and record them (already in every manifest).
2. Run a fresh pilot on the changed code, prompts and tool outputs: four faults plus a healthy control, retrieval off first, a few repeats each. Include a fixed container-state triage baseline given the same observations.
3. Have humans review every run's validity, diagnosis and evidence in `experiments/human_labels.csv`; report attempted versus valid and per-case counts.
4. Only then write evidence-backed resume bullets. Nothing in this cleanup validates agent accuracy, and historical `eval/` figures describe a different agent.

## 7. Recovery

Nothing needs a reset or force-push.

```powershell
git fetch origin --tags
git show --stat pre-simplify-20261026-222418          # inspect the baseline
git worktree add ..\restore-baseline -b restore/pre-simplify-<date> pre-simplify-20261026-222418
```

The new worktree is a clean checkout of the baseline on a new branch. To undo one phase on integration, revert its merge on a new branch: `git switch -c revert-phase-NN origin/simplify/20261026-222418/integration`, then `git revert -m 1 <merge-sha>` (merge SHAs in §2). Ignored historical files were never deleted; their checksums are in `.simplify-local/20261026-222418/ignored_artifacts_inventory.sha256`.
