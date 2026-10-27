"""Recompute V2 diagnostic results from run manifests and human labels.

No label is inferred from the agent's confidence or from an LLM judge.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "experiments" / "runs" / "v2"
LABELS = ROOT / "experiments" / "human_labels.csv"
VALID_LABELS = {"correct", "wrong", "abstain"}
EVIDENCE_LABELS = {"supported", "unsupported", "inconclusive"}


def wilson(successes: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.96
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [round(center - margin, 4), round(center + margin, 4)]


def summarize(manifests: list[dict], labels: dict[str, dict[str, str]]) -> dict:
    ids = [m["run_id"] for m in manifests]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate run ID in manifests")
    if set(labels) - set(ids):
        raise ValueError("human labels include an unknown run ID")
    machine_valid = [m for m in manifests if m.get("status") == "completed" and not m.get("validity_errors")]
    missing = [m["run_id"] for m in machine_valid if m["run_id"] not in labels]
    if missing:
        raise ValueError(f"completed runs lack human labels: {missing}")
    for run_id, review in labels.items():
        if not review.get("reviewer"):
            raise ValueError(f"human review for {run_id} lacks reviewer")
        if review.get("run_validity") not in {"valid", "invalid"}:
            raise ValueError(f"human review for {run_id} needs valid or invalid run_validity")
        if review["run_validity"] == "invalid" and not review.get("validity_reason"):
            raise ValueError(f"invalid run {run_id} lacks validity_reason")
        if review["run_validity"] == "valid" and (
            review.get("diagnosis_label") not in VALID_LABELS or
            review.get("evidence_label") not in EVIDENCE_LABELS
        ):
            raise ValueError(f"valid run {run_id} needs diagnosis and evidence labels")
    valid = [m for m in machine_valid if labels[m["run_id"]]["run_validity"] == "valid"]
    invalid_reasons = Counter()
    for manifest in manifests:
        if manifest not in machine_valid:
            invalid_reasons.update(manifest.get("validity_errors", []) or [manifest.get("status", "unknown")])
        elif manifest not in valid:
            invalid_reasons.update(["human review: " + labels[manifest["run_id"]]["validity_reason"]])
    by_condition = defaultdict(lambda: Counter())
    for manifest in valid:
        label = labels[manifest["run_id"]]["diagnosis_label"]
        key = (manifest["case"], manifest["retrieval_mode"])
        by_condition[key][label] += 1
    faults = [m for m in valid if m["case"] != "healthy"]
    controls = [m for m in valid if m["case"] == "healthy"]
    correct = sum(labels[m["run_id"]]["diagnosis_label"] == "correct" for m in faults)
    false_alarms = sum(labels[m["run_id"]]["diagnosis_label"] == "wrong" for m in controls)
    evidence_counts = Counter(labels[m["run_id"]]["evidence_label"] for m in valid)
    return {
        "schema_version": "2.0",
        "attempted": len(manifests), "valid": len(valid), "invalid": len(manifests) - len(valid),
        "invalid_reasons": dict(sorted(invalid_reasons.items())),
        "fault_runs": len(faults), "fault_correct": correct,
        "fault_wrong": sum(labels[m["run_id"]]["diagnosis_label"] == "wrong" for m in faults),
        "fault_abstain": sum(labels[m["run_id"]]["diagnosis_label"] == "abstain" for m in faults),
        "fault_accuracy": round(correct / len(faults), 4) if faults else None,
        "fault_accuracy_wilson95": wilson(correct, len(faults)),
        "healthy_controls": len(controls), "healthy_false_alarms": false_alarms,
        "healthy_false_alarm_rate": round(false_alarms / len(controls), 4) if controls else None,
        "healthy_false_alarm_wilson95": wilson(false_alarms, len(controls)),
        "evidence_support_counts": dict(sorted(evidence_counts.items())),
        "by_case_and_retrieval": [
            {"case": case, "retrieval_mode": retrieval, "counts": dict(sorted(counts.items()))}
            for (case, retrieval), counts in sorted(by_condition.items())
        ],
    }


def load_labels(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    labels = {}
    for row in rows:
        run_id = row["run_id"].strip()
        if not run_id:
            continue
        if run_id in labels:
            raise ValueError(f"duplicate human label for {run_id}")
        labels[run_id] = {field: (value or "").strip() for field, value in row.items() if field != "run_id"}
    return labels


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=RUNS)
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifests = [json.loads(path.read_text(encoding="utf-8"))
                 for path in sorted(args.runs.glob("*/manifest.json"))]
    result = summarize(manifests, load_labels(args.labels))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
