#!/bin/bash
# Redis fault 1: Node stopped
# Kills one Redis cluster node outright, like a hardware/process failure.
# Visible via: redis_up (per-instance) drops to 0, cluster_state may degrade.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-redis2}"

inject() {
    echo "Stopping $TARGET..."
    compose stop "$TARGET"
}

restore() {
    echo "Starting $TARGET (rejoins cluster automatically, container filesystem persisted)..."
    compose start "$TARGET"
}

case "${1:-}" in
    inject) inject ;;
    restore) restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=redis2 env var)"; exit 1 ;;
esac
