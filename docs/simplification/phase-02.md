# Phase 02 — Go pipeline

- Branch: `simplify/20261026-222418/02-pipeline`
- Base: `fe46afbe466ded741262ca8748ef15818e6f1c29` (Phase 01)
- Owned files: `go-server/`, `go-enricher/`, `internal/`, `go.mod`/`go.sum` (no version changes), the two Lua tests' source paths (`tests/test_rate_limit.py`, `tests/test_redis_window.py`), `fault-injection/README.md` (fault 15 decision only).
- Callers checked: Dockerfiles copy `go-server/`, `go-enricher/`, `internal/`, `pb/`; the two Python Lua tests read the scripts from Go source by regex; no other code imports these packages.

(Work in progress; completed below when the phase is accepted.)
