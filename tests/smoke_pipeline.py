"""Controlled gRPC -> Kafka -> enrichment smoke check.

Requires grpcio, a healthy Compose stack and a stopped synthetic producer.
Works whether the output topic has one or several partitions.
"""

import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import grpc

from experiments.stack import isolated_env
from proto.fraud.v1 import fraud_pb2, fraud_pb2_grpc

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.yaml")]


def compose(env, *args, timeout=30):
    return subprocess.run([*COMPOSE, *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, check=True, timeout=timeout)


def offsets(env, topic):
    topics = set(compose(env, "exec", "-T", "kafka", "kafka-topics",
                         "--bootstrap-server", "kafka:29092", "--list").stdout.splitlines())
    if topic not in topics:
        return {}
    output = compose(env, "exec", "-T", "kafka", "kafka-get-offsets",
                     "--bootstrap-server", "kafka:29092", "--topic", topic,
                     "--time", "-1").stdout
    result = {}
    for line in output.splitlines():
        name, partition, end = line.rsplit(":", 2)
        if name == topic:
            result[int(partition)] = int(end)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-stack", action="store_true")
    args = parser.parse_args()
    env = isolated_env() if args.experiment_stack else dict(os.environ)
    grpc_address = f"localhost:{env.get('GRPC_HOST_PORT', '50051')}"
    topic = "enriched_transactions"
    before = offsets(env, topic)
    transaction_id = "smoke-" + uuid.uuid4().hex
    ip_hex = uuid.uuid4().hex[:24]
    test_ip = "2001:db8:" + ":".join(ip_hex[i:i + 4] for i in range(0, 24, 4))
    request = fraud_pb2.TransactionRequest(
        transaction_id=transaction_id, user_id="smoke-user-" + uuid.uuid4().hex,
        amount=100, timestamp=int(time.time() * 1000), ip_address=test_ip, type="PAYMENT",
    )
    with grpc.insecure_channel(grpc_address) as channel:
        stub = fraud_pb2_grpc.FraudIngestionStub(channel)
        for _ in range(2):
            response = stub.SendTransaction(request, timeout=20)
            if not response.success:
                raise AssertionError(f"ingestion rejected request: {response.message}")
        invalid = fraud_pb2.TransactionRequest(
            transaction_id="invalid-" + uuid.uuid4().hex, user_id="smoke-user-invalid",
            amount=1, timestamp=int(time.time() * 1000), ip_address="not-an-ip", type="PAYMENT",
        )
        try:
            stub.SendTransaction(invalid, timeout=5)
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.INVALID_ARGUMENT:
                raise AssertionError(f"unexpected invalid-input status: {error.code()}") from error
        else:
            raise AssertionError("invalid IP was accepted")

    deadline = time.monotonic() + 40
    after = offsets(env, topic)
    while sum(end - before.get(partition, 0) for partition, end in after.items()) < 2:
        if time.monotonic() >= deadline:
            raise AssertionError("two enriched records were not delivered within 40 seconds")
        time.sleep(1)
        after = offsets(env, topic)
    records = []
    for partition, end in sorted(after.items()):
        count = end - before.get(partition, 0)
        if count == 0:
            continue
        output = compose(env, "exec", "-T", "kafka", "kafka-console-consumer",
                         "--bootstrap-server", "kafka:29092", "--topic", topic,
                         "--partition", str(partition), "--offset", str(before.get(partition, 0)),
                         "--timeout-ms", "30000", "--max-messages", str(count), timeout=40).stdout
        records.extend(json.loads(line) for line in output.splitlines() if line.startswith("{"))
    matching = [record for record in records if record.get("transaction_id") == transaction_id]
    if len(matching) != 2 or any(record["txn_count_2h"] != 1 for record in matching):
        raise AssertionError(f"duplicate reconciliation failed: {matching}")
    print(json.dumps({"status": "passed", "transaction_id": transaction_id,
                      "outputs": len(matching), "txn_count_2h": 1,
                      "invalid_ip_rejected": True}))


if __name__ == "__main__":
    main()
