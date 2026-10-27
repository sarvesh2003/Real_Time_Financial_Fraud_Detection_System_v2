# Phase 03 — MCP observation tools

- Branch: `simplify/20261026-222418/03-mcp`
- Base: `fe46afbe466ded741262ca8748ef15818e6f1c29` (Phase 01)
- Owned files: `mcp-server/server.py`, new `tests/test_mcp_tools.py`; `tests/test_mcp_metrics.py` kept.
- Callers checked: `agent/run.py` starts `mcp-server/server.py` over stdio and requires the five tool names; `tests/test_mcp_metrics.py` imports it; the historical `eval/` reads its tool docstrings (re-review of the working tree, already a documented limitation); `mcp-server/test_client.py` is a manual helper.

(Work in progress; completed when the phase is accepted.)
