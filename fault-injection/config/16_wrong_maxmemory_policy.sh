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
    echo "Restoring maxmemory=0 and the CORRECT policy (allkeys-lru) on $TARGET..."
    redis_cli "$TARGET" CONFIG SET maxmemory 0
    redis_cli "$TARGET" CONFIG SET maxmemory-policy allkeys-lru
    redis_cli "$TARGET" FLUSHALL
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=redis1 FLOOD_KEYS=200000)"; exit 1 ;;
esac
