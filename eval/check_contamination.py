"""
Contamination guard for the fault-investigation eval.

Two independent checks, one static and one on recorded data. Both exist because
a real leak got past review: agent/system_prompt.md:16 states fault 1's
CLUSTERDOWN mechanism outright, rubric.yaml fault 1 accepts it, and three
local-qwen9b runs scored above 0 while making zero tool calls - they recited the
prompt and the grader took it. Nothing in the harness caught that; a human did,
by noticing a full-marks run with an empty tool-call sequence. These checks turn
both halves of that discovery into something the harness enforces by itself.

CHECK 1 - answerable-without-evidence (static)
    For every fault in rubric.yaml, ask whether a score-2 answer is constructible
    from text the model is handed for free. Scoring 2 needs mechanism_any_of AND
    mechanism_and_any_of (when present) AND target_any_of. If all required
    components match somewhere in a free-text source, that fault can be answered
    with no investigation at all, and any point it scores is unearned.

    Free-text sources are anything in the model's context before it probes:
      - agent/system_prompt.md, injected verbatim every run
      - every @mcp.tool() docstring in mcp-server/server.py, sent to the model
        as part of the tool schema whether or not the tool is ever called

    A single component matching is reported as a WARNING, not a failure. Generic
    tokens ("down", "slow") legitimately occur in architecture prose, and the
    rubric would still need the other half. Only the full set is a FAIL.

CHECK 2 - zero-evidence scoring (data)
    Scan eval/results.csv for rows that scored above 0 with calls_used == 0, i.e.
    an answer produced without one tool call. This is the signature that actually
    exposed the leak. Rows already corrected in score_overrides.csv are reported
    as covered rather than failing, so the check stays green once a known case is
    handled but fires again the moment a new one appears.

Matching mirrors grade.py::_any_hit exactly - re.escape + IGNORECASE - so this
cannot pass on a phrase the grader would have accepted.

Usage:  venv/Scripts/python.exe eval/check_contamination.py [--strict]
        --strict also fails on WARNINGs.
Exit:   0 = clean, 1 = contamination found.
"""

from __future__ import annotations

import argparse
import ast
import csv
import re
import sys
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
RUBRIC = EVAL_DIR / "rubric.yaml"
RESULTS = EVAL_DIR / "results.csv"
OVERRIDES = EVAL_DIR / "score_overrides.csv"
SYSTEM_PROMPT = REPO_ROOT / "agent" / "system_prompt.md"
MCP_SERVER = REPO_ROOT / "mcp-server" / "server.py"


def _any_hit(text: str, phrases: list[str]) -> str | None:
    """Identical to grade.py::_any_hit. Kept in lockstep on purpose."""
    for p in phrases:
        if re.search(re.escape(p), text, re.IGNORECASE):
            return p
    return None


def free_text_sources() -> dict[str, str]:
    """Everything the model can read before it has investigated anything."""
    sources: dict[str, str] = {}

    if SYSTEM_PROMPT.exists():
        sources["agent/system_prompt.md"] = SYSTEM_PROMPT.read_text(encoding="utf-8")

    # Tool docstrings ship to the model inside the tool schema, so a docstring
    # that explains a fault's mechanism is as much of a leak as the prompt is.
    if MCP_SERVER.exists():
        tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            is_tool = any(
                "mcp.tool" in ast.unparse(d) or "tool(" in ast.unparse(d)
                for d in node.decorator_list
            )
            doc = ast.get_docstring(node)
            if is_tool and doc:
                sources[f"mcp-server/server.py::{node.name}()"] = doc

    return sources


def check_answerable_without_evidence(sources: dict[str, str]) -> tuple[list, list]:
    rubric = yaml.safe_load(RUBRIC.read_text(encoding="utf-8"))
    failures, warnings = [], []

    for fault in rubric.get("faults", []):
        fid = fault.get("fault_id")
        name = fault.get("script", "?")

        # Exactly the components grade.py requires for a 2.
        required = {"mechanism": fault.get("mechanism_any_of", [])}
        if fault.get("mechanism_and_any_of"):
            required["mechanism_and"] = fault["mechanism_and_any_of"]
        required["target"] = fault.get("target_any_of", [])

        for src_name, src_text in sources.items():
            hits = {
                comp: _any_hit(src_text, phrases)
                for comp, phrases in required.items()
                if phrases
            }
            matched = {c: p for c, p in hits.items() if p}
            if not matched:
                continue
            if len(matched) == len(hits):
                failures.append((fid, name, src_name, matched))
            else:
                warnings.append((fid, name, src_name, matched, set(hits) - set(matched)))

    return failures, warnings


def check_zero_evidence_scoring() -> tuple[list, list]:
    if not RESULTS.exists():
        return [], []

    covered = set()
    if OVERRIDES.exists():
        with OVERRIDES.open(newline="", encoding="utf-8") as f:
            covered = {r["run_id"] for r in csv.DictReader(f)}

    failures, handled = [], []
    with RESULTS.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            calls = (row.get("calls_used") or "").strip()
            score = (row.get("score") or "").strip()
            if calls != "0":
                continue
            if score in ("", "0", "-1"):
                continue
            entry = (row["run_id"], calls, score, (row.get("env_label") or "").strip())
            (handled if row["run_id"] in covered else failures).append(entry)

    return failures, handled


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="fail on warnings too")
    args = ap.parse_args()

    sources = free_text_sources()
    print(f"Scanning {len(sources)} free-text source(s) the model sees before probing:")
    for s in sources:
        print(f"  - {s}")
    print()

    fails, warns = check_answerable_without_evidence(sources)

    print("CHECK 1 - faults answerable with no evidence")
    print("-" * 72)
    if fails:
        for fid, name, src, matched in fails:
            parts = ", ".join(f"{c}={p!r}" for c, p in sorted(matched.items()))
            print(f"  FAIL  fault {fid} ({name})")
            print(f"        every scoring component is present in {src}")
            print(f"        {parts}")
            print("        a model can score 2 here without investigating.")
    else:
        print("  ok - no fault has a complete accept-set reachable from free text.")

    if warns:
        print()
        for fid, name, src, matched, missing in warns:
            parts = ", ".join(f"{c}={p!r}" for c, p in sorted(matched.items()))
            print(f"  warn  fault {fid} ({name}): partial match in {src}")
            print(f"        present: {parts} | still needs: {', '.join(sorted(missing))}")
    print()

    zero_fails, zero_handled = check_zero_evidence_scoring()

    print("CHECK 2 - runs that scored without calling a tool")
    print("-" * 72)
    if zero_fails:
        for run_id, calls, score, env in zero_fails:
            print(f"  FAIL  {run_id} ({env}) scored {score} with calls_used={calls}")
            print("        no tool was called; this answer is not grounded in evidence.")
    else:
        print("  ok - no unreviewed row scored above 0 with zero tool calls.")
    if zero_handled:
        print(f"  ({len(zero_handled)} such row(s) already corrected in score_overrides.csv:")
        for run_id, _, score, env in zero_handled:
            print(f"     {run_id} ({env}) -> {score})")
    print()

    hard = len(fails) + len(zero_fails)
    soft = len(warns)
    print("=" * 72)
    if hard:
        print(f"CONTAMINATION: {hard} failure(s), {soft} warning(s).")
        return 1
    if soft and args.strict:
        print(f"CLEAN, but {soft} warning(s) and --strict was set.")
        return 1
    print(f"CLEAN. {soft} warning(s), not failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
