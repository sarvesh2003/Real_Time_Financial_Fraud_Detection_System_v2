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
    echo "Recreating $TARGET to fully clear the CPU cap..."
    # Neither `docker update --cpu-quota=-1` nor `docker update --cpus=0`
    # actually clears `HostConfig.NanoCpus` once inject() has set it —
    # confirmed by direct testing (docker inspect kept showing the injected
    # NanoCpus value after both). Docker's update only overwrites a field
    # when the incoming value is non-zero, so the cap silently survives any
    # `docker update`-based restore and comes back on the container's next
    # unrelated restart. Recreating from the compose file is the only
    # reliable way to actually clear it — same cost as a normal restart.
    compose up -d --force-recreate -- "$TARGET"
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=go-server LOW_CPUS=0.1)"; exit 1 ;;
esac
