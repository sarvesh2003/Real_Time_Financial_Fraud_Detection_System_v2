# Validity gate

One question: **is this agent evaluation measuring what it claims to measure?**

Not whether the agent is good. Whether the number is about the thing on the
label. It runs before you publish, and it exits non-zero when it is not.

```
agent/venv_py314_blocked/Scripts/python.exe eval/validity/run.py
```

Add `--strict` to fail on warnings too, `--quiet` to hide info findings,
`--list` to print the registered checks and stop. The console report is the
primary output; `eval/validity_report.md` is written as a side effect so a
result can be committed alongside the summary it gates.

Phase 1 is deterministic. No model is called. Every finding is a count, a
timestamp, a set membership, a string match or a schema difference, which
means a finding is either true or a bug, never a judgement call that has to be
re-litigated.

## The taxonomy

| class | invariant question |
| --- | --- |
| `stimulus_validity` | Was the thing under test present when the agent observed? |
| `context_integrity` | Did the agent receive what the harness sent? |
| `leakage` | Could the answer be produced without doing the work? |
| `behavioral_evidence` | Did the agent actually do the work it got credit for? |
| `scoring_integrity` | Is the score computed from model output only? |
| `comparability` | Are pooled rows the same experiment? |
| `metric_soundness` | Can this metric mean what its label says? |

`stimulus_validity` is Phase 2. It needs per run injection and restore
timestamps that this harness does not emit, so its one check skips and says
what would make it live. It is not approximated, because the approximation
available (assume the fault was live for the whole run) is the exact
assumption the check exists to test.

## The contract

```
adapter.py  ->  Manifest  ->  checks/*.py
```

Checks read the manifest and nothing else. They never open a file, never know
a model name, never know an environment label, never know a fault id, and
never compare against a constant that came from looking at this data. A check
states an invariant and derives its comparison points from the manifest, so it
can fire on a repository nobody involved has seen.

All repository specific knowledge lives in `adapter.py`. Porting the gate to
another harness means rewriting that one file. `manifest.py` documents the
structures it has to produce; the short version is in
`manifest.describe_contract()` and at the end of every generated report.

The concepts that carry the most weight:

- **free context** (`TextSource.free`). Text that reached the model without
  the agent choosing to fetch it: instructions, tool schemas, a scripted warm
  up sweep, injected retrieval results. Be generous about what counts. The
  leakage check is only as good as this flag.
- **provenance** (`ScoredText.provenance`). Who wrote each field the grader
  reads. `model`, `human`, `harness` or `unknown`, and `unknown` is treated as
  not-model.
- **agent initiated** (`Action.agent_initiated`). Work the agent chose to do,
  as distinct from work the harness did on its behalf.
- **declared limits and policy outcomes** (`Run.limits`,
  `Run.policy_outcomes`, `Run.policy_antecedents`). The rules each individual
  row ran under, so that pooling rows from two harness versions is visible.
- **record schema** (`Run.schema_fields`). The field names the unflattened
  record carried. A field that is absent rather than empty identifies which
  version of the emitter wrote the row.

A manifest that omits something causes the checks that need it to **skip**,
loudly, in their own section of the report. Skip is not pass. A run where most
checks skipped means the harness is not instrumented for validity, not that
the evaluation is clean.

## Adding a check

One decorated function. Nothing else is wired.

```python
# eval/validity/checks/leakage.py

@check(
    check_id="answer_reachable_from_free_context",
    failure_class="leakage",
    title="Full credit is not constructible from free context",
    invariant=(
        "No run may have every component its grader requires for full credit "
        "present in text the agent received without spending an action."
    ),
)
def answer_reachable_from_free_context(m: Manifest) -> Iterable[Finding]:
    if not any(m.free_context_for(r) for r in m.included()):
        raise SkipCheck("requires free context; populate Run.free_context")
    ...
    yield Finding(severity="fail", message="...", run_ids=(run.run_id,))
```

Rules, in descending order of how much trouble breaking them causes:

1. **The invariant sentence must survive being read by somebody who has never
   seen this repository.** If it needs a model name, an environment label or a
   number somebody picked, the check is not generic. That sentence is printed
   verbatim in the report and is the check's real specification.
2. **Derive every comparison point from the data.** Not `> 2050`, but "below
   every other value in the report". Not `== "local-qwen9b"`, but "the cluster
   of runs sharing one value". Where a structural criterion genuinely needs a
   constant, for example how many runs at the same maximum count as
   saturation rather than coincidence, name it, explain what it means, and put
   it at the top of the module.
3. **Skip rather than assume.** A missing field is a reason to raise
   `SkipCheck` with instructions for the adapter author, never a reason to
   substitute a default.
4. **Never tune a check to catch a known defect.** A generic check that misses
   a defect you know about is information: report the miss. A check bent to
   catch one row proves nothing about the next repository.
5. **Findings name rows and phrases.** A finding that says something leaked
   without naming the phrase and the source cannot be acted on.
6. **Severity is per finding, not per check.** The same comparison usually has
   a strong form that blocks publication and a weak form that is worth saying.

A new check module needs its name added to `_MODULES` in
`checks/__init__.py`. A check added to an existing module needs nothing.

## What this supersedes

`eval/check_contamination.py` implemented two of these checks first and got
the idea right: mirror the grader's matcher exactly, and treat a full marks
run with an empty tool call sequence as evidence of a leak. It scanned only
the system prompt and the tool docstrings, so it never saw that the free
baseline sweep alone contains a complete accept set for several faults. Both
of its checks live on here in generic form, over a wider definition of free
context. The old file is left in place and still runs.
