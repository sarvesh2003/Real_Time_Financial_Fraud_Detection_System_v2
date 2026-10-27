"""Exercise the four reversible pilot faults without calling a model.

Checks injection, a five-second sustained state, target recovery and that
enriched output resumes. It does not score diagnoses. Run on a fresh
isolated stack after --init-stack and start-traffic; it uses up the
clean-stack marker, so recreate the stack before a scored run.

The saved experiments/fault_validation.json is historical evidence and is
never overwritten; results go to a new file under experiments/runs/.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from experiments.runner import (
    CASES, RECOVERY_SECONDS, ROOT, consume_marker, fault_is_live, restore_and_wait, script_action,
    stack_health, utc_now, verify_stack_marker, wait_for_growth,
)
from experiments.stack import project

NAMES = ("redis_node_stopped", "ingestion_paused", "broker_stopped", "enricher_paused")
HISTORICAL_RESULT = ROOT / "experiments" / "fault_validation.json"


def validate(name: str, log_dir: Path) -> dict:
    case = CASES[name]
    case_dir = log_dir / name
    case_dir.mkdir(parents=True, exist_ok=True)
    entry = {"case": name, "started_at_utc": utc_now(), "status": "invalid", "errors": []}
    try:
        result = script_action(case, "inject", case_dir)
        if result.returncode != 0:
            raise RuntimeError(f"inject exited {result.returncode}: {result.stderr[-500:]}")
        live, state = fault_is_live(case)
        entry["live_probe"] = {"at_utc": utc_now(), "live": live, "state": state}
        if not live:
            raise RuntimeError("fault did not reach its expected active state")
        time.sleep(5)
        still_live, state = fault_is_live(case)
        entry["sustained_probe"] = {"at_utc": utc_now(), "live": still_live, "state": state}
        if not still_live:
            raise RuntimeError("fault was not sustained for five seconds")
    except Exception as error:
        entry["errors"].append(str(error))
    finally:
        try:
            errors, entry["recovery_probe"] = restore_and_wait(case, case_dir)
            entry["errors"].extend(errors)
            entry["processing_probe"] = wait_for_growth("enriched_transactions", RECOVERY_SECONDS)
            if not entry["processing_probe"]["grew"]:
                entry["errors"].append("enriched output did not resume")
        except Exception as error:
            entry["errors"].append(f"recovery: {error}")
        entry["finished_at_utc"] = utc_now()
    if not entry["errors"]:
        entry["status"] = "passed"
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, help="result file (default: a new file under experiments/runs/fault-validation/)")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or ROOT / "experiments" / "runs" / "fault-validation" / f"{stamp}.json"
    if output.resolve() == HISTORICAL_RESULT.resolve():
        parser.error("refusing to overwrite the historical experiments/fault_validation.json")

    healthy, states = stack_health()
    marker_valid, marker_error = verify_stack_marker()
    if not healthy or not marker_valid:
        raise RuntimeError(f"isolated stack is not ready: healthy={healthy}, marker={marker_error}, states={states}")
    consume_marker(f"fault-validation-{stamp}")

    log_dir = output.parent / f"{output.stem}_logs"
    results = []
    for name in NAMES:
        results.append(validate(name, log_dir))
        if results[-1]["status"] != "passed":
            break
    passed = len(results) == len(NAMES) and all(r["status"] == "passed" for r in results)
    summary = {"schema_version": "2.1", "purpose": "fault mechanics and processing recovery, no model diagnosis",
               "compose_project": project(), "created_at_utc": utc_now(), "cases": results,
               "status": "passed" if passed else "failed"}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": summary["status"], "cases": {r["case"]: r["status"] for r in results},
                      "result": str(output)}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
