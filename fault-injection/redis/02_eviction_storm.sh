#!/bin/bash
# Redis fault 2: Eviction storm
# Caps maxmemory very low, then floods the node with writes so it has to
# start evicting keys to stay under the cap.
# Visible via: redis_evicted_keys_total climbing, redis_memory_used_bytes pinned at max.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-redis1}"
LOW_MAXMEMORY="5mb"
FLOOD_KEYS="${FLOOD_KEYS:-200000}"

inject() {
    echo "Capping maxmemory=$LOW_MAXMEMORY, policy=allkeys-lru on $TARGET..."
    redis_cli "$TARGET" CONFIG SET maxmemory "$LOW_MAXMEMORY"
    redis_cli "$TARGET" CONFIG SET maxmemory-policy allkeys-lru

    echo "Flooding $TARGET with $FLOOD_KEYS keys to force eviction..."
    compose exec -T "$TARGET" redis-benchmark --cluster -q -t set -n "$FLOOD_KEYS" -d 200 -r 1000000000

    echo "Done. Check redis_evicted_keys_total in Prometheus."
}

restore() {
    echo "Restoring maxmemory=0, policy=noeviction on $TARGET..."
    redis_cli "$TARGET" CONFIG SET maxmemory 0
    redis_cli "$TARGET" CONFIG SET maxmemory-policy noeviction

    echo "Clearing fault-injected keys on $TARGET..."
    redis_cli "$TARGET" FLUSHALL
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=redis1 FLOOD_KEYS=200000)"; exit 1 ;;
esac
