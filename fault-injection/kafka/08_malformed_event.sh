#!/bin/bash
# Kafka fault 8: Malformed event -> deserialization failure
# Publishes a plain garbage string directly onto raw_transactions,
# bypassing the normal gRPC/protobuf path entirely. go-enricher's
# proto.Unmarshal call fails on it.
# Visible via: fraud_enricher_errors_total{reason="unmarshal"} incrementing,
# "Failed to Unmarshal" log line.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

inject() {
    echo "Publishing a malformed (non-protobuf) message to raw_transactions..."
    echo "this-is-not-a-valid-protobuf-payload-$(date +%s)" | compose exec -T kafka kafka-console-producer --broker-list localhost:9092 --topic raw_transactions
    echo "Done. Check fraud_enricher_errors_total{reason=\"unmarshal\"}."
}

restore() {
    echo "No restore needed — the bad message is consumed and dropped, no lasting state."
}

case "${1:-}" in
    inject) log_change inject; inject ;;
    restore) log_change restore; restore ;;
    *) echo "Usage: $0 <inject|restore>"; exit 1 ;;
esac
