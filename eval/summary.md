# Eval summary

Total graded runs: 20


## claude-desktop-haiku4.5  (n=2)

- Raw accuracy: 1/2 (50%, Wilson 95% CI 9%-91%)
- Coverage (confirmed): 100% (CI 34%-100%)
- Selective accuracy (of confirmed, % actually right): 50%
- **False-confidence rate: 1/2 (50%, CI 9%-91%)**
- Confirmed-recall (of correct answers, % flagged confident): 100%
- Inconclusive (honest abstain) count: 0 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=2 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=2, correct=1, accuracy=50%
  - best_guess: n=0, correct=0, accuracy=n/a
  - inconclusive: n=0, correct=0, accuracy=n/a
- Corroboration-gate ablation: gate fired on 0/2 runs; false-confidence rate WITHOUT the gate (raw self-reported) = 50% vs WITH the gate = 50%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):

- Cost-weighted skill score (confirmed-wrong=10x): -0.67
  - Sensitivity sweep: 5x=0.17, 10x=-0.67, 20x=-2.33
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 0%

- Mean calls-to-resolution (correct answers only): 8.0
- Mean fraction of cited evidence from the free baseline sweep: n/a
- Mean evidence groundedness: n/a
- Duplicate/tool-error/hallucination rate per run: None/None/None

- Mean wall-clock: n/a
- Mean tokens in/out: None/None
- Tokens/sec: n/a

## claude-desktop-opus5  (n=2)

- Raw accuracy: 2/2 (100%, Wilson 95% CI 34%-100%)
- Coverage (confirmed): 100% (CI 34%-100%)
- Selective accuracy (of confirmed, % actually right): 100%
- **False-confidence rate: 0/2 (0%, CI 0%-66%)**
- Confirmed-recall (of correct answers, % flagged confident): 100%
- Inconclusive (honest abstain) count: 0 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=2 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=2, correct=2, accuracy=100%
  - best_guess: n=0, correct=0, accuracy=n/a
  - inconclusive: n=0, correct=0, accuracy=n/a
- Corroboration-gate ablation: gate fired on 0/2 runs; false-confidence rate WITHOUT the gate (raw self-reported) = 0% vs WITH the gate = 0%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):

- Cost-weighted skill score (confirmed-wrong=10x): 1.00
  - Sensitivity sweep: 5x=1.00, 10x=1.00, 20x=1.00
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 0%

- Mean calls-to-resolution (correct answers only): 14.0
- Mean fraction of cited evidence from the free baseline sweep: n/a
- Mean evidence groundedness: n/a
- Duplicate/tool-error/hallucination rate per run: None/None/None

- Mean wall-clock: n/a
- Mean tokens in/out: None/None
- Tokens/sec: n/a

## claude-desktop-sonnet5  (n=2)

- Raw accuracy: 2/2 (100%, Wilson 95% CI 34%-100%)
- Coverage (confirmed): 100% (CI 34%-100%)
- Selective accuracy (of confirmed, % actually right): 100%
- **False-confidence rate: 0/2 (0%, CI 0%-66%)**
- Confirmed-recall (of correct answers, % flagged confident): 100%
- Inconclusive (honest abstain) count: 0 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=2 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=2, correct=2, accuracy=100%
  - best_guess: n=0, correct=0, accuracy=n/a
  - inconclusive: n=0, correct=0, accuracy=n/a
- Corroboration-gate ablation: gate fired on 0/2 runs; false-confidence rate WITHOUT the gate (raw self-reported) = 0% vs WITH the gate = 0%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):

- Cost-weighted skill score (confirmed-wrong=10x): 1.00
  - Sensitivity sweep: 5x=1.00, 10x=1.00, 20x=1.00
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 0%

- Mean calls-to-resolution (correct answers only): 19.5
- Mean fraction of cited evidence from the free baseline sweep: n/a
- Mean evidence groundedness: n/a
- Duplicate/tool-error/hallucination rate per run: None/None/None

- Mean wall-clock: n/a
- Mean tokens in/out: None/None
- Tokens/sec: n/a

## kaggle-qwen14b (rag=off)  (n=4)

- Raw accuracy: 2/4 (50%, Wilson 95% CI 15%-85%)
- Coverage (confirmed): 25% (CI 5%-70%)
- Selective accuracy (of confirmed, % actually right): 100%
- **False-confidence rate: 0/4 (0%, CI 0%-49%)**
- Confirmed-recall (of correct answers, % flagged confident): 50%
- Inconclusive (honest abstain) count: 1 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=4 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=1, correct=1, accuracy=100%
  - best_guess: n=2, correct=1, accuracy=50%
  - inconclusive: n=1, correct=0, accuracy=0%
- Corroboration-gate ablation: gate fired on 0/4 runs; false-confidence rate WITHOUT the gate (raw self-reported) = 0% vs WITH the gate = 0%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):
    - held_out_with_neighbor: n/a
    - indexed: n/a

- Cost-weighted skill score (confirmed-wrong=10x): 0.25
  - Sensitivity sweep: 5x=0.25, 10x=0.25, 20x=0.25
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 0%

- Mean calls-to-resolution (correct answers only): 12.5
- Mean fraction of cited evidence from the free baseline sweep: 54%
- Mean evidence groundedness: 100%
- Duplicate/tool-error/hallucination rate per run: 6.25/0.0/0.0

- Mean wall-clock per investigation: 414.0s
- Mean tokens in/out: 143198.0/2291.5
- Tokens/sec: 6.2

## kaggle-qwen14b (rag=on)  (n=4)

- Raw accuracy: 2/4 (50%, Wilson 95% CI 15%-85%)
- Coverage (confirmed): 50% (CI 15%-85%)
- Selective accuracy (of confirmed, % actually right): 50%
- **False-confidence rate: 1/4 (25%, CI 5%-70%)**
- Confirmed-recall (of correct answers, % flagged confident): 50%
- Inconclusive (honest abstain) count: 0 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=4 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=2, correct=1, accuracy=50%
  - best_guess: n=2, correct=1, accuracy=50%
  - inconclusive: n=0, correct=0, accuracy=n/a
- Corroboration-gate ablation: gate fired on 0/4 runs; false-confidence rate WITHOUT the gate (raw self-reported) = 25% vs WITH the gate = 25%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):
    - held_out_with_neighbor (n=2): hit@1=50%, MRR=0.50
    - indexed (n=2): hit@1=100%, MRR=1.00 (expected ~100% - known-item search, this is leakage not generalization)

- Cost-weighted skill score (confirmed-wrong=10x): -0.33
  - Sensitivity sweep: 5x=0.08, 10x=-0.33, 20x=-1.17
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 33%

- Mean calls-to-resolution (correct answers only): 15.0
- Mean fraction of cited evidence from the free baseline sweep: 52%
- Mean evidence groundedness: 92%
- Duplicate/tool-error/hallucination rate per run: 7.0/0.0/0.0

- Mean wall-clock per investigation: 439.9s
- Mean tokens in/out: 160758.0/2853.25
- Tokens/sec: 7.3

## local-qwen9b (rag=off)  (n=3)

- Raw accuracy: 1/3 (33%, Wilson 95% CI 6%-79%)
- Coverage (confirmed): 67% (CI 21%-94%)
- Selective accuracy (of confirmed, % actually right): 50%
- **False-confidence rate: 1/3 (33%, CI 6%-79%)**
- Confirmed-recall (of correct answers, % flagged confident): 100%
- Inconclusive (honest abstain) count: 0 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=3 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=2, correct=1, accuracy=50%
  - best_guess: n=1, correct=0, accuracy=0%
  - inconclusive: n=0, correct=0, accuracy=n/a
- Corroboration-gate ablation: gate fired on 1/3 runs; false-confidence rate WITHOUT the gate (raw self-reported) = 67% vs WITH the gate = 33%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):
    - held_out_with_neighbor: n/a
    - indexed: n/a

- Cost-weighted skill score (confirmed-wrong=10x): -0.67
  - Sensitivity sweep: 5x=-0.11, 10x=-0.67, 20x=-1.78
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 33%

- Mean calls-to-resolution (correct answers only): 0.0
- Mean fraction of cited evidence from the free baseline sweep: 100%
- Mean evidence groundedness: 100%
- Duplicate/tool-error/hallucination rate per run: 0.0/0.0/0.0

- Mean wall-clock per investigation: 99.6s
- Mean tokens in/out: 3872.3333333333335/1045.0
- Tokens/sec: 15.3

## local-qwen9b (rag=on)  (n=3)

- Raw accuracy: 1/3 (33%, Wilson 95% CI 6%-79%)
- Coverage (confirmed): 100% (CI 44%-100%)
- Selective accuracy (of confirmed, % actually right): 33%
- **False-confidence rate: 2/3 (67%, CI 21%-94%)**
- Confirmed-recall (of correct answers, % flagged confident): 100%
- Inconclusive (honest abstain) count: 0 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=3 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=3, correct=1, accuracy=33%
  - best_guess: n=0, correct=0, accuracy=n/a
  - inconclusive: n=0, correct=0, accuracy=n/a
- Corroboration-gate ablation: gate fired on 0/3 runs; false-confidence rate WITHOUT the gate (raw self-reported) = 67% vs WITH the gate = 67%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):
    - held_out_with_neighbor (n=1): hit@1=100%, MRR=1.00
    - indexed (n=2): hit@1=100%, MRR=1.00 (expected ~100% - known-item search, this is leakage not generalization)

- Cost-weighted skill score (confirmed-wrong=10x): -1.22
  - Sensitivity sweep: 5x=-0.11, 10x=-1.22, 20x=-3.44
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 33%

- Mean calls-to-resolution (correct answers only): 0.0
- Mean fraction of cited evidence from the free baseline sweep: 100%
- Mean evidence groundedness: n/a
- Duplicate/tool-error/hallucination rate per run: 0.0/0.0/0.0

- Mean wall-clock per investigation: 102.0s
- Mean tokens in/out: 3872.3333333333335/836.6666666666666
- Tokens/sec: 12.0