#!/bin/bash

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_DIR="results_${TIMESTAMP}"
mkdir -p ${RESULTS_DIR}

cat > ${RESULTS_DIR}/payload.json << 'PAYLOAD'
{
  "transaction_id": "txn-{{.RequestNumber}}-{{.TimestampUnix}}",
  "user_id": "{{randomInt 1000 9999}}",
  "amount": {{randomInt 10 100000}}.{{randomInt 10 99}},
  "timestamp": {{.TimestampUnixMilli}},
  "is_fraud": false,
  "type": "PAYMENT",
  "old_balance_orig": {{randomInt 1000 500000}}.{{randomInt 0 99}},
  "new_balance_orig": {{randomInt 0 500000}}.{{randomInt 0 99}},
  "old_balance_dest": {{randomInt 1000 500000}}.{{randomInt 0 99}},
  "new_balance_dest": {{randomInt 0 500000}}.{{randomInt 0 99}},
  "is_unauthorized_overdraft": {{randomInt 0 1}},
  "ip_address": "{{randomInt 1 255}}.{{randomInt 1 255}}.{{randomInt 1 255}}.{{randomInt 1 255}}"
}
PAYLOAD

echo "Results will be saved to: ${RESULTS_DIR}"
echo ""

ghz --insecure --proto ../proto/fraud/v1/fraud.proto --call fraud.FraudIngestion.SendTransaction --data-file ${RESULTS_DIR}/payload.json -c 10 -n 100 localhost:50051 > /dev/null 2>&1

if [ $? -ne 0 ]; then
    echo "ERROR: Server not running on localhost:50051"
    exit 1
fi

echo "[1/5] Testing 1K requests..."
ghz --insecure --proto ../proto/fraud/v1/fraud.proto --call fraud.FraudIngestion.SendTransaction --data-file ${RESULTS_DIR}/payload.json -c 20 -n 1000 -o ${RESULTS_DIR}/1k.json --format json localhost:50051 > /dev/null
sleep 10

echo "[2/5] Testing 10K requests..."
ghz --insecure --proto ../proto/fraud/v1/fraud.proto --call fraud.FraudIngestion.SendTransaction --data-file ${RESULTS_DIR}/payload.json -c 50 -n 10000 -o ${RESULTS_DIR}/10k.json --format json localhost:50051 > /dev/null
sleep 15

echo "[3/5] Testing 50K requests..."
ghz --insecure --proto ../proto/fraud/v1/fraud.proto --call fraud.FraudIngestion.SendTransaction --data-file ${RESULTS_DIR}/payload.json -c 100 -n 50000 -o ${RESULTS_DIR}/50k.json --format json localhost:50051 > /dev/null
sleep 20

echo "[4/5] Testing 100K requests..."
ghz --insecure --proto ../proto/fraud/v1/fraud.proto --call fraud.FraudIngestion.SendTransaction --data-file ${RESULTS_DIR}/payload.json -c 100 -n 100000 -o ${RESULTS_DIR}/100k.json --format json localhost:50051 > /dev/null
sleep 30

echo "[5/5] Testing sustained 60s..."
ghz --insecure --proto ../proto/fraud/v1/fraud.proto --call fraud.FraudIngestion.SendTransaction --data-file ${RESULTS_DIR}/payload.json -c 50 --duration 60s -o ${RESULTS_DIR}/sustained.json --format json localhost:50051 > /dev/null

echo ""
echo "All tests completed! Results in: ${RESULTS_DIR}"
echo "Run: ./analyze.sh ${RESULTS_DIR}"
