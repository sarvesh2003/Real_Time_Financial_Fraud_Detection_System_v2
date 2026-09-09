#!/bin/bash
# Configuration fault 13: Client timeout set too low
# Sets the Redis client's dial/read/write timeouts far below what a real
# round trip needs, so the client gives up before Redis can even respond —
# even though Redis itself is completely healthy.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-go-server}"
LOW_TIMEOUT_MS="${LOW_TIMEOUT_MS:-1}"

inject() {
    echo "Setting Redis client timeouts to ${LOW_TIMEOUT_MS}ms on $TARGET..."
    export REDIS_DIAL_TIMEOUT_MS="$LOW_TIMEOUT_MS"
    export REDIS_READ_TIMEOUT_MS="$LOW_TIMEOUT_MS"
    export REDIS_WRITE_TIMEOUT_MS="$LOW_TIMEOUT_MS"
    compose up -d --force-recreate "$TARGET"
    echo "Done. Redis itself is healthy — errors are purely client-side timeouts."
}

restore() {
    echo "Clearing timeout overrides on $TARGET (back to library defaults)..."
    unset REDIS_DIAL_TIMEOUT_MS REDIS_READ_TIMEOUT_MS REDIS_WRITE_TIMEOUT_MS
    compose up -d --force-recreate "$TARGET"
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=go-server LOW_TIMEOUT_MS=1)"; exit 1 ;;
esac
