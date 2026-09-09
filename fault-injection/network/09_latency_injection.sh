#!/bin/bash
# Network fault 9: Latency injection (tc netem delay on gRPC)
# Adds artificial network delay to the target container's interface using a
# throwaway sidecar (nicolaka/netshoot) that joins its network namespace —
# no changes to the app image, no permanent NET_ADMIN on the app container.
# This is the fault catalog's "highest-value" fault: it produces *no error
# lines anywhere*. The only signal is a shift in the latency histogram.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

TARGET="${TARGET:-go-server}"
DELAY="${DELAY:-200ms}"
IFACE="${IFACE:-eth0}"

inject() {
    CID="$(container_id "$TARGET")"
    echo "Adding ${DELAY} delay to $TARGET's $IFACE..."
    docker run --rm --net=container:"$CID" --cap-add=NET_ADMIN nicolaka/netshoot \
        tc qdisc add dev "$IFACE" root netem delay "$DELAY"
    echo "Done. Check the app's own latency histogram — no error logs expected."
}

restore() {
    CID="$(container_id "$TARGET")"
    echo "Removing netem delay from $TARGET's $IFACE..."
    docker run --rm --net=container:"$CID" --cap-add=NET_ADMIN nicolaka/netshoot \
        tc qdisc del dev "$IFACE" root netem || echo "(already clean, nothing to remove)"
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: TARGET=go-server DELAY=200ms IFACE=eth0)"; exit 1 ;;
esac
