# Phase 07 — archive, current docs, build and CI

- Branch: `simplify/20261026-222418/07-docs-build`
- Base: `cd4225b8737227166accd76b3c5c2c8473616837` (integration with phases 01–06)
- Owned files: `archive/pre-simplify/`, `README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/EXPERIMENT_PROTOCOL.md`, `eval/README.md` (label only), Dockerfiles, `.dockerignore`, `docker-compose.yaml` image tags, `.env.example`, Python requirement files, `.github/workflows/verify.yml`, `experiments/fixture_stack.json`, `experiments/runner.py` (`--wait-ready` only), Lua test image tag, one stale comment in `fault-injection/common.sh`.

## Archive

Moved with `git mv` (contents unchanged, baseline-relative paths kept) into `archive/pre-simplify/`: both root plans/audits, the 2026-10-14 review, the completion plan, the legacy README, the legacy producer script, the stale public Compose file and `Chatgpt_Documentation/`. The [index](../../archive/pre-simplify/README.md) gives the baseline tag/SHA, original paths, the reason each was archived and what was carried into current docs. Not moved: `Simplify.md`, the runtime prompt, fixtures, schemas, generated code, requirement files, and `eval/` (left in place, labelled by `eval/README.md`).

**Public Compose decision:** `docker-compose.public.yml` used older prebuilt images without the current observability setup and was never verified for V2, so it is archived; `docker-compose.yaml` is the only supported stack.

## Build and dependency wiring

| Item | Change |
| --- | --- |
| Base images | `golang:1.27.3-alpine3.24` (both Go images, same Dockerfile layout), `python:3.10.22-slim-trixie`. Both are digest-identical to the images built and tested in this run (`sha256:d200d2dd…`, `sha256:6ff50646…`). |
| Service images | `redis:8.4.0`, `prom/prometheus:v3.14.0`, `oliver006/redis_exporter:v1.91.1`, `danielqsj/kafka-exporter:v1.10.0`, `maxmindinc/geoipupdate:v7.1.1` — the versions reported by the images used in the Phase 05/06 live stacks; Confluent 7.4.0 was already pinned |
| Python | `agent/requirements.txt`, `mcp-server/requirements.txt`, `corpus/requirements.txt` pinned to the tested venv versions (agent venv Python 3.13.14); producer pins unchanged. `eval/requirements.txt` (historical) untouched |
| `.dockerignore` | Allow-list of exactly what the three images copy; stale per-service `go.mod` entries removed; test files excluded |
| `.env.example` | Adds `COMPOSE_PROFILES=geoip` and explains the credential-free alternative |
| `experiments/fixture_stack.json` | Tracked throwaway profile (`fraud_fixture_check`, 3xxxx ports, 192.168.250.0/24, record-free GeoIP) |
| `runner --wait-ready N` | One readiness command for CI and docs (services up and all Prometheus targets scraped; producer not required) |

## CI (`.github/workflows/verify.yml`)

Jobs: `go` (build images; vet/test the checkout in the build image), `producer` (tests in its image), `unit` (stdlib tests and Lua on disposable Redis), `agent` and `mcp` (pinned requirements), `integration` (fixture stack: wait-ready → 50-request probe → smoke → down), `historical-evaluator`. No job needs a model, GPU or MaxMind credentials. Remote CI results were not observed in this phase.

## Checks run (local equivalents)

| Check | Result |
| --- | --- |
| `experiments.stack config` with the fixture profile; `docker compose config` with `COMPOSE_PROFILES=geoip` | valid |
| `experiments.stack build` (fixture project) from the pinned Dockerfiles | all three built. The first attempt hit Docker Hub **429 Too Many Requests**; the pinned tags were then created locally from the identical (digest-checked Go/Python; version-checked service) images instead of pulling |
| Go vet + tests in the new `go-enricher` image (go1.27.3) | all packages pass |
| `tests.test_traffic` in the new producer image | 8 passed |
| Integration job, fresh `fraud_fixture_check` stack: `--wait-ready 300`, `pipeline_probe --count 50`, `tests.smoke_pipeline`, `down` | ready; probe passed (50 acknowledged/raw/enriched, 0 DLQ, no missing/duplicate IDs); smoke passed (2 outputs for the replayed ID with `txn_count_2h=1`, invalid IP rejected before Kafka, poison record dead-lettered); no containers left |
| Python suites; historical semantic tests | 28 + 24 + 23 passed; 256 passed |
| Link/path check of current docs (README, docs/, phase notes, fault/eval/archive READMEs) | 0 problems |
| Stale-reference search (`--experiment-stack`, public Compose, `OLLAMA_*`, script-style `agent/run.py`, archived names, change ledger) | only intentional mentions remain (archive links, the alias-removal test, the historical retrieval corpus text) |

## Limitations

Cross-platform portability beyond Windows + Docker Desktop and the Linux CI runner was not checked. The real-GeoIP path (MaxMind credentials) was not run in this cleanup. Pins are to tags; a registry can rebuild a tag, so experiment manifests still record image IDs.
