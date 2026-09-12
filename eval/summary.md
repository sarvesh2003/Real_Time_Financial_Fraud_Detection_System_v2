# Eval summary

Total graded runs: 38


## claude-desktop-haiku4.5  (n=5)

- Raw accuracy: 2/5 (40%, Wilson 95% CI 12%-77%)
- Coverage (confirmed): 40% (CI 12%-77%)
- Selective accuracy (of confirmed, % actually right): 50%
- **False-confidence rate: 1/5 (20%, CI 4%-62%)**
- Confirmed-recall (of correct answers, % flagged confident): 50%
- Inconclusive (honest abstain) count: 1 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=5 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=2, correct=1, accuracy=50%
  - best_guess: n=2, correct=1, accuracy=50%
  - inconclusive: n=1, correct=0, accuracy=0%
- Confidence-downgrade ablation: any gate fired on 0/5 runs (0 corroboration, 0 budget-exhaustion, 0 from before downgrade_reason was tracked); false-confidence rate WITHOUT any gate (raw self-reported) = 20% vs WITH the gate(s) = 20%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):

- Cost-weighted skill score (confirmed-wrong=10x): -0.27
  - Sensitivity sweep: 5x=0.07, 10x=-0.27, 20x=-0.93
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
- Confidence-downgrade ablation: any gate fired on 0/2 runs (0 corroboration, 0 budget-exhaustion, 0 from before downgrade_reason was tracked); false-confidence rate WITHOUT any gate (raw self-reported) = 0% vs WITH the gate(s) = 0%

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

## claude-desktop-sonnet5  (n=5)

- Raw accuracy: 4/5 (80%, Wilson 95% CI 38%-96%)
- Coverage (confirmed): 100% (CI 57%-100%)
- Selective accuracy (of confirmed, % actually right): 80%
- **False-confidence rate: 1/5 (20%, CI 4%-62%)**
- Confirmed-recall (of correct answers, % flagged confident): 100%
- Inconclusive (honest abstain) count: 0 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=5 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=5, correct=4, accuracy=80%
  - best_guess: n=0, correct=0, accuracy=n/a
  - inconclusive: n=0, correct=0, accuracy=n/a
- Confidence-downgrade ablation: any gate fired on 0/5 runs (0 corroboration, 0 budget-exhaustion, 0 from before downgrade_reason was tracked); false-confidence rate WITHOUT any gate (raw self-reported) = 20% vs WITH the gate(s) = 20%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):

- Cost-weighted skill score (confirmed-wrong=10x): 0.33
  - Sensitivity sweep: 5x=0.67, 10x=0.33, 20x=-0.33
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 0%

- Mean calls-to-resolution (correct answers only): 19.5
- Mean fraction of cited evidence from the free baseline sweep: n/a
- Mean evidence groundedness: n/a
- Duplicate/tool-error/hallucination rate per run: None/None/None

- Mean wall-clock: n/a
- Mean tokens in/out: None/None
- Tokens/sec: n/a

## kaggle-qwen14b (rag=off)  (n=7)

- Raw accuracy: 2/7 (29%, Wilson 95% CI 8%-64%)
- Coverage (confirmed): 14% (CI 3%-51%)
- Selective accuracy (of confirmed, % actually right): 100%
- **False-confidence rate: 0/7 (0%, CI 0%-35%)**
- Confirmed-recall (of correct answers, % flagged confident): 50%
- Inconclusive (honest abstain) count: 2 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=7 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=1, correct=1, accuracy=100%
  - best_guess: n=4, correct=1, accuracy=25%
  - inconclusive: n=2, correct=0, accuracy=0%
- Confidence-downgrade ablation: any gate fired on 0/7 runs (0 corroboration, 0 budget-exhaustion, 0 from before downgrade_reason was tracked); false-confidence rate WITHOUT any gate (raw self-reported) = 0% vs WITH the gate(s) = 0%

- Contrast-pair accuracy (strict, both must be correct): 0/1 (0%)
    - fault 13 vs 4: FAIL (scores -1.0/-1.0)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):
    - held_out_no_neighbor (n=1): correct behavior is retrieving NOTHING relevant - false-positive hits: 0/1
    - held_out_with_neighbor: n/a
    - indexed: n/a

- Cost-weighted skill score (confirmed-wrong=10x): -0.05
  - Sensitivity sweep: 5x=-0.05, 10x=-0.05, 20x=-0.05
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 0%

- Mean calls-to-resolution (correct answers only): 12.5
- Mean fraction of cited evidence from the free baseline sweep: 40%
- Mean evidence groundedness: 95%
- Duplicate/tool-error/hallucination rate per run: 5.857142857142857/0.0/0.0

- Mean wall-clock per investigation: 498.5s
- Mean tokens in/out: 159723.85714285713/2351.0
- Tokens/sec: 5.2

## kaggle-qwen14b (rag=on)  (n=7)

- Raw accuracy: 2/7 (29%, Wilson 95% CI 8%-64%)
- Coverage (confirmed): 29% (CI 8%-64%)
- Selective accuracy (of confirmed, % actually right): 50%
- **False-confidence rate: 1/7 (14%, CI 3%-51%)**
- Confirmed-recall (of correct answers, % flagged confident): 50%
- Inconclusive (honest abstain) count: 2 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=7 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=2, correct=1, accuracy=50%
  - best_guess: n=3, correct=1, accuracy=33%
  - inconclusive: n=2, correct=0, accuracy=0%
- Confidence-downgrade ablation: any gate fired on 0/7 runs (0 corroboration, 0 budget-exhaustion, 0 from before downgrade_reason was tracked); false-confidence rate WITHOUT any gate (raw self-reported) = 14% vs WITH the gate(s) = 14%

- Contrast-pair accuracy (strict, both must be correct): 0/1 (0%)
    - fault 13 vs 4: FAIL (scores -1.0/0.0)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):
    - held_out_no_neighbor (n=1): correct behavior is retrieving NOTHING relevant - false-positive hits: 0/1
    - held_out_with_neighbor (n=3): hit@1=33%, MRR=0.50
    - indexed (n=3): hit@1=100%, MRR=1.00 (expected ~100% - known-item search, this is leakage not generalization)

- Cost-weighted skill score (confirmed-wrong=10x): -0.29
  - Sensitivity sweep: 5x=-0.05, 10x=-0.29, 20x=-0.76
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 25%

- Mean calls-to-resolution (correct answers only): 15.0
- Mean fraction of cited evidence from the free baseline sweep: 45%
- Mean evidence groundedness: 92%
- Duplicate/tool-error/hallucination rate per run: 5.428571428571429/0.0/0.0

- Mean wall-clock per investigation: 509.0s
- Mean tokens in/out: 166793.42857142858/2632.5714285714284
- Tokens/sec: 5.7

## kaggle-qwen32b (rag=off)  (n=3)

- Raw accuracy: 0/3 (0%, Wilson 95% CI 0%-56%)
- Coverage (confirmed): 0% (CI 0%-56%)
- Selective accuracy (of confirmed, % actually right): n/a
- **False-confidence rate: 0/3 (0%, CI 0%-56%)**
- Confirmed-recall (of correct answers, % flagged confident): n/a
- Inconclusive (honest abstain) count: 2 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=3 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=0, correct=0, accuracy=n/a
  - best_guess: n=1, correct=0, accuracy=0%
  - inconclusive: n=2, correct=0, accuracy=0%
- Confidence-downgrade ablation: any gate fired on 1/3 runs (0 corroboration, 1 budget-exhaustion, 0 from before downgrade_reason was tracked); false-confidence rate WITHOUT any gate (raw self-reported) = 33% vs WITH the gate(s) = 0%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):
    - held_out_no_neighbor (n=1): correct behavior is retrieving NOTHING relevant - false-positive hits: 0/1
    - held_out_with_neighbor: n/a
    - indexed: n/a

- Cost-weighted skill score (confirmed-wrong=10x): -0.22
  - Sensitivity sweep: 5x=-0.22, 10x=-0.22, 20x=-0.22
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 0%

- Mean calls-to-resolution: n/a
- Mean fraction of cited evidence from the free baseline sweep: 63%
- Mean evidence groundedness: n/a
- Duplicate/tool-error/hallucination rate per run: 5.0/0.0/0.0

- Mean wall-clock per investigation: 806.3s
- Mean tokens in/out: 116889.33333333333/2098.0
- Tokens/sec: 2.7

## kaggle-qwen32b (rag=on)  (n=3)

- Raw accuracy: 0/3 (0%, Wilson 95% CI 0%-56%)
- Coverage (confirmed): 0% (CI 0%-56%)
- Selective accuracy (of confirmed, % actually right): n/a
- **False-confidence rate: 0/3 (0%, CI 0%-56%)**
- Confirmed-recall (of correct answers, % flagged confident): n/a
- Inconclusive (honest abstain) count: 3 (of which 0 were parse failures, not a real abstention decision)
- Crashed runs (n=3 includes these; priced worse than inconclusive in the cost score below): 0

Reliability table:
  - confirmed: n=0, correct=0, accuracy=n/a
  - best_guess: n=0, correct=0, accuracy=n/a
  - inconclusive: n=3, correct=0, accuracy=0%
- Confidence-downgrade ablation: any gate fired on 0/3 runs (0 corroboration, 0 budget-exhaustion, 0 from before downgrade_reason was tracked); false-confidence rate WITHOUT any gate (raw self-reported) = 0% vs WITH the gate(s) = 0%

- Contrast-pair accuracy (strict, both must be correct): 0/0 (n/a)

- Retrieval, by split (NOT pooled - each split has a different correct behavior):
    - held_out_no_neighbor (n=1): correct behavior is retrieving NOTHING relevant - false-positive hits: 0/1
    - held_out_with_neighbor (n=1): hit@1=0%, MRR=0.33
    - indexed (n=1): hit@1=100%, MRR=1.00 (expected ~100% - known-item search, this is leakage not generalization)

- Cost-weighted skill score (confirmed-wrong=10x): 0.00
  - Sensitivity sweep: 5x=0.00, 10x=0.00, 20x=0.00
- Scripted-baseline-only accuracy (on baseline-sufficient faults): 0%

- Mean calls-to-resolution: n/a
- Mean fraction of cited evidence from the free baseline sweep: 60%
- Mean evidence groundedness: 100%
- Duplicate/tool-error/hallucination rate per run: 4.0/0.0/0.0

- Mean wall-clock per investigation: 632.1s
- Mean tokens in/out: 94507.66666666667/1427.6666666666667
- Tokens/sec: 2.4

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
- Confidence-downgrade ablation: any gate fired on 1/3 runs (0 corroboration, 0 budget-exhaustion, 1 from before downgrade_reason was tracked); false-confidence rate WITHOUT any gate (raw self-reported) = 67% vs WITH the gate(s) = 33%

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
- Confidence-downgrade ablation: any gate fired on 0/3 runs (0 corroboration, 0 budget-exhaustion, 0 from before downgrade_reason was tracked); false-confidence rate WITHOUT any gate (raw self-reported) = 67% vs WITH the gate(s) = 67%

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