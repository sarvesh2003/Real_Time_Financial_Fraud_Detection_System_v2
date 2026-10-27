"""Exercise the four reversible pilot faults without calling a model.

This checks injection and recovery mechanics only. It does not score diagnoses.
Run on a dedicated isolated stack after --init-stack and start-traffic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from experiments.runner import (
    CASES, ROOT, fault_is_live, fault_is_restored, script_action,
    stack_health, utc_now, verify_stack_marker,
)

RESULT = ROOT / "experiments" / "fault_validation.json"
NAMES = ("redis_node_stopped", "ingestion_paused", "broker_stopped", "enricher_paused")


def main() -> int:
    healthy, states = stack_health()
    marker_valid, marker_error = verify_stack_marker()
    if not healthy or not marker_valid:
        raise RuntimeError(f"isolated stack is not ready: healthy={healthy}, marker={marker_error}, states={states}")

    log_dir = ROOT / "experiments" / "fault_validation_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for name in NAMES:
        case = CASES[name]
        case_dir = log_dir / name
        case_dir.mkdir(parents=True, exist_ok=True)
        entry = {"case": name, "started_at_utc": utc_now(), "status": "invalid"}
        injected = False
        try:
            injected = True
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
            entry["error"] = str(error)
        finally:
            if injected:
                try:
                    restored = script_action(case, "restore", case_dir)
                    if restored.returncode != 0:
                        raise RuntimeError(f"restore exited {restored.returncode}: {restored.stderr[-500:]}")
                    for _ in range(120):
                        recovered, state = fault_is_restored(case)
                        if recovered:
                            break
                        time.sleep(1)
                    entry["recovery_probe"] = {"at_utc": utc_now(), "recovered": recovered, "state": state}
                    if not recovered:
                        raise RuntimeError("service did not recover within 120 seconds")
                    if not entry.get("error"):
                        entry["status"] = "passed"
                except Exception as error:
                    entry["error"] = f"{entry.get('error', '')}; recovery: {error}".strip("; ")
            entry["finished_at_utc"] = utc_now()
            results.append(entry)
        if entry["status"] != "passed":
            break

    summary = {"schema_version": "2.0", "purpose": "fault mechanics, no model diagnosis",
               "created_at_utc": utc_now(), "cases": results,
               "status": "passed" if len(results) == len(NAMES) and all(r["status"] == "passed" for r in results)
               else "failed"}
    RESULT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": summary["status"],
                      "cases": {r["case"]: r["status"] for r in results},
                      "result": str(RESULT)}))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
