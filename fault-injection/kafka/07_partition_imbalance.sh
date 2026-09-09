#!/bin/bash
# Kafka fault 7: Partition imbalance (skewed keys)
# Sends a burst of transactions all for the SAME user_id. Messages are
# keyed by user_id (hash(user_id) % partitions), so every one of these
# lands on the same single partition, spiking it while the others idle.
# Visible via: partition offset / consumer lag growth skewed heavily
# toward one partition while the others stay flat.
set -euo pipefail
source "$(dirname "$0")/../common.sh"

BURST_USER="${BURST_USER:-fault-burst-user}"
BURST_COUNT="${BURST_COUNT:-500}"

inject() {
    echo "Sending $BURST_COUNT transactions for a single user ($BURST_USER)..."
    compose exec -T python-producer python3 -c "
import time, uuid, grpc
from proto.fraud.v1 import fraud_pb2, fraud_pb2_grpc

channel = grpc.insecure_channel('go-server:50051')
stub = fraud_pb2_grpc.FraudIngestionStub(channel)

for i in range($BURST_COUNT):
    req = fraud_pb2.TransactionRequest(
        transaction_id=str(uuid.uuid4()),
        user_id='$BURST_USER',
        amount=100.0,
        timestamp=int(time.time() * 1000),
        is_fraud=False,
        type='PAYMENT',
        old_balance_orig=1000.0,
        new_balance_orig=900.0,
        old_balance_dest=0.0,
        new_balance_dest=0.0,
        is_unauthorized_overdraft=0.0,
        ip_address='8.8.8.8',
    )
    try:
        stub.SendTransaction(req)
    except grpc.RpcError:
        pass
print('Burst done.')
"
    echo "Done. Check per-partition offsets/lag for skew."
}

restore() {
    echo "No restore needed — this only sends extra traffic, no state to undo."
}

case "${1:-}" in
    inject) inject ;;
    restore) restore ;;
    *) echo "Usage: $0 <inject|restore>  (optional: BURST_USER=... BURST_COUNT=500)"; exit 1 ;;
esac
