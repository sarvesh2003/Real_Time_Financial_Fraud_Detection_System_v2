"""
Entrypoint for the validity gate.

Builds a manifest through the adapter, runs every registered check against it,
prints a report and writes eval/validity_report.md. Exits 1 when any invariant
is violated, so it can sit in front of publication rather than beside it.

WHY THE REPORT IS SHAPED THE WAY IT IS
--------------------------------------
The report is organized by failure class rather than by severity, because the
question a reader arrives with is "is this number publishable", and that is
answered class by class: the answer leaked, the rows are not the same
experiment, the metric cannot mean what it says. A flat list sorted by severity
would scatter one underlying defect across three places.

Skipped checks get their own section with the reason, at the end and in full.
A gate that reports "0 failures" while half its checks looked at nothing is
worse than no gate, because it launders an absence of instrumentation into a
statement of quality.

Usage:
    agent/venv_py314_blocked/Scripts/python.exe -m eval.validity.run
    agent/venv_py314_blocked/Scripts/python.exe eval/validity/run.py [--strict] [--quiet]

    --strict  treat warnings as failures
    --quiet   suppress info-level findings in the console output
Exit codes:
    0  every check that ran passed
    1  at least one invariant is violated (or a check errored)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable

if __package__ in (None, ""):
    # Allow running the file directly as well as with -m.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    __package__ = "eval.validity"

from .adapter import build_manifest  # noqa: E402
from .checks import load_all  # noqa: E402
from .manifest import Manifest, describe_contract  # noqa: E402
from .registry import (  # noqa: E402
    CLASS_QUESTION,
    CheckResult,
    iter_by_class,
    registered,
    run_all,
)

REPORT_PATH = Path(__file__).resolve().parent.parent / "validity_report.md"

STATUS_LABEL = {
    "pass": "PASS",
    "warn": "WARN",
    "fail": "FAIL",
    "skip": "SKIP",
    "error": "ERROR",
}

MAX_RUN_IDS_SHOWN = 8


def _run_ids(ids: tuple[str, ...]) -> str:
    if not ids:
        return ""
    shown = list(ids[:MAX_RUN_IDS_SHOWN])
    if len(ids) > MAX_RUN_IDS_SHOWN:
        shown.append(f"... and {len(ids) - MAX_RUN_IDS_SHOWN} more")
    return "rows: " + ", ".join(shown)


def _wrap(text: str, width: int, indent: str) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(indent + current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(indent + current)
    return lines


def render(manifest: Manifest, results: list[CheckResult], quiet: bool) -> list[str]:
    out: list[str] = []
    add = out.append

    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0, "error": 0}
    for r in results:
        counts[r.status] += 1

    add("VALIDITY GATE")
    add("=" * 78)
    add(f"harness      {manifest.harness}")
    add(f"generated    {manifest.generated_at}")
    add(f"population   {len(manifest.included())} run(s) in "
        f"{len(manifest.groups())} pooled group(s), "
        f"{len({r.stimulus_id for r in manifest.included()})} distinct stimuli")
    add(f"checks       {len(results)} registered: "
        + ", ".join(f"{counts[k]} {STATUS_LABEL[k].lower()}" for k in
                    ("fail", "warn", "pass", "skip", "error") if counts[k]))
    add("")

    for cls, cls_results in iter_by_class(results):
        if not cls_results:
            continue
        add(f"{cls}  -  {CLASS_QUESTION[cls]}")
        add("-" * 78)
        for result in cls_results:
            add(f"  [{STATUS_LABEL[result.status]}] {result.spec.check_id}")
            for line in _wrap(result.spec.invariant, 70, "        "):
                add(line)
            if result.status == "skip":
                for line in _wrap(f"skipped: {result.skip_reason}", 70, "        "):
                    add(line)
            if result.status == "error":
                for line in result.error.splitlines():
                    add(f"        {line}")
            for finding in result.findings:
                if quiet and finding.severity == "info":
                    continue
                add("")
                for i, line in enumerate(_wrap(finding.message, 66, "")):
                    prefix = f"      {finding.severity.upper():5} " if i == 0 else "            "
                    add(prefix + line.strip())
                if finding.run_ids:
                    for line in _wrap(_run_ids(finding.run_ids), 62, "            "):
                        add(line)
                for det in finding.detail:
                    for line in _wrap(det, 62, "            "):
                        add(line)
            add("")
        add("")

    add("=" * 78)
    failed = [r for r in results if r.failed]
    warned = [r for r in results if r.status == "warn"]
    skipped = [r for r in results if r.status == "skip"]
    if failed:
        add(f"NOT PUBLISHABLE: {len(failed)} invariant(s) violated.")
        for r in failed:
            add(f"  - {r.spec.failure_class}/{r.spec.check_id}")
    else:
        add("No invariant violated by the checks that ran.")
    if warned:
        add(f"{len(warned)} check(s) raised warnings.")
    if skipped:
        add(f"{len(skipped)} check(s) could not run and proved nothing:")
        for r in skipped:
            add(f"  - {r.spec.failure_class}/{r.spec.check_id}")
    if manifest.notes:
        add("")
        add("adapter notes")
        for note in manifest.notes:
            for line in _wrap(note, 74, "  "):
                add(line)
    return out


def render_markdown(manifest: Manifest, results: list[CheckResult]) -> str:
    lines = ["# Validity report", ""]
    lines.append(
        "Generated by `eval/validity/run.py`. This report says nothing about how good "
        "the agent is. It says whether the numbers in `eval/summary.md` are measuring "
        "what they claim to measure."
    )
    lines.append("")
    lines.append(f"- harness: {manifest.harness}")
    lines.append(f"- generated: {manifest.generated_at}")
    lines.append(
        f"- population: {len(manifest.included())} runs, {len(manifest.groups())} pooled groups"
    )
    failed = [r for r in results if r.failed]
    lines.append(
        f"- verdict: **{'NOT PUBLISHABLE' if failed else 'no invariant violated'}** "
        f"({len(failed)} failing, "
        f"{sum(1 for r in results if r.status == 'warn')} warning, "
        f"{sum(1 for r in results if r.status == 'skip')} skipped)"
    )
    lines.append("")

    lines.append("| class | check | status | findings |")
    lines.append("| --- | --- | --- | --- |")
    for result in results:
        counts = result.counts()
        summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "-"
        lines.append(
            f"| {result.spec.failure_class} | `{result.spec.check_id}` | "
            f"{STATUS_LABEL[result.status]} | {summary} |"
        )
    lines.append("")

    for cls, cls_results in iter_by_class(results):
        if not cls_results:
            continue
        lines.append(f"## {cls}")
        lines.append("")
        lines.append(f"_{CLASS_QUESTION[cls]}_")
        lines.append("")
        for result in cls_results:
            lines.append(f"### {STATUS_LABEL[result.status]} `{result.spec.check_id}`")
            lines.append("")
            lines.append(f"**Invariant.** {result.spec.invariant}")
            lines.append("")
            if result.status == "skip":
                lines.append(f"Skipped: {result.skip_reason}")
                lines.append("")
                continue
            if result.status == "error":
                lines.append("```")
                lines.append(result.error)
                lines.append("```")
                lines.append("")
                continue
            if not result.findings:
                lines.append("No findings.")
                lines.append("")
                continue
            for finding in result.findings:
                lines.append(f"- **{finding.severity.upper()}** {finding.message}")
                if finding.run_ids:
                    lines.append(f"  - {_run_ids(finding.run_ids)}")
                for det in finding.detail:
                    lines.append(f"  - {det}")
            lines.append("")

    lines.append("## Contract")
    lines.append("")
    lines.append("```")
    lines.extend(describe_contract())
    lines.append("```")
    lines.append("")
    if manifest.notes:
        lines.append("## Adapter notes")
        lines.append("")
        for note in manifest.notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the validity gate.")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    parser.add_argument("--quiet", action="store_true", help="hide info findings on stdout")
    parser.add_argument(
        "--list", action="store_true", help="list registered checks and exit"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    load_all()

    if args.list:
        for spec in registered():
            print(f"{spec.failure_class:22} {spec.check_id}")
            print(f"{'':22} {spec.invariant}")
        return 0

    manifest = build_manifest()
    results = run_all(manifest)

    print("\n".join(render(manifest, results, quiet=args.quiet)))

    REPORT_PATH.write_text(render_markdown(manifest, results), encoding="utf-8")
    print(f"\nwrote {REPORT_PATH}")

    failed = any(r.failed for r in results)
    warned = any(r.status == "warn" for r in results)
    if failed or (args.strict and warned):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
