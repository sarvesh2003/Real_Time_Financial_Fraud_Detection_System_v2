"""
Fixture builders for the semantic-eval tests.

Every fixture is written into a throwaway temp directory shaped like the real
`eval/runs/faultNN/` layout, so the loader under test is exercised through the
same discovery path it uses on the repository. The result dicts are cut down
from a real artifact (`eval/runs/fault09/fault9_kaggle-qwen14b_ragon.result.json`)
and keep its field names exactly; nothing here invents a schema.
"""

from __future__ import annotations

import json
from pathlib import Path


def result_doc(run_id: str = "fault9_probe_ragon", **overrides) -> dict:
    """A complete automated-run result record, trimmed to the fields used."""
    doc = {
        "run_id": run_id,
        "fault_id": "9",
        "fault_family": "network",
        "fault_name": "latency_injection",
        "env_label": "probe",
        "retrieval_mode": "on",
        "provider": "ollama",
        "model_id": "qwen2.5:14b-instruct",
        "status": "completed",
        "root_cause_id": "go-server-network-latency",
        "summary": "go-server shows 200ms added latency on every Redis call.",
        "reasoning": "Latency rose at the change timestamp while Redis stayed healthy.",
        "confidence": "confirmed",
        "model_self_reported_confidence": "confirmed",
        "confidence_was_downgraded": False,
        "downgrade_reason": None,
        "cited_evidence_indices": [1, 2],
        "distinct_supporting_tools": ["get_metrics", "list_recent_changes"],
        "n_cited": 2,
        "cited_baseline_fraction": 0.5,
        "baseline_evidence_count": 2,
        "calls_used": 2,
        "duplicate_calls": 0,
        "tool_errors": 0,
        "hallucinated_tool_calls": 0,
        "empty_result_calls": 0,
        "wall_clock_s": 240.5,
        "llm_calls": 3,
        "input_tokens_total": 50000,
        "output_tokens_total": 1800,
        "evidence_groundedness": None,
    }
    doc.update(overrides)
    return doc


def trace_events(run_id: str = "fault9_probe_ragon", fault_id: str = "9", **overrides) -> list[dict]:
    """
    A four-event trace: two free baseline observations, one agent-chosen tool
    call, one conclusion. Event shapes copy `agent/graph.py`'s `trace_event`
    payloads, including `node == "baseline"` for the scripted sweep.
    """
    cited = overrides.get("cited_evidence_indices", [1, 2])
    return [
        {
            "ts": "2026-09-12T22:40:00+00:00",
            "event": "investigation_started",
            "run_id": run_id,
            "env_label": "probe",
            "retrieval_mode": "on",
            "trigger": "Something seems off with the fraud pipeline, can you investigate?",
            "fault_id": fault_id,
        },
        {
            "ts": "2026-09-12T22:40:01+00:00",
            "event": "tool_call",
            "node": "baseline",
            "tool_name": "get_container_state",
            "args": {},
            "result": "go-server: state=running, status=\"Up 3 minutes\"",
            "tool_ms": 12.0,
        },
        {
            "ts": "2026-09-12T22:40:02+00:00",
            "event": "tool_call",
            "node": "baseline",
            "tool_name": "list_recent_changes",
            # The real tool emits an opaque change id and the word "inject" and
            # nothing else - no mechanism, no target, no script name. Copied
            # verbatim in shape, because a fixture that named the script would
            # make the trace judge look like it was leaking ground truth when
            # the leak was in the fixture.
            "args": {"window": "24h"},
            "result": "2026-09-12T22:40:02Z | change=CHG-346b4524 | inject",
            "tool_ms": 9.0,
        },
        {
            "ts": "2026-09-12T22:41:00+00:00",
            "event": "action_selected",
            "node": "investigate",
            "action": {"type": "tool_call", "name": "get_metrics", "args": {"promql": "latency"}},
            "calls_used": 0,
            "retrieved": [],
            "input_tokens": 6220,
            "output_tokens": 130,
            "finish_reason": "tool_calls",
        },
        {
            "ts": "2026-09-12T22:41:05+00:00",
            "event": "tool_call",
            "node": "tool_call",
            "tool_name": "get_metrics",
            "args": {"promql": "latency"},
            "result": "go_server_redis_call_seconds{quantile=\"0.99\"} 0.201",
            "calls_used": 1,
            "tool_ms": 40.0,
        },
        {
            "ts": "2026-09-12T22:42:00+00:00",
            "event": "conclusion",
            "node": "conclude",
            "conclusion": {
                "root_cause_id": "go-server-network-latency",
                "summary": "go-server shows 200ms added latency on every Redis call.",
                "confidence": "confirmed",
                "reasoning": "Latency rose at the change timestamp while Redis stayed healthy.",
                "cited_evidence_indices": cited,
                "distinct_supporting_tools": ["get_metrics", "list_recent_changes"],
            },
            "input_tokens": 12944,
            "output_tokens": 205,
            "finish_reason": "stop",
        },
    ]


def write_run(
    runs_dir: Path,
    fault_dir: str = "fault09",
    stem: str = "fault9_probe_ragon",
    result: dict | str | None = None,
    trace: list[dict] | str | None = None,
) -> Path:
    """
    Materialize one run. `result`/`trace` accept a raw string so a test can
    write deliberately malformed bytes. Passing None for `trace` writes no
    trace file at all, which is what the hand-transcribed rows look like.
    """
    d = runs_dir / fault_dir
    d.mkdir(parents=True, exist_ok=True)

    result_path = d / f"{stem}.result.json"
    if result is None:
        result = result_doc(run_id=stem)
    if isinstance(result, str):
        result_path.write_text(result, encoding="utf-8")
    else:
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if trace is not None:
        trace_path = d / f"{stem}.trace.jsonl"
        if isinstance(trace, str):
            trace_path.write_text(trace, encoding="utf-8")
        else:
            trace_path.write_text(
                "\n".join(json.dumps(e) for e in trace) + "\n", encoding="utf-8"
            )
    return result_path


def tree_digest(root: Path) -> dict[str, str]:
    """Path -> sha256 for every file under root, for immutability assertions."""
    import hashlib

    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out
