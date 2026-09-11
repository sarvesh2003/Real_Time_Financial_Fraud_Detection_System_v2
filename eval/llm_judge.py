"""
Semantic grading companion to grade.py's mechanical phrase-matching.

grade.py scores a run by checking whether specific phrases from rubric.yaml
appear in its summary/reasoning text. That's fast and reproducible, but it
has real, observed failure modes: a bare disqualifier phrase can zero out a
correct answer that mentions the word in passing (fixed for faults 1, 2,
16, 13 - see PLAN.md), and a phrase can mechanically match while the
underlying causal claim is wrong (e.g. fault 6's Sonnet run cites "restart"
correctly while blaming an organic memory leak instead of the real,
deliberate restart-loop injection - see that run's _grading_note).

This script asks Claude directly, given the real ground-truth mechanism and
the run's own summary/reasoning/transcript, to judge semantic correctness
and confidence calibration, and to write a short qualitative insight per
run. It does not replace grade.py's score or touch results.csv/summary.md -
it writes its own eval/llm_judge_report.md and eval/llm_judge_results.csv,
as an independent second opinion.

Usage:
    agent/venv/Scripts/python.exe eval/llm_judge.py 6 9 13
    (fault numbers to judge; reads every result.json under eval/runs/faultNN/)

Requires ANTHROPIC_API_KEY in the environment or .env.
"""

import csv
import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
RUNS_DIR = EVAL_DIR / "runs"
load_dotenv(REPO_ROOT / ".env")

JUDGE_MODEL = os.environ.get("LLM_JUDGE_MODEL", "claude-sonnet-5")

# Verified directly against each fault's own injection script - not derived
# from rubric.yaml's phrase lists, so the judge isn't just re-doing keyword
# matching with extra steps.
GROUND_TRUTH = {
    6: (
        "kafka/rebalance_churn: the fault script restarts go-enricher "
        "(all 3 replicas) 6 times, 5 seconds apart, forcing Kafka to "
        "repeatedly rebalance partition assignment across the consumer "
        "group instead of settling. The correct diagnosis names go-enricher "
        "restarts/consumer-group rebalance churn as the cause. A confident "
        "claim of an organic problem (e.g. a memory leak, a crash from a "
        "bug) instead of a deliberate repeated-restart pattern is WRONG, "
        "even if it correctly notices go-enricher was restarting."
    ),
    9: (
        "network/latency_injection: the fault script adds a 200ms tc netem "
        "delay to go-server's eth0 interface. No error lines are produced "
        "anywhere - the only signal is a shift in latency/duration metrics "
        "and the resulting throughput drop. Redis and CPU are both "
        "unaffected and healthy. The correct diagnosis names go-server (or "
        "its network link) and network-level latency/delay as the cause. "
        "Blaming Redis, CPU throttling, or leaving the affected service "
        "unnamed are all wrong or incomplete."
    ),
    13: (
        "config/client_timeout_too_low: the fault script sets go-server's "
        "Redis client env vars (REDIS_DIAL_TIMEOUT_MS, REDIS_READ_TIMEOUT_MS, "
        "REDIS_WRITE_TIMEOUT_MS) to 1ms and recreates the go-server "
        "container. Redis itself is completely healthy - the failure mode "
        "is purely client-side timeouts on go-server. The correct diagnosis "
        "names go-server's Redis client timeout configuration as the cause. "
        "Blaming Redis itself, a server-side connection limit, or a retry/"
        "backoff policy misconfiguration are all wrong."
    ),
}


def load_fault_runs(fault_num: int) -> list[dict]:
    fault_dir = RUNS_DIR / f"fault{fault_num:02d}"
    if not fault_dir.exists():
        raise SystemExit(f"{fault_dir} not found")
    runs = []
    for result_path in sorted(fault_dir.glob(f"fault{fault_num}_*.result.json")):
        run = json.loads(result_path.read_text(encoding="utf-8"))
        env_label = run.get("env_label", "")
        transcript_path = None
        if env_label.startswith("claude-desktop-"):
            model_tag = env_label.removeprefix("claude-desktop-")
            candidate = fault_dir / f"claude_desktop_transcript_{model_tag}.md"
            if candidate.exists():
                transcript_path = candidate
        runs.append({"result": run, "result_path": result_path, "transcript_path": transcript_path})
    return runs


def build_prompt(fault_num: int, run: dict) -> str:
    result = run["result"]
    transcript = ""
    if run["transcript_path"]:
        transcript = run["transcript_path"].read_text(encoding="utf-8")

    return f"""Ground truth for this fault:
{GROUND_TRUTH.get(fault_num, "(no verified ground-truth description available - use the summary's own internal consistency only)")}

Run being judged:
- env_label: {result.get('env_label')}
- self-reported confidence: {result.get('model_self_reported_confidence')}
- enforced confidence (after code-level downgrade rules): {result.get('confidence')}
- status: {result.get('status')}

Its stated summary:
{result.get('summary') or '(empty)'}

Its stated reasoning:
{result.get('reasoning') or '(empty)'}

{"Full transcript:\\n" + transcript if transcript else ""}

Judge this run. Respond with ONLY a JSON object, no markdown fences, no other text:
{{
  "verdict": "correct" | "partial" | "incorrect",
  "verdict_reason": "<one sentence citing the specific evidence in the ground truth vs the run's claim>",
  "confidence_calibration": "well-calibrated" | "overconfident" | "underconfident" | "n/a",
  "calibration_reason": "<one sentence>",
  "insight": "<1-2 sentences a human reading a per-fault report would want to know - what this run got right/wrong and why, in plain language>"
}}"""


def judge_run(client: Anthropic, fault_num: int, run: dict) -> dict:
    prompt = build_prompt(fault_num, run)
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def main() -> None:
    fault_nums = [int(a) for a in sys.argv[1:]]
    if not fault_nums:
        raise SystemExit("Usage: llm_judge.py <fault_number> [fault_number ...]")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY must be set (in .env or the environment).")
    client = Anthropic(api_key=api_key)

    all_verdicts = []
    report_sections = []

    for fault_num in fault_nums:
        runs = load_fault_runs(fault_num)
        section_lines = [f"## Fault {fault_num}\n"]
        for run in runs:
            env_label = run["result"].get("env_label", "?")
            print(f"Judging fault {fault_num} / {env_label} ...")
            try:
                verdict = judge_run(client, fault_num, run)
            except Exception as e:  # noqa: BLE001
                verdict = {
                    "verdict": "error",
                    "verdict_reason": f"{type(e).__name__}: {e}",
                    "confidence_calibration": "n/a",
                    "calibration_reason": "",
                    "insight": "Judge call failed - see verdict_reason.",
                }
            all_verdicts.append({"fault_id": fault_num, "env_label": env_label, **verdict})
            section_lines.append(
                f"- **{env_label}** - verdict: `{verdict['verdict']}` "
                f"({verdict['verdict_reason']}) | calibration: `{verdict['confidence_calibration']}` "
                f"({verdict['calibration_reason']})\n  - {verdict['insight']}"
            )
        report_sections.append("\n".join(section_lines))

    report_path = EVAL_DIR / "llm_judge_report.md"
    report_path.write_text(
        "# LLM-judge semantic grading report\n\n"
        "Independent second opinion alongside grade.py's mechanical phrase-matching score - "
        f"judged by `{JUDGE_MODEL}` given each run's own summary/reasoning/transcript and a "
        "verified ground-truth description of the fault's real mechanism (see GROUND_TRUTH in "
        "llm_judge.py). Does not replace or feed into results.csv/summary.md.\n\n"
        + "\n\n".join(report_sections) + "\n",
        encoding="utf-8",
    )

    csv_path = EVAL_DIR / "llm_judge_results.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["fault_id", "env_label", "verdict", "verdict_reason", "confidence_calibration", "calibration_reason", "insight"])
        writer.writeheader()
        writer.writerows(all_verdicts)

    print(f"\nWrote {report_path}")
    print(f"Wrote {csv_path}")


if __name__ == "__main__":
    main()
