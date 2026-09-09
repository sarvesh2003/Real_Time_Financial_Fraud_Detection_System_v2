#!/bin/bash
# Network fault 10: Packet loss (tc netem loss)
# Drops a percentage of packets on the target container's interface using
# the same throwaway sidecar approach as fault 9.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-go-server}"
LOSS_PCT="${LOSS_PCT:-10%}"
IFACE="${IFACE:-eth0}"

inject() {
    CID="$(container_id "$TARGET")"
    echo "Adding ${LOSS_PCT} packet loss to $TARGET's $IFACE..."
    docker run --rm --net=container:"$CID" --cap-add=NET_ADMIN nicolaka/netshoot \
        tc qdisc add dev "$IFACE" root netem loss "$LOSS_PCT"
    echo "Done. Check for retries/timeouts and dropped-connection errors."
}

restore() {
    CID="$(container_id "$TARGET")"
    echo "Removing netem loss from $TARGET's $IFACE..."
    docker run --rm --net=container:"$CID" --cap-add=NET_ADMIN nicolaka/netshoot \
        tc qdisc del dev "$IFACE" root netem || echo "(already clean, nothing to remove)"
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=go-server LOSS_PCT=10% IFACE=eth0)"; exit 1 ;;
esac
