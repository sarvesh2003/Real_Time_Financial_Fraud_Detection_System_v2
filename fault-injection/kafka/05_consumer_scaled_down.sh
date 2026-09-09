#!/bin/bash
# Kafka fault 5: Consumer scaled 3 -> 1
# Ensures go-enricher is running at normal operating capacity (3 replicas),
# then scales it down to 1 — the remaining instance must pick up the
# abandoned partitions' backlog on top of its own.
# Visible via: kafka_consumergroup_lag climbing on the partitions the
# removed replicas used to own.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

NORMAL_REPLICAS=3
FAULT_REPLICAS=1

inject() {
    echo "Ensuring go-enricher is at normal capacity ($NORMAL_REPLICAS replicas)..."
    compose up -d --scale go-enricher="$NORMAL_REPLICAS" --no-recreate go-enricher
    echo "Waiting for the group to settle..."
    sleep 15

    echo "Scaling go-enricher down to $FAULT_REPLICAS replica(s)..."
    compose up -d --scale go-enricher="$FAULT_REPLICAS" --no-recreate go-enricher
    echo "Done. Watch kafka_consumergroup_lag climb on the abandoned partitions."
}

restore() {
    echo "Scaling go-enricher back up to $NORMAL_REPLICAS replicas..."
    compose up -d --scale go-enricher="$NORMAL_REPLICAS" --no-recreate go-enricher
}

case "${1:-}" in
    inject) inject ;;
    restore) restore ;;
    *) echo "Usage: $0 <inject|restore>"; exit 1 ;;
esac
