# Fault-injection scripts

Each script takes `inject` or `restore` and drives the stack through `docker compose` (see `common.sh`). Scripts inherit `COMPOSE_PROJECT_NAME` and port settings from the caller, so the experiment runner aims them at its isolated stack.

## Supported in V2

Only these four are registered in `experiments/runner.py` `CASES`; each is sustained until `restore` and has a live-state probe:

| Case | Script | Mechanism |
| --- | --- | --- |
| `redis_node_stopped` | `redis/01_node_stopped.sh` | `compose stop redis2` |
| `ingestion_paused` | `network/11_container_paused.sh` | `compose pause go-server` |
| `broker_stopped` | `kafka/17_broker_stopped.sh` | `compose stop kafka` |
| `enricher_paused` | `network/18_enricher_paused.sh` | `compose pause go-enricher` |

## Historical (kept unchanged, not supported)

The other numbered scripts belong to the historical evaluation in `eval/`, which reads these files as its ground truth; they are therefore left byte-for-byte as collected. Known problems with them:

- `redis/02_eviction_storm.sh` and `config/16_wrong_maxmemory_policy.sh` restore with `FLUSHALL`, which deletes application state; never run them against a stack whose data matters.
- `kafka/06_rebalance_churn.sh` and `redis/04_slow_commands.sh` finish their effect before the injector returns, so the fault may be over before an agent looks.
- `config/15_bad_retry_backoff.sh` sets the retry backoff to `0` ms. With go-redis v9.22.0, `ClusterOptions.init` treats `0` as "use the default" (10 ms minimum, 1 s maximum); only `-1` disables backoff, and `internal/redisconfig` rejects negative values. The script therefore does **not** create zero backoff. Its restore message ("3 retries, 8ms-512ms") also does not describe this library's cluster defaults.
- Several restores reapply hard-coded defaults rather than a captured prior configuration.

`changelog.jsonl` is a local audit log written by `log_change`; it is git-ignored and not shown to the agent.
