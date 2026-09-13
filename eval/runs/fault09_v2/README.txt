v2 tool-docs experiment. NOT part of the graded dataset (grade.py's glob is
fault[0-9][0-9]/, which does not match this directory).

Kept because the fault-9 result is informative: after the get_metrics
docstring was expanded 1259 -> 2389 chars to name the latency histogram the
v1 agent never queried, BOTH legs stopped calling get_metrics entirely
(v1: 6 and 3 calls -> v2: 0 and 0) and spent all 15 calls on query_logs.
Longer tool description, tool abandoned.

Not claimed anywhere: this is one fault / two runs. The v2 arm was abandoned
before faults 13 and 6 could replicate it, so there is no paired A/B.
See eval/runs/fault13_v2/README.txt.
