#!/bin/bash
# Redis fault 4: Slow commands
# Uses Redis's built-in DEBUG SLEEP to block the single-threaded event loop
# for N seconds, standing in for a slow blocking command (e.g. a big KEYS
# scan) hogging the node while everything else queues behind it.
# Visible via: latency histogram shift, no error logs anywhere (mirrors what
# a real slow-command incident looks like).
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-redis1}"
DURATION="${DURATION:-10}"

inject() {
    echo "Blocking $TARGET's event loop for ${DURATION}s via DEBUG SLEEP..."
    redis_cli "$TARGET" DEBUG SLEEP "$DURATION"
    echo "Done."
}

restore() {
    echo "No restore needed — DEBUG SLEEP is self-terminating."
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=redis1 DURATION=10)"; exit 1 ;;
esac
