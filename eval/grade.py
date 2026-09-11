"""
Mechanical grading: reads every result.json under eval/runs/, scores it
against the pre-registered eval/rubric.yaml, and writes eval/results.csv.

Scoring (mirrors rubric.yaml's header comment):
    2  = mechanism (+ mechanism_and, if present) + target all hit, no disqualifier
    1  = partial - only mechanism OR only target hit, or a partial_credit phrase hit
    0  = a disqualifier fired, or nothing relevant hit at all
   -1  = inconclusive with no root cause claimed (tracked separately from 0 -
         this is an honest abstention, not a wrong answer, and must NOT be
         counted in the false-confidence rate)

Ambiguous rows (contradictory signal, non-completed status, a PARSE_FAILURE,
or an unfilled Claude Desktop template) are written to eval/grading_queue.csv
for a human to resolve, WITHOUT the fault identity or the rubric's answer
phrases visible (blind grading - see _blind_row). To apply a resolution,
add a line to eval/score_overrides.csv (run_id,score,note) and re-run this
script - overrides are applied after auto-scoring and are the only way a
reviewed row's score changes; results.csv is always rebuilt from scratch,
so this is the one place a human correction actually persists.

Usage: venv/Scripts/python.exe eval/grade.py
"""

import csv
import json
import re
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
RUNS_DIR = EVAL_DIR / "runs"


def _any_hit(text: str, phrases: list[str]) -> str | None:
    for p in phrases:
        if re.search(re.escape(p), text, re.IGNORECASE):
            return p
    return None


def score_run(result: dict, rubric_entry: dict) -> dict:
    text = f"{result.get('summary', '')} {result.get('reasoning', '')}"
    root_cause_id = (result.get("root_cause_id") or "").lower()

    not_completed = result.get("status") != "completed"
    parse_failure = root_cause_id == "parse_failure"
    unfilled_manual_template = (
        result.get("status") == "completed"
        and str(result.get("env_label", "")).startswith("claude-desktop")
        and not text.strip()
    )
    no_answer = (
        result.get("confidence") == "inconclusive"
        and (not root_cause_id or root_cause_id in ("none", "parse_failure"))
    )

    disqualifier_hit = None
    for dq in rubric_entry.get("disqualifiers", []):
        hit = _any_hit(text, dq["any_of"])
        if hit:
            disqualifier_hit = dq["id"]
            break

    mechanism_hit = _any_hit(text, rubric_entry.get("mechanism_any_of", []))
    mechanism_and_ok = True
    if rubric_entry.get("mechanism_and_any_of"):
        mechanism_and_ok = bool(_any_hit(text, rubric_entry["mechanism_and_any_of"]))
    target_hit = _any_hit(text, rubric_entry.get("target_any_of", []))
    partial_hit = _any_hit(text, rubric_entry.get("partial_credit_any_of", []))

    needs_review = False
    if not_completed or parse_failure or unfilled_manual_template:
        # Nothing gradable here without a human decision - never silently
        # score a crash, a parse failure, or a forgotten manual entry.
        score = -1 if parse_failure else 0
        needs_review = True
    elif no_answer:
        score = -1
    elif disqualifier_hit and (mechanism_hit or target_hit):
        # ANY overlap between a disqualifying phrase and a real answer
        # signal is contradictory enough to need a human look - not just
        # the case where every positive signal also hit, which missed a
        # partially-correct answer that triggers a disqualifier on an
        # incidental mention (e.g. naming healthy peers for comparison).
        score = 0
        needs_review = True
    elif disqualifier_hit:
        score = 0
    elif mechanism_hit and mechanism_and_ok and target_hit:
        score = 2
    elif partial_hit or mechanism_hit or target_hit:
        score = 1
    else:
        score = 0

    return {
        "score": score,
        "mechanism_hit": mechanism_hit or "",
        "target_hit": target_hit or "",
        "disqualifier_hit": disqualifier_hit or "",
        "needs_review": needs_review,
    }


def _load_overrides() -> dict[str, dict]:
    path = EVAL_DIR / "score_overrides.csv"
    if not path.exists():
        return {}
    overrides = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            overrides[row["run_id"]] = {"score": int(row["score"]), "note": row.get("note", "")}
    return overrides


def main() -> None:
    rubric = yaml.safe_load((EVAL_DIR / "rubric.yaml").read_text(encoding="utf-8"))
    rubric_by_id = {str(f["fault_id"]): f for f in rubric["faults"]}
    overrides = _load_overrides()

    rows = []
    review_rows = []
    # Exact "fault" + 2 digits only - NOT "fault*", which would also match
    # sibling archive folders like "fault02_contaminated_run1" left behind
    # by a redo (see eval/runs/ for examples) and silently double/triple-count
    # that fault's rows.
    result_files = sorted(RUNS_DIR.glob("fault[0-9][0-9]/*.result.json"))
    if not result_files:
        print(f"No result.json files found under {RUNS_DIR} - nothing to grade yet.")
        return

    for path in result_files:
        result = json.loads(path.read_text(encoding="utf-8"))
        fault_id = str(result.get("fault_id", ""))
        rubric_entry = rubric_by_id.get(fault_id)
        if rubric_entry is None:
            print(f"WARNING: {path} has no rubric entry for fault_id={fault_id!r} - skipping")
            continue

        grading = score_run(result, rubric_entry)
        run_id = result.get("run_id")
        human_resolved = False
        if run_id in overrides:
            grading["score"] = overrides[run_id]["score"]
            grading["needs_review"] = False
            human_resolved = True

        row = {**result, **grading, "human_resolved": human_resolved}
        rows.append(row)

        if grading["needs_review"]:
            # Blind: no fault identity, no rubric answer phrases - only the
            # disqualifier's ID (a short label, not the phrase list) and the
            # model's own text, so a reviewer can judge the answer on its
            # merits without the fault name anchoring their judgment.
            review_rows.append({
                "review_key": f"row{len(review_rows) + 1}",
                "summary": result.get("summary", ""),
                "reasoning": result.get("reasoning", ""),
                "status": result.get("status"),
                "disqualifier_id_hit": row.get("disqualifier_hit", ""),
                "mechanism_hit": row.get("mechanism_hit", ""),
                "target_hit": row.get("target_hit", ""),
                "auto_score": row["score"],
                "_run_id_for_override_file": run_id,
            })

    if not rows:
        print("No gradable rows produced - check that result.json files have a matching fault_id.")
        return

    fieldnames = sorted({k for r in rows for k in r.keys()})
    out_csv = EVAL_DIR / "results.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in r.items()})
    print(f"Wrote {len(rows)} graded rows to {out_csv} ({sum(r['human_resolved'] for r in rows)} human-resolved via overrides)")

    if review_rows:
        queue_csv = EVAL_DIR / "grading_queue.csv"
        with open(queue_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(review_rows[0].keys()))
            writer.writeheader()
            for r in review_rows:
                writer.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in r.items()})
        print(f"{len(review_rows)} rows need manual review - see {queue_csv}")
        print("To resolve: add a row to eval/score_overrides.csv with the run_id from "
              "'_run_id_for_override_file' + your score (0/1/2/-1) + a note, then re-run this script.")
    else:
        print("No rows need manual review.")


if __name__ == "__main__":
    main()
