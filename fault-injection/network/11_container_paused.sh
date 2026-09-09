#!/bin/bash
# Network/resource fault 11: Container paused
# Freezes the target container's process without killing it — it still
# shows "Up" in `docker ps` but does zero work while paused.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-go-server}"

inject() {
    echo "Pausing $TARGET..."
    compose pause "$TARGET"
}

restore() {
    echo "Unpausing $TARGET..."
    compose unpause "$TARGET"
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=go-server)"; exit 1 ;;
esac
