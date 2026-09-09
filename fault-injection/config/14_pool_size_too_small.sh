#!/bin/bash
# Configuration fault 14: Connection pool size too small
# Caps the Redis client's connection pool far below what concurrent load
# needs, so requests queue up waiting for a free connection.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-go-server}"
LOW_POOL_SIZE="${LOW_POOL_SIZE:-2}"

inject() {
    echo "Setting Redis pool size to $LOW_POOL_SIZE on $TARGET..."
    export REDIS_POOL_SIZE="$LOW_POOL_SIZE"
    compose up -d --force-recreate "$TARGET"
    echo "Done. Send concurrent load to see pool-wait timeouts."
}

restore() {
    echo "Clearing pool size override on $TARGET (back to library default)..."
    unset REDIS_POOL_SIZE
    compose up -d --force-recreate "$TARGET"
}

case "${1:-}" in
    inject) inject ;;
    restore) restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=go-server LOW_POOL_SIZE=2)"; exit 1 ;;
esac
