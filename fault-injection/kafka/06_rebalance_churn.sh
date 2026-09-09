#!/bin/bash
# Kafka fault 6: Repeated consumer kill -> rebalance churn
# Restarts go-enricher in a tight loop so the consumer group is constantly
# leaving and rejoining, forcing Kafka to rebalance partition assignment
# over and over instead of settling.
# Visible via: repeated rebalance activity / processing throughput dropping
# during the churn window (fraud_enricher_messages_processed_total rate dips).
set -euo pipefail
source "$(dirname "$0")/../common.sh"

CYCLES="${CYCLES:-6}"
INTERVAL="${INTERVAL:-5}"

inject() {
    echo "Restarting go-enricher $CYCLES times, ${INTERVAL}s apart..."
    for i in $(seq 1 "$CYCLES"); do
        echo "  Restart $i/$CYCLES..."
        compose restart go-enricher
        sleep "$INTERVAL"
    done
    echo "Done. Check throughput dip during the churn window."
}

restore() {
    echo "No restore needed — go-enricher is already running normally after the last restart."
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: CYCLES=6 INTERVAL=5)"; exit 1 ;;
esac
