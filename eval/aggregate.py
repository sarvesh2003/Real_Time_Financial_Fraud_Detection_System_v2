"""
Computes every metric in the final eval design (personal-notes/
implementation-logs.md, eval design step) from eval/results.csv +
eval/rubric.yaml + eval/cost_matrix.yaml, and writes eval/summary.md.

Run this AFTER eval/grade.py and after resolving eval/grading_queue.csv
(if non-empty) by hand.

Usage: venv/Scripts/python.exe eval/aggregate.py
"""

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
RUNS_DIR = EVAL_DIR / "runs"


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) / n) + (z * z / (4 * n * n)))
    return (max(0.0, (center - margin) / denom), min(1.0, (center + margin) / denom))


def load_results() -> list[dict]:
    path = EVAL_DIR / "results.csv"
    if not path.exists():
        raise SystemExit(f"{path} not found - run eval/grade.py first.")
    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for key in ("cited_evidence_indices", "distinct_supporting_tools", "tool_call_sequence",
                        "distinct_tools_used_total", "vram_pre", "vram_post"):
                if row.get(key):
                    try:
                        row[key] = json.loads(row[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
            for key in ("score", "calls_used", "wall_clock_s", "llm_calls", "llm_ms_total",
                        "tool_ms_total", "input_tokens_total", "output_tokens_total",
                        "cited_baseline_fraction", "evidence_groundedness", "n_cited",
                        "duplicate_calls", "tool_errors", "hallucinated_tool_calls"):
                if row.get(key) not in (None, ""):
                    try:
                        row[key] = float(row[key])
                    except ValueError:
                        pass
            rows.append(row)
    return rows


def fmt_pct(x: float | None) -> str:
    return f"{x*100:.0f}%" if x is not None else "n/a"


def env_metrics(rows: list[dict], rubric_by_id: dict) -> dict:
    n = len(rows)
    correct = sum(1 for r in rows if r.get("score") == 2)
    confirmed_rows = [r for r in rows if r.get("confidence") == "confirmed"]
    confirmed_correct = sum(1 for r in confirmed_rows if r.get("score") == 2)
    confirmed_wrong = sum(1 for r in confirmed_rows if r.get("score") in (0, 1))
    best_guess_correct = sum(1 for r in rows if r.get("confidence") == "best_guess" and r.get("score") == 2)
    inconclusive_rows = [r for r in rows if r.get("confidence") == "inconclusive"]

    crashed_count = sum(1 for r in rows if r.get("status") != "completed")
    parse_failure_count = sum(1 for r in rows if (r.get("root_cause_id") or "").upper() == "PARSE_FAILURE")

    coverage = len(confirmed_rows) / n if n else 0
    selective_accuracy = confirmed_correct / len(confirmed_rows) if confirmed_rows else None
    false_confidence_rate = confirmed_wrong / n if n else 0
    # Denominator is EVERY correct answer regardless of confidence label
    # (including a score==2 that was labeled inconclusive) - using only
    # confirmed+best_guess correct counts would silently drop that case and
    # inflate this number.
    all_correct = sum(1 for r in rows if r.get("score") == 2)
    confirmed_recall = confirmed_correct / all_correct if all_correct else None

    # Reliability table + corroboration-gate ablation.
    reliability = {}
    for tier in ("confirmed", "best_guess", "inconclusive"):
        tier_rows = [r for r in rows if r.get("confidence") == tier]
        tier_correct = sum(1 for r in tier_rows if r.get("score") == 2)
        reliability[tier] = {"n": len(tier_rows), "correct": tier_correct,
                              "accuracy": tier_correct / len(tier_rows) if tier_rows else None}

    self_reported_confirmed = [r for r in rows if r.get("model_self_reported_confidence") == "confirmed"]
    self_reported_wrong = sum(1 for r in self_reported_confirmed if r.get("score") in (0, 1))
    gate_bind_count = sum(1 for r in rows if r.get("confidence") != r.get("model_self_reported_confidence"))

    # Efficiency.
    correct_rows = [r for r in rows if r.get("score") == 2 and r.get("calls_used") is not None]
    mean_calls_to_resolution = (sum(r["calls_used"] for r in correct_rows) / len(correct_rows)) if correct_rows else None
    mean_cited_baseline_fraction = _mean([r.get("cited_baseline_fraction") for r in rows])
    mean_groundedness = _mean([r.get("evidence_groundedness") for r in rows])
    dup_rate = _mean([r.get("duplicate_calls") for r in rows])
    tool_error_rate = _mean([r.get("tool_errors") for r in rows])
    hallucination_rate = _mean([r.get("hallucinated_tool_calls") for r in rows])

    # Scripted-baseline floor: of faults rubric-labeled baseline-sufficient,
    # how many were solved using ONLY baseline evidence (cited fraction 1.0).
    sufficient_ids = {fid for fid, entry in rubric_by_id.items() if entry.get("baseline_sufficiency") == "sufficient"}
    sufficient_rows = [r for r in rows if str(r.get("fault_id")) in sufficient_ids]
    baseline_solved = sum(1 for r in sufficient_rows if r.get("score") == 2 and (r.get("cited_baseline_fraction") or 0) >= 0.999)
    scripted_baseline_accuracy = baseline_solved / len(sufficient_rows) if sufficient_rows else None

    # Cost/timing.
    mean_wall_clock = _mean([r.get("wall_clock_s") for r in rows])
    mean_tokens_in = _mean([r.get("input_tokens_total") for r in rows])
    mean_tokens_out = _mean([r.get("output_tokens_total") for r in rows])
    mean_llm_ms = _mean([r.get("llm_ms_total") for r in rows])
    tokens_per_sec = None
    if mean_tokens_out and mean_llm_ms:
        tokens_per_sec = mean_tokens_out / (mean_llm_ms / 1000)

    return {
        "n": n, "correct": correct,
        "coverage": coverage, "coverage_ci": wilson_ci(len(confirmed_rows), n),
        "selective_accuracy": selective_accuracy,
        "selective_accuracy_ci": wilson_ci(confirmed_correct, len(confirmed_rows)) if confirmed_rows else None,
        "false_confidence_rate": false_confidence_rate, "false_confidence_n": confirmed_wrong,
        "false_confidence_ci": wilson_ci(confirmed_wrong, n),
        "confirmed_recall": confirmed_recall,
        "raw_accuracy": correct / n if n else 0, "raw_accuracy_ci": wilson_ci(correct, n),
        "inconclusive_count": len(inconclusive_rows),
        "crashed_count": crashed_count, "parse_failure_count": parse_failure_count,
        "reliability": reliability,
        "gate_bind_count": gate_bind_count,
        "self_reported_false_confidence_rate": self_reported_wrong / n if n else 0,
        "mean_calls_to_resolution": mean_calls_to_resolution,
        "mean_cited_baseline_fraction": mean_cited_baseline_fraction,
        "mean_groundedness": mean_groundedness,
        "dup_rate": dup_rate, "tool_error_rate": tool_error_rate, "hallucination_rate": hallucination_rate,
        "scripted_baseline_accuracy": scripted_baseline_accuracy,
        "mean_wall_clock_s": mean_wall_clock,
        "mean_tokens_in": mean_tokens_in, "mean_tokens_out": mean_tokens_out,
        "tokens_per_sec": tokens_per_sec,
    }


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None and v != ""]
    return sum(vals) / len(vals) if vals else None


def contrast_pair_accuracy(rows: list[dict], pairs: list[list[int]]) -> dict:
    by_fault = {str(r["fault_id"]): r for r in rows}
    passed, total = 0, 0
    detail = []
    for a, b in pairs:
        ra, rb = by_fault.get(str(a)), by_fault.get(str(b))
        if ra is None or rb is None:
            continue
        total += 1
        ok = ra.get("score") == 2 and rb.get("score") == 2
        passed += int(ok)
        detail.append((a, b, ok, ra.get("score"), rb.get("score")))
    return {"passed": passed, "total": total, "rate": passed / total if total else None, "detail": detail}


def cost_weighted_score(rows: list[dict], cost_cells: dict, confirmed_wrong_override: float | None = None) -> float | None:
    cw = confirmed_wrong_override if confirmed_wrong_override is not None else cost_cells["confirmed_wrong"]
    # A crashed run produces no answer AND no evidence artifact at all -
    # strictly worse than an honest "inconclusive" (which at least ran the
    # baseline sweep and told the engineer that). Pricing it the same as
    # inconclusive (the bug this replaces) made a crash look free.
    crash_cost = cost_cells["inconclusive"] * 1.5
    total_cost, always_abstain_cost = 0, 0
    for r in rows:
        conf, score = r.get("confidence"), r.get("score")
        if r.get("status") != "completed":
            c = crash_cost
        elif conf == "confirmed" and score == 2:
            c = cost_cells["confirmed_correct"]
        elif conf == "best_guess" and score == 2:
            c = cost_cells["best_guess_correct"]
        elif conf == "inconclusive":
            c = cost_cells["inconclusive"]
        elif conf == "best_guess":
            c = cost_cells["best_guess_wrong"]
        elif conf == "confirmed":
            c = cw
        else:
            c = cost_cells["inconclusive"]
        total_cost += c
        always_abstain_cost += cost_cells["inconclusive"]
    if not rows or always_abstain_cost == 0:
        return None
    return 1 - (total_cost / always_abstain_cost)


def retrieval_metrics(rows: list[dict], rubric_by_id: dict) -> dict:
    """
    Broken down by split, NOT pooled - the three splits have structurally
    different expected behavior (indexed = known-item search, should be
    ~1.0, that's leakage not generalization; held_out_no_neighbor = no
    relevant doc exists at all, correct behavior is retrieving NOTHING
    above the distance cutoff, so a "hit" there is actually a false
    positive). Pooling them into one number, like the code this replaced
    did, hides exactly the thing this metric exists to show.
    """
    by_split = defaultdict(lambda: {"hits_at_1": 0, "mrr_scores": [], "checked": 0, "false_positive_hits": 0})
    for r in rows:
        run_id = r.get("run_id")
        fault_id = str(r.get("fault_id", ""))
        split = rubric_by_id.get(fault_id, {}).get("split", "unknown")
        # Exact "fault" + 2 digits only - see the matching note in grade.py.
        trace_candidates = list(RUNS_DIR.glob(f"fault[0-9][0-9]/{run_id}.trace.jsonl"))
        if not trace_candidates:
            continue
        try:
            lines = trace_candidates[0].read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        first_retrieval = None
        for line in lines:
            evt = json.loads(line)
            if evt.get("event") == "action_selected" and evt.get("retrieved"):
                first_retrieval = evt["retrieved"]
                break
        bucket = by_split[split]
        bucket["checked"] += 1
        if first_retrieval is None:
            continue  # nothing above the distance cutoff - correct behavior for held_out_no_neighbor
        true_family = r.get("fault_family", "")
        rank = next((i + 1 for i, c in enumerate(first_retrieval) if c.get("fault_family") == true_family), None)
        if split == "held_out_no_neighbor":
            bucket["false_positive_hits"] += 1 if rank else 0
            continue
        if rank == 1:
            bucket["hits_at_1"] += 1
        bucket["mrr_scores"].append(1 / rank if rank else 0)

    out = {}
    for split, b in by_split.items():
        out[split] = {
            "checked": b["checked"],
            "hit_at_1": b["hits_at_1"] / b["checked"] if b["checked"] else None,
            "mrr": sum(b["mrr_scores"]) / len(b["mrr_scores"]) if b["mrr_scores"] else None,
            "false_positive_hits": b.get("false_positive_hits", 0),
        }
    return out


def main() -> None:
    rows = load_results()
    rubric = yaml.safe_load((EVAL_DIR / "rubric.yaml").read_text(encoding="utf-8"))
    rubric_by_id = {str(f["fault_id"]): f for f in rubric["faults"]}
    cost_matrix = yaml.safe_load((EVAL_DIR / "cost_matrix.yaml").read_text(encoding="utf-8"))

    by_env = defaultdict(list)
    for r in rows:
        env_key = r.get("env_label", "unknown")
        rag_key = r.get("retrieval_mode", "on")  # claude-desktop rows have no retrieval_mode - default "on" groups them alone, fine
        is_claude_desktop = env_key.startswith("claude-desktop")
        by_env[env_key if is_claude_desktop else f"{env_key} (rag={rag_key})"].append(r)

    lines = ["# Eval summary\n", f"Total graded runs: {len(rows)}\n"]

    for env, env_rows in sorted(by_env.items()):
        m = env_metrics(env_rows, rubric_by_id)
        cw_score = cost_weighted_score(env_rows, cost_matrix["cost_cells"])
        sweep = {cw: cost_weighted_score(env_rows, cost_matrix["cost_cells"], cw)
                 for cw in cost_matrix["sensitivity_sweep_confirmed_wrong"]}
        pairs = contrast_pair_accuracy(env_rows, rubric["contrast_pairs"])
        retrieval_by_split = retrieval_metrics(env_rows, rubric_by_id)

        lines.append(f"\n## {env}  (n={m['n']})\n")
        lines.append(f"- Raw accuracy: {m['correct']}/{m['n']} ({fmt_pct(m['raw_accuracy'])}, "
                      f"Wilson 95% CI {fmt_pct(m['raw_accuracy_ci'][0])}-{fmt_pct(m['raw_accuracy_ci'][1])})")
        lines.append(f"- Coverage (confirmed): {fmt_pct(m['coverage'])} "
                      f"(CI {fmt_pct(m['coverage_ci'][0])}-{fmt_pct(m['coverage_ci'][1])})")
        lines.append(f"- Selective accuracy (of confirmed, % actually right): {fmt_pct(m['selective_accuracy'])}")
        lines.append(f"- **False-confidence rate: {m['false_confidence_n']}/{m['n']} "
                      f"({fmt_pct(m['false_confidence_rate'])}, CI {fmt_pct(m['false_confidence_ci'][0])}-{fmt_pct(m['false_confidence_ci'][1])})**")
        lines.append(f"- Confirmed-recall (of correct answers, % flagged confident): {fmt_pct(m['confirmed_recall'])}")
        lines.append(f"- Inconclusive (honest abstain) count: {m['inconclusive_count']} "
                      f"(of which {m['parse_failure_count']} were parse failures, not a real abstention decision)")
        lines.append(f"- Crashed runs (n={m['n']} includes these; priced worse than inconclusive in the cost score below): {m['crashed_count']}")
        lines.append("")
        lines.append("Reliability table:")
        for tier, d in m["reliability"].items():
            lines.append(f"  - {tier}: n={d['n']}, correct={d['correct']}, accuracy={fmt_pct(d['accuracy'])}")
        lines.append(f"- Corroboration-gate ablation: gate fired on {m['gate_bind_count']}/{m['n']} runs; "
                      f"false-confidence rate WITHOUT the gate (raw self-reported) = {fmt_pct(m['self_reported_false_confidence_rate'])} "
                      f"vs WITH the gate = {fmt_pct(m['false_confidence_rate'])}")
        lines.append("")
        lines.append(f"- Contrast-pair accuracy (strict, both must be correct): {pairs['passed']}/{pairs['total']} "
                      f"({fmt_pct(pairs['rate'])})")
        for a, b, ok, sa, sb in pairs["detail"]:
            lines.append(f"    - fault {a} vs {b}: {'PASS' if ok else 'FAIL'} (scores {sa}/{sb})")
        lines.append("")
        lines.append("- Retrieval, by split (NOT pooled - each split has a different correct behavior):")
        for split, rd in sorted(retrieval_by_split.items()):
            if split == "held_out_no_neighbor":
                lines.append(f"    - {split} (n={rd['checked']}): correct behavior is retrieving NOTHING relevant - "
                              f"false-positive hits: {rd['false_positive_hits']}/{rd['checked']}")
            else:
                note = " (expected ~100% - known-item search, this is leakage not generalization)" if split == "indexed" else ""
                lines.append(f"    - {split} (n={rd['checked']}): hit@1={fmt_pct(rd['hit_at_1'])}, "
                              f"MRR={rd['mrr']:.2f}{note}" if rd['mrr'] is not None else f"    - {split}: n/a")
        lines.append("")
        lines.append(f"- Cost-weighted skill score (confirmed-wrong=10x): {cw_score:.2f}" if cw_score is not None else "- Cost-weighted skill score: n/a")
        lines.append(f"  - Sensitivity sweep: " + ", ".join(f"{k}x={v:.2f}" for k, v in sweep.items() if v is not None))
        lines.append(f"- Scripted-baseline-only accuracy (on baseline-sufficient faults): {fmt_pct(m['scripted_baseline_accuracy'])}")
        lines.append("")
        lines.append(f"- Mean calls-to-resolution (correct answers only): {m['mean_calls_to_resolution']:.1f}" if m['mean_calls_to_resolution'] is not None else "- Mean calls-to-resolution: n/a")
        lines.append(f"- Mean fraction of cited evidence from the free baseline sweep: {fmt_pct(m['mean_cited_baseline_fraction'])}")
        lines.append(f"- Mean evidence groundedness: {fmt_pct(m['mean_groundedness'])}")
        lines.append(f"- Duplicate/tool-error/hallucination rate per run: {m['dup_rate']}/{m['tool_error_rate']}/{m['hallucination_rate']}")
        lines.append("")
        lines.append(f"- Mean wall-clock per investigation: {m['mean_wall_clock_s']:.1f}s" if m['mean_wall_clock_s'] is not None else "- Mean wall-clock: n/a")
        lines.append(f"- Mean tokens in/out: {m['mean_tokens_in']}/{m['mean_tokens_out']}")
        lines.append(f"- Tokens/sec: {m['tokens_per_sec']:.1f}" if m['tokens_per_sec'] is not None else "- Tokens/sec: n/a")

    out_path = EVAL_DIR / "summary.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_path}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
