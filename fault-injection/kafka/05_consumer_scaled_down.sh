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
BURST_COUNT="${BURST_COUNT:-60000}"
BURST_CONCURRENCY="${BURST_CONCURRENCY:-200}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

inject() {
    echo "Ensuring go-enricher is at normal capacity ($NORMAL_REPLICAS replicas)..."
    compose up -d --scale go-enricher="$NORMAL_REPLICAS" --no-recreate go-enricher
    echo "Waiting for the group to settle..."
    sleep 15

    echo "Scaling go-enricher down to $FAULT_REPLICAS replica(s)..."
    compose up -d --scale go-enricher="$FAULT_REPLICAS" --no-recreate go-enricher

    # Ambient python-producer traffic (~5 tx/s) is nowhere near enough for a
    # single consumer to fall behind on. A sequential burst (one blocking
    # call at a time) doesn't fix this either - the sender itself becomes
    # the bottleneck, never outpacing even a single consumer. ghz fires
    # $BURST_CONCURRENCY requests concurrently (same tool already used in
    # load-tests/), so arrival rate genuinely exceeds what one enricher
    # instance can drain. Random user_id per request spreads load across
    # all 3 partitions.
    echo "Firing $BURST_COUNT concurrent requests (concurrency=$BURST_CONCURRENCY) to actually stress the lone consumer..."
    PAYLOAD_FILE=$(mktemp)
    cat > "$PAYLOAD_FILE" << 'PAYLOAD'
{
  "transaction_id": "txn-{{.RequestNumber}}-{{.TimestampUnix}}",
  "user_id": "{{randomInt 1000 999999}}",
  "amount": {{randomInt 10 100000}}.{{randomInt 10 99}},
  "timestamp": {{.TimestampUnixMilli}},
  "is_fraud": false,
  "type": "PAYMENT",
  "old_balance_orig": {{randomInt 1000 500000}}.{{randomInt 0 99}},
  "new_balance_orig": {{randomInt 0 500000}}.{{randomInt 0 99}},
  "old_balance_dest": {{randomInt 1000 500000}}.{{randomInt 0 99}},
  "new_balance_dest": {{randomInt 0 500000}}.{{randomInt 0 99}},
  "is_unauthorized_overdraft": 0,
  "ip_address": "{{randomInt 1 255}}.{{randomInt 1 255}}.{{randomInt 1 255}}.{{randomInt 1 255}}"
}
PAYLOAD
    ghz --insecure --proto "$REPO_ROOT/proto/fraud/v1/fraud.proto" \
        --call fraud.FraudIngestion.SendTransaction \
        --data-file "$PAYLOAD_FILE" \
        -c "$BURST_CONCURRENCY" -n "$BURST_COUNT" \
        localhost:50051 > /dev/null
    rm -f "$PAYLOAD_FILE"
    echo "Done. Watch kafka_consumergroup_lag climb on the abandoned partitions."
}

restore() {
    echo "Scaling go-enricher back up to $NORMAL_REPLICAS replicas..."
    compose up -d --scale go-enricher="$NORMAL_REPLICAS" --no-recreate go-enricher
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>"; exit 1 ;;
esac
