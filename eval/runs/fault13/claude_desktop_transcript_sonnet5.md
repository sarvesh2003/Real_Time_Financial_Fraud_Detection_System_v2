# Fault 13 (config/client_timeout_too_low) — Claude Desktop, Sonnet 5 Max

Trigger sent: "Something seems off with the fraud pipeline, can you investigate?"
Injected fresh (confirmed-healthy baseline, rotated changelog) at fault-13
injection time (see eval/runs/fault13/inject.log).

Note: narrated response text as provided by the user, not a raw export with
explicit tool-call blocks — see `_grading_note` in the matching result.json.

---

Found it. This isn't a self-healing blip — there's a live, active misconfiguration on `go-server`.

**Root cause:** `go-server`'s Redis client timeouts were slashed to 1 millisecond:
```
REDIS_READ_TIMEOUT_MS=1
REDIS_WRITE_TIMEOUT_MS=1
REDIS_DIAL_TIMEOUT_MS=1
```
For comparison, `go-enricher` — which talks to the exact same Redis cluster — has these unset (library defaults, normally hundreds of ms+). So this isn't a stack-wide policy, it's a single service getting nearly-instant timeouts on every Redis round trip.

**Timeline:**
- `list_recent_changes` shows exactly one active fault, injected (not yet restored) — change `CHG-909e1848`.
- 33 seconds later, `go-server` was recreated to pick up the new env vars (its `redis-cluster-setup` sidecar re-ran alongside it, exited cleanly).
- On boot, the Redis cluster client immediately hit an i/o timeout fetching `COMMAND INFO` from one cluster node — a call that would essentially never fail under a sane timeout. That's the fault announcing itself.

**Why it hasn't fully fallen over (yet):** Since restart, transaction throughput has been steady (~4/sec) and error counters (`redis_dedup`, `rate_limit_error`) haven't ticked up — this Docker network is fast enough that most calls are sneaking in under 1ms. But there's zero margin for error: any GC pause, retry, or load bump will push real calls over that threshold, and every one of those failures lands on the *dedup check* — meaning duplicate fraud transactions could start slipping through uncaught, or getting spuriously errored. It's a latent landmine more than a current outage.

**Ruled out along the way:**
- Redis cluster itself is healthy — 3/3 nodes, 0 failed/pfail slots, no evictions, stable memory (~11.5MB).
- Kafka consumer lag is small and flat (single digits) — `go-enricher` isn't falling behind.
- `go-enricher`'s restart (24m prior) and `redis1`/`redis2`'s restarts (4h/7h prior) all predate this injection — leftovers from an earlier test cycle, not part of this incident.
- All 3 Redis nodes have identical, sane server-side configs (maxmemory, eviction policy, etc.) — the problem is purely the client-side env vars on `go-server`.

**Fix:** Restore `REDIS_READ_TIMEOUT_MS` / `REDIS_WRITE_TIMEOUT_MS` / `REDIS_DIAL_TIMEOUT_MS` on `go-server` to sane values (or trigger the harness's restore for `CHG-909e1848` if this is a managed exercise) and let it recreate the container.

One unrelated thing I noticed while in the logs: every "Received & Pushed" line has a `%!d(string=...)` formatting artifact — a Go `fmt` verb mismatch (`%d` used on a string). Cosmetic, pre-existing, and unrelated to this fault, but worth a cleanup ticket.
