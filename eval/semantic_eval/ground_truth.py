"""
Ground truth for the semantic judge: the injection script, verbatim.

WHY THE SCRIPT AND NOT THE RUBRIC
---------------------------------
`eval/rubric.yaml` holds phrase lists - `mechanism_any_of`, `target_any_of`,
`disqualifiers` - and `eval/grade.py` scores a run by substring-matching them
against its summary. Those lists are a grader, and a grader is not ground
truth: a run can match every phrase while making the wrong causal claim, and
can make the right claim in words nobody listed. The observed failures are
documented in `eval/llm_judge.py`'s module docstring.

What was actually done to the system is in `fault-injection/<family>/<n>.sh`.
That file is the answer key. This module carries it through verbatim, so there
is no human paraphrase sitting between the executed fault and the judge - and
so that editing a script automatically corrects the answer key.

The rubric is still read, for three things only:

    fault_id -> family
    fault_id -> script path
    fault_id -> liveness / baseline_sufficiency

which the plan explicitly permits ("rubric.yaml may locate the script"). The
phrase lists are never read, and `LEGACY_PHRASE_KEYS` exists so a test can
assert they never appear in anything handed to a judge.

THE SCRIPT PATH IS CROSS-CHECKED
--------------------------------
Two files claim to know which script answers a fault: the rubric, and
`eval/run_fault_eval.sh`'s `FAULTS` table, which is what actually executed.
`rubric_script_map()` and `driver_script_map()` expose both so a test can
require they agree. If they ever diverge, the answer key is describing a
different experiment from the one that ran, and that must not be discovered
by reading a judge's output.

LIVENESS IS PART OF THE TRUTH
-----------------------------
Fault 4 blocks a Redis node with `DEBUG SLEEP` for 10 seconds; the runs that
were graded against it lasted several minutes. A judge told only "a Redis node
was blocked" would read "I found nothing wrong" as a plain miss, when the
stimulus had in fact already self-resolved. So `liveness` travels with the
script, and `eval/validity/`'s `stimulus_live_during_observation` check - which
would have measured this properly - is reported as unverified by the gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

EVAL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVAL_DIR.parent
RUBRIC_YAML = EVAL_DIR / "rubric.yaml"
DRIVER_SH = EVAL_DIR / "run_fault_eval.sh"
SCRIPTS_DIR = REPO_ROOT / "fault-injection"

# Rubric keys that constitute the legacy substring grader. Named here so tests
# can assert none of them reaches a judge payload. Never read by this module.
LEGACY_PHRASE_KEYS = (
    "mechanism_any_of",
    "mechanism_and_any_of",
    "target_any_of",
    "partial_credit_any_of",
    "disqualifiers",
    "look_alike_of",
)

# Metadata this module is allowed to take from the rubric.
_METADATA_KEYS = ("family", "script", "liveness", "baseline_sufficiency", "split")

# `VAR="${VAR:-default}"`, the shell idiom every injection script uses to
# declare its own defaults. The eval driver invokes `bash <script> inject`
# with no overrides, so these defaults are what ran.
_DEFAULT_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]*)="\$\{\1:-([^}]*)\}"', re.MULTILINE)

# `[4]="redis/04_slow_commands.sh|redis|slow_commands|transient"`
_DRIVER_RE = re.compile(r'^\s*\[(\d+)\]="([^|"]+)\|', re.MULTILINE)


class GroundTruthError(RuntimeError):
    """
    Ground truth could not be established.

    Always raised rather than returning an empty answer key: a judge handed a
    blank ground truth would mark every diagnosis wrong, and that reads as a
    model failure rather than a missing file.
    """


@dataclass(frozen=True)
class FaultGroundTruth:
    """
    What was injected, as the script says it.

    Carries no accept phrases and no verdict guidance. Describing the fault is
    this object's whole job; deciding whether an answer matches is the judge's.
    """

    fault_id: str
    family: str
    fault_name: str
    script_relpath: str
    script_path: Path
    script_source: str
    liveness: str | None
    baseline_sufficiency: str | None
    injection_defaults: Mapping[str, str]

    def as_judge_text(self) -> str:
        """The ground-truth block as it appears in a judge prompt."""
        lines = [
            f"Injection script: {self.script_relpath}",
            f"Fault family: {self.family}",
        ]
        if self.liveness:
            lines.append(
                f"Stimulus liveness: {self.liveness}"
                + (
                    " (the fault self-resolves within seconds of injection, so it may "
                    "no longer have been present for most of the observation window)"
                    if self.liveness == "transient"
                    else " (the fault stays present until explicitly restored)"
                )
            )
        if self.injection_defaults:
            params = ", ".join(f"{k}={v}" for k, v in sorted(self.injection_defaults.items()))
            lines.append(
                f"Parameters the script defaulted to when it was invoked: {params}"
            )
        lines.append("")
        lines.append("Script source, verbatim:")
        lines.append("```bash")
        lines.append(self.script_source.rstrip())
        lines.append("```")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "fault_id": self.fault_id,
            "family": self.family,
            "fault_name": self.fault_name,
            "script_relpath": self.script_relpath,
            "liveness": self.liveness,
            "baseline_sufficiency": self.baseline_sufficiency,
            "injection_defaults": dict(self.injection_defaults),
        }


# ---------------------------------------------------------------------------
# Locating the script
# ---------------------------------------------------------------------------


def _rubric_entries(rubric_path: Path = RUBRIC_YAML) -> dict[str, dict]:
    if not rubric_path.exists():
        raise GroundTruthError(f"{rubric_path} not found")
    doc = yaml.safe_load(rubric_path.read_text(encoding="utf-8")) or {}
    out: dict[str, dict] = {}
    for fault in doc.get("faults", []):
        fault_id = str(fault.get("fault_id"))
        # Only the metadata keys are copied out, so no phrase list can reach a
        # caller of this module even by accident.
        out[fault_id] = {k: fault.get(k) for k in _METADATA_KEYS}
    return out


def rubric_script_map(rubric_path: Path = RUBRIC_YAML) -> dict[str, str]:
    """fault_id -> script relpath, as the rubric declares it."""
    return {
        fault_id: entry["script"]
        for fault_id, entry in _rubric_entries(rubric_path).items()
        if entry.get("script")
    }


def driver_script_map(driver_path: Path = DRIVER_SH) -> dict[str, str]:
    """
    fault_id -> script relpath, as the harness that ran the experiments
    declares it. The second opinion on the rubric's mapping.
    """
    if not driver_path.exists():
        return {}
    src = driver_path.read_text(encoding="utf-8")
    start = src.find("FAULTS=(")
    if start == -1:
        return {}
    end = src.find(")", start)
    return {m.group(1): m.group(2) for m in _DRIVER_RE.finditer(src[start:end])}


def _fault_name_from_script(relpath: str) -> str:
    """`config/13_client_timeout_too_low.sh` -> `client_timeout_too_low`."""
    stem = Path(relpath).stem
    return re.sub(r"^\d+_", "", stem)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_ground_truth(
    fault_id: str | int,
    rubric_path: Path = RUBRIC_YAML,
    scripts_dir: Path = SCRIPTS_DIR,
) -> FaultGroundTruth:
    """Read one fault's answer key straight off its injection script."""
    fault_id = str(fault_id)
    entries = _rubric_entries(rubric_path)
    entry = entries.get(fault_id)
    if entry is None:
        raise GroundTruthError(
            f"no rubric entry for fault_id {fault_id!r}; known ids: "
            f"{', '.join(sorted(entries, key=lambda x: int(x) if x.isdigit() else 0))}"
        )

    relpath = entry.get("script")
    if not relpath:
        raise GroundTruthError(
            f"fault {fault_id} has no 'script' field in {rubric_path.name}, so the "
            "injected fault cannot be located; ground truth is not inferable from the "
            "phrase lists"
        )

    script_path = scripts_dir / relpath
    if not script_path.is_file():
        raise GroundTruthError(
            f"fault {fault_id}: injection script {relpath} does not exist under "
            f"{scripts_dir}"
        )
    source = script_path.read_text(encoding="utf-8")
    if not source.strip():
        raise GroundTruthError(f"fault {fault_id}: injection script {relpath} is empty")

    return FaultGroundTruth(
        fault_id=fault_id,
        family=str(entry.get("family") or ""),
        fault_name=_fault_name_from_script(relpath),
        script_relpath=relpath,
        script_path=script_path,
        script_source=source,
        liveness=entry.get("liveness"),
        baseline_sufficiency=entry.get("baseline_sufficiency"),
        injection_defaults=dict(_DEFAULT_RE.findall(source)),
    )


def load_all_ground_truth(
    rubric_path: Path = RUBRIC_YAML, scripts_dir: Path = SCRIPTS_DIR
) -> dict[str, FaultGroundTruth]:
    """Every fault the rubric locates a script for."""
    return {
        fault_id: load_ground_truth(fault_id, rubric_path, scripts_dir)
        for fault_id in rubric_script_map(rubric_path)
    }
