#!/bin/bash
# Network/resource fault 12: CPU quota throttle
# Caps the target container's CPU using Docker's native --cpus quota,
# starving it so it can't keep pace with incoming load.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-go-server}"
LOW_CPUS="${LOW_CPUS:-0.1}"

inject() {
    CID="$(container_id "$TARGET")"
    echo "Throttling $TARGET to ${LOW_CPUS} CPUs..."
    docker update --cpus="$LOW_CPUS" "$CID"
}

restore() {
    CID="$(container_id "$TARGET")"
    echo "Removing CPU limit on $TARGET..."
    # `docker update --cpus=0` does NOT reliably clear the limit (confirmed by
    # testing — the higher-level --cpus flag silently no-ops at 0 on this
    # Docker version). The lower-level --cpu-quota=-1 actually works.
    docker update --cpu-quota=-1 "$CID"
}

case "${1:-}" in
    inject) inject ;;
    restore) restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=go-server LOW_CPUS=0.1)"; exit 1 ;;
esac
