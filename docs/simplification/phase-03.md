# Phase 03 — MCP observation tools

- Branch: `simplify/20261026-222418/03-mcp`
- Base: `fe46afbe466ded741262ca8748ef15818e6f1c29` (Phase 01)
- Owned files: `mcp-server/server.py`, new `tests/test_mcp_tools.py`; `tests/test_mcp_metrics.py` unchanged and still passing.
- Callers checked: `agent/run.py` starts `mcp-server/server.py` over stdio and requires the five tool names; `tests/test_mcp_metrics.py` imports it; the historical `eval/` re-reads its docstrings from the working tree (an existing, documented limitation); `mcp-server/test_client.py` is a manual helper.

## Responsibility

Produce neutral, bounded, current observations from Docker and Prometheus for the five read-only tools. Phase 04 owns the agent-side cap and the end-to-end check.

## Observation contract (producer side)

- Every reply is at most `MAX_RESPONSE_CHARS`, read from `OBSERVATION_MAX_CHARS` (default 2000; Phase 04 makes the agent pass its limit).
- `query_logs`: validates service name, `since` (Docker duration or RFC3339) and `grep` before calling Docker; keeps the **newest** of at most 500 matching lines that fit, with a header `[K newest of M matching lines (of N logged since S); older lines omitted]`. A single over-long line is cut. Filter-miss and no-log replies are unchanged.
- `get_metrics`: strict window (`5m`, `1h30m`; rejects `0m`, `5mfoo`, `5x`); header `[N series; query end <UTC> = unix <t>]`; per series the four newest `unix:value` points and the window range; at most 10 series and as many as fit, then `[K of N series omitted ...]`. A failed 6 h follow-up lookup is now an `ERROR`, not "no series". Non-JSON replies report the HTTP status.
- `list_metric_names`: bounded, with an omitted-name count.
- `get_container_state`: accepts NDJSON or one JSON array; reports unparseable lines instead of dropping them silently.
- `read_config`: if every Redis key fails, one `ERROR:` reply (previously five "key: ERROR" lines that the agent's evidence rule counted as usable); partial failures keep good values. Environment allow-list and secret filtering unchanged.
- All five tools declare `readOnlyHint`. Docker calls are argument lists ending in `-- <service>`; no shell.

## Deliberate behavior changes

1. Newest-evidence truncation inside the tool (D5 producer side).
2. Removed prescriptive text from neutral output (D6): the "exporter lost its connection to the real backend", "usually means", and "may be blocked, not just slow" phrasing; the no-series reply now states the fact and points to `list_metric_names`.
3. Input validation for `since`, service name and window; strict duration parsing.
4. Tool annotations added (additive MCP schema metadata; names and arguments unchanged).

## Checks run

| Check | Result |
| --- | --- |
| `tests.test_mcp_tools` (22 tests) against the **baseline** server | 3 preserved-contract groups passed; 16 failures + 3 errors = the defects/gaps above (busy log lost the newest line, unbounded errors/names, swallowed follow-up failure, prescriptive text, no omitted-series count, lenient window/since, no annotations, all-failed Redis config looked usable, JSON array unsupported, unparsed lines hidden) |
| `tests.test_mcp_tools` + `tests.test_mcp_metrics` after the change (MCP venv) | 23 passed |
| Busy-log probe: 1,201 lines | 1,995-character reply, header "106 newest of 1201", last line is the newest failure |
| Real stdio server via `mcp-server/test_client.py` with `PROMETHEUS_URL` closed and an empty `COMPOSE_PROJECT_NAME` | neutral `ERROR` and "(no containers found for this stack)": the environment selects the project |
| Historical semantic-eval tests; in-process validity checks (no write) | 256 passed; unchanged 10 fail / 2 warn / 1 skip / 1 pass |

## Limitations

The character limit bounds text, not model tokens. Metric series order is Prometheus's; the most relevant series can still be among those omitted, but the reply now says so. Tool docstrings changed slightly, so tool-schema hashes in future traces will differ from historical ones.

## Why it is easier to follow

Each tool validates input, makes one command or HTTP call through a shared helper, and formats through two bounding helpers (`fit` for errors, `newest_lines` for logs), so where truncation happens and what it keeps is visible in one place.
