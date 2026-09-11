# Fault 6 (kafka/rebalance_churn) — Claude Desktop, Sonnet 5 Max

Trigger sent: "Something seems off with the fraud pipeline, can you investigate?"
Injected fresh (clean changelog, confirmed-healthy baseline) at 2026-09-11T03:27:35Z.

Note: narrated response text as provided by the user, not a raw export with
explicit tool-call blocks — see `_grading_note` in the matching result.json.
IMPORTANT: this run's conclusion is contamination-affected — see
`_grading_note` for the full explanation before using this row in any
aggregate metric. The model's tool use and reasoning process were rigorous;
the wrong conclusion traces to real leftover restart-history in Prometheus
from repeated same-session fault-6 injection attempts (02:54, 03:03, ~03:23,
03:27:35), not a reasoning failure on a clean instance of this fault.

---

Found something. Let me pin down the exact timing with a quick local calculation, then dig into why go-enricher keeps restarting.This is looking like a memory leak in go-enricher, and it lines up suspiciously well with a jump in transaction volume. Let me confirm both threads — Kafka/Redis health, and whether the traffic pattern really shifted when the trouble started.Found it — this is a memory leak in `go-enricher`, not a config problem or a Redis issue.

Since about 02:54 UTC (~45 minutes ago), all three `go-enricher` replicas have been crash-looping in lockstep: each one's memory climbs for 10-20 minutes, then it gets killed and comes back near zero, over and over. Before that they'd been stable for hours, restarting only every 3-4 hours — that cadence change is the "something's off" you noticed.

What's driving it, from the logs and metrics:

- The enricher never logs a panic or fatal error before a restart — it just goes dark and comes back as a fresh process. That's the signature of an OOM kill, not a handled crash.
- The logs show the Redis-cache-hit debug line (`FOUND IN REDIS CACHE... sample data: city=X`) getting mashed together — a dozen-plus repeats concatenated onto one line before a city name finally prints. That looks like a buffer or string being appended to across multiple cache lookups without being reset, which would leak memory roughly in proportion to transaction volume.
- Consistent with that: throughput on `raw_transactions` jumped roughly 4x (from ~1/s to ~4/s) at almost exactly 02:54 UTC — the same moment the crash loop started. More volume would fill a per-transaction leak much faster, which fits restarts going from every few hours to every 10-20 minutes.

Ruled out: Redis is healthy throughout (up, no evictions, no rejected connections, no failed commands, memory flat and low), so it's not a Redis-side problem. `go-server`'s error counters are flat and its transaction counter climbs smoothly — it's not crashing, just cleanly upstream of this. There's also one deliberate fault injection on record at 03:27:35 UTC, but that's over 30 minutes after the crash loop began, so it isn't the trigger — worth a separate check with whoever owns that test.

Impact so far: each restart causes a brief Kafka consumer-group lag spike (a few hundred messages on `raw_transactions`) that drains again within a minute or two — periodic reprocessing/latency blips rather than a runaway backlog, at least for now.

As of my last check, all three replicas had gone 10 minutes without restarting, so this may just be between cycles rather than resolved. I'd point whoever owns the enrichment code at that Redis-cache-hit logging path first — it's the most concrete lead for where the leak lives.
