#!/bin/bash
# Configuration fault 16: Wrong maxmemory-policy
# Caps maxmemory but sets the WRONG eviction policy for this app's use case
# (noeviction instead of allkeys-lru), so once memory fills up, Redis starts
# REJECTING writes with OOM errors instead of quietly evicting old cache
# entries — a much worse failure mode for a caching workload like this one.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-redis1}"
LOW_MAXMEMORY="5mb"
FLOOD_KEYS="${FLOOD_KEYS:-200000}"

inject() {
    echo "Capping maxmemory=$LOW_MAXMEMORY with the WRONG policy (noeviction) on $TARGET..."
    redis_cli "$TARGET" CONFIG SET maxmemory "$LOW_MAXMEMORY"
    redis_cli "$TARGET" CONFIG SET maxmemory-policy noeviction

    echo "Flooding $TARGET to fill memory and trigger OOM write rejections..."
    compose exec -T "$TARGET" redis-benchmark --cluster -q -t set -n "$FLOOD_KEYS" -d 200 -r 1000000000 || true
    echo "Done. Expect 'OOM command not allowed' errors instead of quiet eviction."
}

restore() {
    # Restore to noeviction, not allkeys-lru - noeviction is this stack's
    # actual baseline (nothing in docker-compose.yaml ever sets a policy, see
    # personal-notes/faults_and_descriptions.md), and leaving $TARGET on
    # allkeys-lru afterward makes it permanently look like fault 2 (eviction
    # storm) is still running to anything reading redis config later.
    echo "Restoring maxmemory=0 and the baseline policy (noeviction) on $TARGET..."
    redis_cli "$TARGET" CONFIG SET maxmemory 0
    redis_cli "$TARGET" CONFIG SET maxmemory-policy noeviction

    # The --cluster flood in inject() distributes writes across all 3 nodes
    # by hash slot, not just $TARGET - flushing only $TARGET leaves residual
    # keys on the other two (the same bug fault 2 had before being fixed).
    echo "Clearing fault-injected keys on redis1, redis2, redis3..."
    redis_cli redis1 FLUSHALL
    redis_cli redis2 FLUSHALL
    redis_cli redis3 FLUSHALL
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=redis1 FLOOD_KEYS=200000)"; exit 1 ;;
esac
