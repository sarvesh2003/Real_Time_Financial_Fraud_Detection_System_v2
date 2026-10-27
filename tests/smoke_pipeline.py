"""Controlled transaction smoke check on the isolated experiment stack.

Sends one valid request twice (an at-least-once replay), one request with an
invalid IP, and one raw Kafka record that is not protobuf. Expects: two
acknowledgments; INVALID_ARGUMENT with no raw record; two enriched records
for the repeated ID whose Redis window counted it once; and one DLQ record
with reason invalid_protobuf that preserves the raw bytes.

Requires grpcio, a healthy stack from python -m experiments.stack up, and a
stopped python-producer. EXPERIMENT_STACK_FILE selects a throwaway project.
Run: agent\\venv\\Scripts\\python.exe -m tests.smoke_pipeline
"""

import base64
import json
import time
import uuid

import grpc

from experiments.stack import all_offsets, compose, isolated_env, project, read_json_records, records_added
from proto.fraud.v1 import fraud_pb2, fraud_pb2_grpc

POISON = b"not-a-protobuf-" + uuid.uuid4().hex.encode()


def wait_for(before: dict, minimum: dict, seconds: int = 60) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        after = all_offsets()
        if all(records_added(before[topic], after[topic]) >= count for topic, count in minimum.items()):
            return after
        if time.monotonic() >= deadline:
            raise AssertionError(f"records did not arrive within {seconds}s: before={before} after={after}")
        time.sleep(1)


def main():
    if compose("ps", "--status", "running", "-q", "python-producer", check=True).stdout.strip():
        raise AssertionError("stop python-producer before the smoke check")
    before = all_offsets()
    transaction_id = "smoke-" + uuid.uuid4().hex
    ip_hex = uuid.uuid4().hex[:24]
    request = fraud_pb2.TransactionRequest(
        transaction_id=transaction_id, user_id="smoke-user-" + uuid.uuid4().hex, amount=100,
        timestamp=int(time.time() * 1000), type="PAYMENT",
        ip_address="2001:db8:" + ":".join(ip_hex[i:i + 4] for i in range(0, 24, 4)),
    )
    with grpc.insecure_channel(f"localhost:{isolated_env()['GRPC_HOST_PORT']}") as channel:
        stub = fraud_pb2_grpc.FraudIngestionStub(channel)
        for _ in range(2):
            if not stub.SendTransaction(request, timeout=20).success:
                raise AssertionError("ingestion rejected the valid request")
        invalid = fraud_pb2.TransactionRequest(transaction_id="invalid-" + uuid.uuid4().hex, user_id="smoke-user",
                                               amount=1, ip_address="not-an-ip", type="PAYMENT")
        try:
            stub.SendTransaction(invalid, timeout=5)
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.INVALID_ARGUMENT:
                raise AssertionError(f"unexpected invalid-input status: {error.code()}") from error
        else:
            raise AssertionError("invalid IP was accepted")

    compose("exec", "-T", "kafka", "kafka-console-producer", "--bootstrap-server", "kafka:29092",
            "--topic", "raw_transactions", input=POISON.decode() + "\n", check=True)
    after = wait_for(before, {"enriched_transactions": 2, "raw_transactions_dlq": 1})

    raw_added = records_added(before["raw_transactions"], after["raw_transactions"])
    if raw_added != 3:  # two valid requests and the poison record; the invalid IP never reached Kafka
        raise AssertionError(f"expected 3 raw records, found {raw_added}")
    outputs = [r for r in read_json_records("enriched_transactions", before["enriched_transactions"],
                                            after["enriched_transactions"]) if r.get("transaction_id") == transaction_id]
    if len(outputs) != 2 or any(r["txn_count_2h"] != 1 for r in outputs):
        raise AssertionError(f"duplicate reconciliation failed: {outputs}")
    dead_letters = read_json_records("raw_transactions_dlq", before["raw_transactions_dlq"], after["raw_transactions_dlq"])
    poison = [r for r in dead_letters if base64.b64decode(r["payload_base64"]) == POISON]
    if len(poison) != 1 or poison[0]["reason"] != "invalid_protobuf" or poison[0]["source_topic"] != "raw_transactions":
        raise AssertionError(f"poison record not dead-lettered as expected: {dead_letters}")
    print(json.dumps({"status": "passed", "compose_project": project(), "transaction_id": transaction_id,
                      "enriched_outputs_for_repeated_id": 2, "txn_count_2h": 1, "invalid_ip_rejected": True,
                      "raw_records_added": raw_added, "poison_dead_lettered": True,
                      "geoip_city_for_test_ip": outputs[0]["city"]}))


if __name__ == "__main__":
    main()
