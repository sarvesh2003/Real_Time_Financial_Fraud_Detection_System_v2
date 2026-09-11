# Fault 9 (network/latency_injection) — Claude Desktop, Sonnet 5 Max

Trigger sent: "Something seems off with the fraud pipeline, can you investigate?"

Note: this is the narrated response text as provided by the user, not a raw
export including explicit tool-call blocks (tool name/args/result shown
separately in the Claude Desktop UI). Exact tool-call counts could not be
verified from this text alone — see `_grading_note` in the matching
result.json.

---

Found it — this isn't an organic outage, it's an active fault injection: change `CHG-346b4524`, injected at 2026-09-11T02:27 UTC (~15 minutes ago), no restore logged yet.

**What's happening:** go-server's per-transaction processing time jumped from ~1.2ms baseline to a sustained ~600ms right at that timestamp (~500x) and has held flat there since — not worsening, not recovering. Ingest throughput fell from ~4.2 tx/sec to ~1 tx/sec (down ~77%). go-enricher's own message-processing time is untouched (still ~1-2ms) — it's just starved, since it receives far less volume through Kafka now that go-server is the bottleneck.

**Where it's landing:** Everything points at go-server's per-transaction Redis call — the dedup check (I can see `sismember`/`sadd`/`evalsha` traffic consistent with an atomic dedup script). But Redis itself checks out clean:
- Cluster is healthy — 3/3 nodes, 16384/16384 slots ok, nothing marked fail/pfail
- redis1, redis2, redis3 have identical configs (maxmemory, eviction policy, maxclients, timeout, appendonly) — no drift
- Server-side command latency for get/set/sadd/sismember/evalsha is still microseconds — completely unaffected

So the ~600ms isn't Redis executing slowly, it's something adding delay in transit between go-server and Redis — a network-latency injection, not a Redis-side resource or config issue.

I also ruled out the usual suspects on the go-server side: goroutine count is flat (12-14, no leak or pileup), and its CPU-seconds usage actually tracked *down* as throughput dropped rather than pegging a ceiling — the signature of a process sitting blocked on a slow call, not one that's CPU-throttled or stuck in a hot loop. Kafka consumer lag stayed flat and negligible throughout, so nothing's backing up there. And nothing is erroring — the `redis_dedup` and `rate_limit_error` failure-reason counters are flat at zero the whole time; it's not failing, just running ~500x slower on that one hop.

One red herring to ignore if you look further back: redis1 also shows a ~24-minute down blip about 3 hours before this — that's a separate, already-resolved incident, unrelated to what's happening right now.

**Fix:** restore change `CHG-346b4524`. I can only read state, not trigger the restore, so that part needs to happen on your end (or whoever owns the fault-injection harness). Let me know once it's back and I'll re-pull the metrics to confirm it cleared.
