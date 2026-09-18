"""
Phase 3: the semantic diagnosis judge. One question, one label.

    Did the agent identify the actual injected root cause?

WHAT THIS JUDGE SEES, AND WHAT IT DOES NOT
------------------------------------------
Sees:   the injection script verbatim (ground_truth.py), the agent's
        root_cause_id, its summary, its reasoning.
Does not see:
        the agent's confidence, the tool trace, the cited evidence, or any
        phrase list from eval/rubric.yaml.

Each omission is deliberate and tested:

  confidence  Phase 6 crosses this label with the agent's recorded confidence
              to compute selective accuracy and false-confidence exposure. If
              confidence were visible here, that table would be measuring its
              own input.
  trace       the plan puts investigation quality in a separate, ground-truth
              blind judge (Phase 4). Keeping the two payloads disjoint is what
              makes "CORRECT diagnosis, FAIL evidence" a finding rather than a
              contradiction.
  phrases     the whole point of the redesign is that the substring grader is
              no longer the correctness signal.

FAILURE IS NOT A LABEL
----------------------
A judge that cannot answer returns an error record with `label is None`. It
never returns INCORRECT. Defaulting a failed call to a label is how
infrastructure flakiness gets reported as agent error, and it is the specific
thing `legacy eval/llm_judge.py` came closest to doing by writing
`verdict: "error"` into the same column as real verdicts.

Phase 6 therefore has two denominators to keep straight: runs eligible, and
runs actually judged. Both are reported.

NO HIDDEN CHAIN OF THOUGHT
--------------------------
The prompt asks for a label and one sentence. Extra keys the model volunteers
are dropped rather than stored: a persisted scratchpad is a liability in an
audit trail and the plan asks for a concise justification instead.

Usage:
    # build the real prompts for the eligible runs without calling anything
    agent/venv_py314_blocked/Scripts/python.exe -m eval.semantic_eval.diagnosis_judge --dry-run

    # judge them (needs ANTHROPIC_API_KEY and the anthropic package)
    agent/venv/Scripts/python.exe -m eval.semantic_eval.diagnosis_judge --write

    # check the judge itself against the plan's four worked examples
    agent/venv/Scripts/python.exe -m eval.semantic_eval.diagnosis_judge --self-check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

from .artifacts import RUNS_DIR, LoadedRun, is_recorded, load_runs
from .gate import GateReport, eligible_runs, run_gate
from .ground_truth import FaultGroundTruth, GroundTruthError, load_ground_truth
from .judge_client import (  # noqa: F401 - AnthropicJudge/JudgeClient re-exported
    DEFAULT_JUDGE_MODEL,
    AnthropicJudge,
    JudgeClient,
    JudgeOutputError,
    strip_fence,
)

OUTPUT_DIR = Path(__file__).resolve().parent
JUDGMENTS_JSONL = OUTPUT_DIR / "diagnosis_judgments.jsonl"

MAX_RETRIES = 1


class DiagnosisLabel(Enum):
    """The only four outcomes. PARTIAL is not CORRECT; ABSTAIN is not INCORRECT."""

    CORRECT = "CORRECT"
    PARTIAL = "PARTIAL"
    INCORRECT = "INCORRECT"
    ABSTAIN = "ABSTAIN"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_INSTRUCTIONS = """\
You are grading one incident-diagnosis attempt against the fault that was
actually injected into the system.

You are given the injection script that was executed, and the diagnosis the
agent finally reported. Decide whether the agent identified the injected root
cause.

Labels, and the only four you may use:

CORRECT   The agent identifies the injected mechanism and a materially correct
          affected target or component. Wording need not match; naming the
          target only as vaguely as the evidence allowed is still correct.
PARTIAL   The agent identifies a meaningful part of the right subsystem or
          mechanism but misses an important qualifier, target, or causal step.
INCORRECT The agent attributes the incident to the wrong primary cause,
          contradicts the injected fault, or asserts a materially false root
          cause.
ABSTAIN   The agent explicitly declines to name a root cause, or reports
          insufficient evidence, without asserting a competing primary
          diagnosis.

Judge only what the agent claimed against what the script did. Do not reward
or penalise how the claim was worded, how long it is, or how confident it
sounds.

Reply with ONLY a JSON object, no prose and no markdown fences:

{"label": "CORRECT" | "PARTIAL" | "INCORRECT" | "ABSTAIN",
 "reason": "<one short sentence naming what the agent claimed and how it
             compares to the injected fault>"}
"""

_RETRY_PREFIX = """\
Your previous reply could not be parsed. Reply with ONLY a single JSON object
and nothing else: {"label": "CORRECT" | "PARTIAL" | "INCORRECT" | "ABSTAIN",
"reason": "<one short sentence>"}

"""


def _agent_field(value: object) -> str:
    """Absent text is marked absent, never rendered as an empty string."""
    return str(value) if is_recorded(value) else f"({value})"


def build_diagnosis_prompt(run: LoadedRun, ground_truth: FaultGroundTruth) -> str:
    """
    Assemble the judge payload.

    Only these four things about the run are included. Anything else would have
    to be argued for against the omissions documented at the top of this file.
    """
    return (
        f"{_INSTRUCTIONS}\n"
        "=== What was actually injected ===\n"
        f"{ground_truth.as_judge_text()}\n\n"
        "=== What the agent reported ===\n"
        f"Root cause identifier it assigned: {_agent_field(run.root_cause_id)}\n\n"
        f"Its summary:\n{_agent_field(run.summary)}\n\n"
        f"Its reasoning:\n{_agent_field(run.reasoning)}\n"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_judgment(text: str) -> tuple[DiagnosisLabel, str]:
    """
    Strict parse. Anything unexpected raises rather than being coerced.

    Only `label` and `reason` are read; a `thinking` or `scratchpad` key the
    model volunteers is discarded here and never reaches storage.
    """
    raw = strip_fence(text)
    if not raw:
        raise JudgeOutputError("judge returned an empty response")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JudgeOutputError(f"not JSON ({exc.msg}): {raw[:200]!r}") from exc
    if not isinstance(doc, dict):
        raise JudgeOutputError(f"expected a JSON object, got {type(doc).__name__}")

    if "label" not in doc:
        raise JudgeOutputError(f"no 'label' key in judge output: {sorted(doc)}")
    label_raw = doc["label"]
    if not isinstance(label_raw, str):
        raise JudgeOutputError(
            f"'label' must be one of the four label strings, got {label_raw!r}"
        )
    try:
        label = DiagnosisLabel(label_raw.strip().upper())
    except ValueError as exc:
        raise JudgeOutputError(
            f"{label_raw!r} is not one of "
            f"{', '.join(l.value for l in DiagnosisLabel)}"
        ) from exc

    reason = doc.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise JudgeOutputError("judge output has no usable 'reason'; a verdict "
                               "without a justification is not auditable")
    return label, reason.strip()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagnosisResult:
    """
    One run's verdict, or one explicit reason there is none.

    `label is None` means unjudged. It never means INCORRECT.
    """

    run_id: str
    fault_id: str | None
    script_relpath: str | None
    label: DiagnosisLabel | None
    reason: str | None
    judge_model: str
    attempts: int
    prompt_sha256: str | None
    error_type: str | None = None
    error: str | None = None

    @property
    def judged(self) -> bool:
        return self.label is not None

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "fault_id": self.fault_id,
            "script_relpath": self.script_relpath,
            "label": self.label.value if self.label else None,
            "reason": self.reason,
            "judge_model": self.judge_model,
            "attempts": self.attempts,
            "prompt_sha256": self.prompt_sha256,
            "error_type": self.error_type,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


def judge_run(
    run: LoadedRun,
    ground_truth: FaultGroundTruth,
    client: JudgeClient,
    max_retries: int = MAX_RETRIES,
) -> DiagnosisResult:
    """Judge one run. Returns an error record rather than raising or defaulting."""
    prompt = build_diagnosis_prompt(run, ground_truth)
    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    model = getattr(client, "model", "unknown")

    last_error: tuple[str, str] | None = None
    for attempt in range(1, max_retries + 2):
        payload = prompt if attempt == 1 else _RETRY_PREFIX + prompt
        try:
            raw = client.complete(payload)
        except Exception as exc:  # noqa: BLE001 - provider errors are all shapes
            last_error = ("call_failed", f"{type(exc).__name__}: {exc}")
            continue
        try:
            label, reason = parse_judgment(raw)
        except JudgeOutputError as exc:
            last_error = ("invalid_output", str(exc))
            continue
        return DiagnosisResult(
            run_id=run.run_id,
            fault_id=ground_truth.fault_id,
            script_relpath=ground_truth.script_relpath,
            label=label,
            reason=reason,
            judge_model=model,
            attempts=attempt,
            prompt_sha256=sha,
        )

    error_type, message = last_error or ("call_failed", "no attempt was made")
    return DiagnosisResult(
        run_id=run.run_id,
        fault_id=ground_truth.fault_id,
        script_relpath=ground_truth.script_relpath,
        label=None,
        reason=None,
        judge_model=model,
        attempts=max_retries + 1,
        prompt_sha256=sha,
        error_type=error_type,
        error=message,
    )


def judge_runs(
    runs: Sequence[LoadedRun],
    gate: GateReport,
    client: JudgeClient,
    max_retries: int = MAX_RETRIES,
) -> tuple[DiagnosisResult, ...]:
    """
    Judge every eligible run and nothing else.

    The gate filter is applied here rather than by the caller so that no code
    path exists in which an excluded run reaches a judge.
    """
    results: list[DiagnosisResult] = []
    for run in eligible_runs(runs, gate):
        fault_id = run.fault_id if is_recorded(run.fault_id) else None
        try:
            if fault_id is None:
                raise GroundTruthError("run record declares no fault_id")
            ground_truth = load_ground_truth(fault_id)
        except GroundTruthError as exc:
            results.append(
                DiagnosisResult(
                    run_id=run.run_id,
                    fault_id=fault_id,
                    script_relpath=None,
                    label=None,
                    reason=None,
                    judge_model=getattr(client, "model", "unknown"),
                    attempts=0,
                    prompt_sha256=None,
                    error_type="no_ground_truth",
                    error=str(exc),
                )
            )
            continue
        results.append(judge_run(run, ground_truth, client, max_retries))
    return tuple(results)


def write_judgments(
    results: Sequence[DiagnosisResult], path: Path | str = JUDGMENTS_JSONL
) -> Path:
    """Persist the judge's own output. Never touches `eval/runs/`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for res in results:
            fh.write(json.dumps(res.as_dict(), sort_keys=True) + "\n")
    return path


# ---------------------------------------------------------------------------
# The plan's four worked examples, as a check on the judge itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelfCheckCase:
    """
    One hand-written case where the right label is not in dispute.

    These exist because the judge is a measurement instrument. Offline they
    only exercise the plumbing; run against a live judge (`--self-check`) they
    are a cheap smoke test that it distinguishes the four labels at all. They
    are not a substitute for Phase 5's human calibration.
    """

    name: str
    ground_truth: str
    prediction: str
    expected: DiagnosisLabel


_SELF_CHECK_GT = (
    "Injection script: network/09_latency_injection.sh\n"
    "Fault family: network\n"
    "Stimulus liveness: continuous\n\n"
    "What it did: added a 200 ms tc/netem delay to go-server's eth0 interface. "
    "Redis and CPU are unaffected and healthy; no error lines are produced "
    "anywhere, so the only signal is a latency shift and the throughput drop "
    "that follows from it."
)

SELF_CHECK_CASES: tuple[SelfCheckCase, ...] = (
    SelfCheckCase(
        name="names mechanism and target",
        ground_truth=_SELF_CHECK_GT,
        prediction=(
            "Root cause: network latency on go-server. go-server's calls are taking "
            "~200ms longer than baseline while Redis itself responds in microseconds, "
            "so delay is being added on go-server's network path."
        ),
        expected=DiagnosisLabel.CORRECT,
    ),
    SelfCheckCase(
        name="right subsystem, no target",
        ground_truth=_SELF_CHECK_GT,
        prediction=(
            "Root cause: there is a network problem somewhere in the pipeline causing "
            "slow responses. I could not determine which service or link is affected."
        ),
        expected=DiagnosisLabel.PARTIAL,
    ),
    SelfCheckCase(
        name="wrong primary cause",
        ground_truth=_SELF_CHECK_GT,
        prediction=(
            "Root cause: Redis is saturated. The cluster is overloaded and cannot keep "
            "up with the request rate, which is why latency has increased."
        ),
        expected=DiagnosisLabel.INCORRECT,
    ),
    SelfCheckCase(
        name="explicit abstention",
        ground_truth=_SELF_CHECK_GT,
        prediction=(
            "I was unable to identify a root cause. The evidence I gathered was "
            "insufficient to determine what is wrong, and I am not going to guess."
        ),
        expected=DiagnosisLabel.ABSTAIN,
    ),
)


def build_self_check_prompt(case: SelfCheckCase) -> str:
    return (
        f"{_INSTRUCTIONS}\n"
        "=== What was actually injected ===\n"
        f"{case.ground_truth}\n\n"
        "=== What the agent reported ===\n"
        f"Root cause identifier it assigned: (not_recorded)\n\n"
        f"Its summary:\n{case.prediction}\n\n"
        f"Its reasoning:\n(not_recorded)\n"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _summarize(results: Sequence[DiagnosisResult]) -> None:
    judged = [r for r in results if r.judged]
    print()
    print(f"judged {len(judged)} of {len(results)} eligible run(s)")
    for label in DiagnosisLabel:
        n = sum(1 for r in judged if r.label is label)
        print(f"  {label.value:10} {n}")
    unjudged = [r for r in results if not r.judged]
    if unjudged:
        print(f"  unjudged   {len(unjudged)}  (NOT counted as INCORRECT)")
        for r in unjudged:
            print(f"      {r.run_id}: [{r.error_type}] {r.error}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Judge whether each eligible run identified the injected root cause."
    )
    parser.add_argument("--runs-dir", default=str(RUNS_DIR))
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and print the real prompts without calling the judge",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="run the four worked examples against the live judge",
    )
    parser.add_argument("--write", action="store_true", help="write the judgments JSONL")
    args = parser.parse_args(argv)

    if args.self_check:
        client = AnthropicJudge(model=args.model)
        failures = 0
        for case in SELF_CHECK_CASES:
            try:
                label, reason = parse_judgment(client.complete(build_self_check_prompt(case)))
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR  {case.name}: {type(exc).__name__}: {exc}")
                failures += 1
                continue
            ok = label is case.expected
            failures += 0 if ok else 1
            print(
                f"  {'ok  ' if ok else 'MISS'}  {case.name}: expected "
                f"{case.expected.value}, got {label.value} - {reason}"
            )
        print(f"\n{len(SELF_CHECK_CASES) - failures}/{len(SELF_CHECK_CASES)} cases matched")
        return 1 if failures else 0

    gate = run_gate(args.runs_dir)
    runs = load_runs(args.runs_dir)
    population = eligible_runs(runs, gate)

    print(f"eligible for diagnosis judging: {len(population)} of {gate.total_runs}")
    for run in population:
        print(f"  {run.run_id} (fault {run.fault_id})")

    if args.dry_run:
        for run in population:
            gt = load_ground_truth(str(run.fault_id))
            prompt = build_diagnosis_prompt(run, gt)
            print()
            print("=" * 78)
            print(f"{run.run_id}  [{len(prompt)} chars, "
                  f"sha256 {hashlib.sha256(prompt.encode()).hexdigest()[:12]}]")
            print("=" * 78)
            print(prompt)
        return 0

    results = judge_runs(runs, gate, AnthropicJudge(model=args.model))
    _summarize(results)
    for res in results:
        if res.judged:
            print(f"  {res.run_id}: {res.label.value} - {res.reason}")
    if args.write:
        print(f"\nwrote {write_judgments(results)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        __package__ = "eval.semantic_eval"
    raise SystemExit(main())
