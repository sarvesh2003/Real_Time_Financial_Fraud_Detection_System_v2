# Phase 02 — Go pipeline

- Branch: `simplify/20261026-222418/02-pipeline`
- Base: `fe46afbe466ded741262ca8748ef15818e6f1c29` (Phase 01)
- Owned files: `go-server/`, `go-enricher/`, `internal/`, `go.mod` (client_model moved from indirect to direct; no version changes), `.dockerignore` (internal packages), the Lua tests' source path (`tests/test_rate_limit.py`), `fault-injection/README.md` (fault 15 decision only).
- Callers checked: Dockerfiles copy `go-server/`, `go-enricher/`, `internal/`, `pb/`; the two Python Lua tests read the scripts from Go source by regex; nothing else imports these packages.

## Responsibility

Ingress validation, rate limiting and acknowledged raw delivery; enrichment, DLQ and commit ordering; shared Redis configuration and input validation.

## Changes

| Kind | Change |
| --- | --- |
| Fix (D1) | `go-server/server.go`: request duration observed in a deferred closure, so it covers the Redis and Kafka wait. |
| Fix (D3) | `go-enricher/enricher.go`: `GEO_TTL_HOURS` must be a positive integer (it was silently replaced by 24 h, and 0 meant "never expire"); `HOSTING_KEYWORDS` entries are trimmed, lower-cased and empties dropped (an empty entry used to match every ISP). |
| Behavior addition | Both services drain the Kafka producer `Events()` channel and log `kafka.Error`. Broker errors now appear in service logs during faults. |
| Message change | `InvalidArgument` message now names the failing field (`invalid transaction: ip_address is not an IP address`). The status code is unchanged. |
| Refactor | `go-server`: `rateLimiter` and `rawPublisher` seams; token-bucket Lua and limiter in `redis.go`; produce-and-wait in `kafka.go`. |
| Refactor | `internal/validation.Transaction`: one input rule used by ingress and enricher. |
| Refactor | `go-enricher`: `configFromEnv` once at startup; `handleMessage` = process then commit; `geoIP.lookup` returns `GeoData`; cache returns `(geo, found, err)`; processor publishes through a `publish` func; `deadLetterMessage` builds the DLQ record; `utils.go` removed. Lua script text unchanged. |
| Build fix | `.dockerignore` admitted only `internal/redisconfig`; now `internal/*/*.go`. Found by the Linux image build. |
| Decision (D4) | Fault 15 stays unchanged and is documented as historical and not implementing zero backoff (`fault-injection/README.md`). The parser still rejects negative values. |

## Preserved contracts

Topics, keys, DLQ JSON fields, status codes, idempotent `acks=all` producers with a 10 s delivery timeout, output-before-commit, crash-and-replay on processing error, Redis key names and Lua scripts, metric names and labels.

## Checks run

Unless noted: `go vet` + `go test -tags musl -count=1 ./go-server ./go-enricher ./pb/... ./internal/...` with the current source mounted read-only, network disabled.

| Check | Result |
| --- | --- |
| Characterization tests on refactored ingress (before the D1 fix): invalid input ×6, limiter error/deny, Kafka enqueue/delivery, deadline | pass |
| `TestRequestDurationIncludesKafkaAcknowledgment` before fix | **failed**: "observed 0.0000s, want at least the 0.080s Kafka wait" |
| Same after fix | pass; `go vet` no longer reports `time.Since is not deferred` |
| Real librdkafka against a closed port: delivery report becomes `errKafkaDelivery`; deadline returns `DeadlineExceeded`; enricher `publishSync` returns an error | pass |
| Enricher: deliver-then-commit order; no commit after processing or DLQ-delivery failure; commit error returned; poison protobuf and invalid input dead-lettered without Redis, preserving source topic/partition/offset/key/payload | pass |
| `TestConfigRejectsInvalidGeoCacheTTL`, `TestConfigIgnoresEmptyHostingKeywords` before fix | **failed** (3 TTL values accepted; empty keyword matched "Comcast Cable") |
| Same after fix | pass |
| `docker build` of both Dockerfiles, tags `fraud-simplify-20261026/{go-server,go-enricher}:phase02`, fresh `golang:alpine` (go1.27.3) | pass after the `.dockerignore` fix |
| Full Go suite inside the new image | all packages pass; `pb` has no tests |
| `python -m unittest tests.test_redis_window tests.test_rate_limit` (production Lua, disposable Redis) | 2 passed |
| `gofmt -l` on LF-normalized copies | clean |
| `git diff --check` on each commit | clean |

Inherited/environment notes: the Windows checkout uses `core.autocrlf=true`, so gofmt must be checked on LF copies; `git archive` output is also CRLF, so baseline gofmt status could not be measured this way. Kafka delivery and replay against a real broker, and the Redis/GeoIP enrichment path, are exercised by the isolated-stack smoke and reconciliation checks in Phase 08, not here.

## Limitations

The Go Redis client path (cache, window decode) has no Go-level test against Redis; the Lua is tested from Python and the Go decode path end to end. At-least-once remains the guarantee: replay re-publishes enriched records.

## Why it is easier to follow

`SendTransaction` reads top to bottom as validate → limit → marshal → publish, with one switch mapping errors to status codes. The enricher loop only polls and dispatches. `handleMessage` states the commit rule in one place, and `process` reads as decode → validate → geo → window → publish.
