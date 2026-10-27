"""Reconcile a controlled gRPC batch with Kafka input and output records.

Use on a fresh isolated experiment stack with python-producer stopped. The
result is a correctness artifact, not a sustained throughput benchmark.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import grpc

from experiments.stack import ROOT, isolated_env
from proto.fraud.v1 import fraud_pb2, fraud_pb2_grpc

TOPICS = ("raw_transactions", "enriched_transactions", "raw_transactions_dlq")


def compose(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yaml"), *args],
                          cwd=ROOT, env=isolated_env(), text=True, capture_output=True,
                          timeout=timeout, encoding="utf-8", errors="replace", check=True)


def offsets(topic: str) -> dict[int, int]:
    topics = set(compose("exec", "-T", "kafka", "kafka-topics", "--bootstrap-server",
                         "kafka:29092", "--list").stdout.splitlines())
    if topic not in topics:
        return {}
    output = compose("exec", "-T", "kafka", "kafka-get-offsets", "--bootstrap-server",
                     "kafka:29092", "--topic", topic, "--time", "-1").stdout
    result = {}
    for line in output.splitlines():
        name, partition, offset = line.rsplit(":", 2)
        if name == topic:
            result[int(partition)] = int(offset)
    return result


def all_offsets() -> dict[str, dict[int, int]]:
    return {topic: offsets(topic) for topic in TOPICS}


def delta(before: dict[int, int], after: dict[int, int]) -> int:
    return sum(value - before.get(partition, 0) for partition, value in after.items())


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 3)


def consume_output(before: dict[int, int], after: dict[int, int]) -> list[dict]:
    records = []
    for partition, end in sorted(after.items()):
        count = end - before.get(partition, 0)
        if count <= 0:
            continue
        output = compose("exec", "-T", "kafka", "kafka-console-consumer",
                         "--bootstrap-server", "kafka:29092", "--topic", "enriched_transactions",
                         "--partition", str(partition), "--offset", str(before.get(partition, 0)),
                         "--max-messages", str(count), "--timeout-ms", "30000", timeout=45).stdout
        parsed = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        if len(parsed) != count:
            raise RuntimeError(f"partition {partition}: expected {count} output records, got {len(parsed)}")
        records.extend(parsed)
    return records


def run_probe(count: int, workers: int, wait_seconds: int) -> dict:
    if count < 1 or workers < 1:
        raise ValueError("count and workers must be positive")
    running_producer = compose("ps", "--status", "running", "-q", "python-producer").stdout.strip()
    if running_producer:
        raise RuntimeError("stop python-producer before a controlled reconciliation")
    before = all_offsets()
    if any(sum(partitions.values()) for partitions in before.values()):
        raise RuntimeError(f"probe requires empty Kafka topics; current offsets: {before}")

    probe_id = uuid.uuid4().hex
    ids = [f"probe-{probe_id}-{i}" for i in range(count)]
    requests = [fraud_pb2.TransactionRequest(
        transaction_id=transaction_id, user_id=f"probe-user-{i % 10}",
        amount=100 + i % 50, timestamp=int(time.time() * 1000),
        ip_address=f"2001:db8::{i % 10 + 1}", type="PAYMENT",
    ) for i, transaction_id in enumerate(ids)]
    latencies_ms = []
    failures = []
    started = time.perf_counter()
    with grpc.insecure_channel("localhost:15051") as channel:
        stub = fraud_pb2_grpc.FraudIngestionStub(channel)
        def send(request):
            call_start = time.perf_counter()
            response = stub.SendTransaction(request, timeout=20)
            return request.transaction_id, response.success, (time.perf_counter() - call_start) * 1000
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(send, request) for request in requests]
            for future in as_completed(futures):
                try:
                    transaction_id, success, latency = future.result()
                    if success:
                        latencies_ms.append(latency)
                    else:
                        failures.append(transaction_id)
                except Exception as error:
                    failures.append(f"{type(error).__name__}: {error}")
    acknowledgments_finished = time.perf_counter()
    expected = len(latencies_ms)
    deadline = time.monotonic() + wait_seconds
    after = all_offsets()
    while time.monotonic() < deadline and delta(before["enriched_transactions"], after["enriched_transactions"]) < expected:
        time.sleep(1)
        after = all_offsets()
    records = consume_output(before["enriched_transactions"], after["enriched_transactions"])
    output_ids = Counter(record.get("transaction_id") for record in records)
    expected_ids = set(ids) - set(failures)
    missing = sorted(expected_ids - output_ids.keys())
    unexpected = sorted(output_ids.keys() - expected_ids)
    duplicates = sorted(transaction_id for transaction_id, seen in output_ids.items() if seen > 1)
    raw_delta = delta(before["raw_transactions"], after["raw_transactions"])
    enriched_delta = delta(before["enriched_transactions"], after["enriched_transactions"])
    dead_letter_delta = delta(before["raw_transactions_dlq"], after["raw_transactions_dlq"])
    passed = not failures and raw_delta == count and enriched_delta == count and dead_letter_delta == 0 and not missing and not unexpected and not duplicates
    return {
        "schema_version": "2.0", "probe_id": probe_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if passed else "failed", "requested": count,
        "acknowledged": expected, "request_failures": failures,
        "raw_records_delta": raw_delta, "enriched_records_delta": enriched_delta,
        "dead_letter_records_delta": dead_letter_delta,
        "missing_ids": missing, "unexpected_ids": unexpected, "duplicate_output_ids": duplicates,
        "grpc_ack_p50_ms": percentile(latencies_ms, 0.5) if latencies_ms else None,
        "grpc_ack_p99_ms": percentile(latencies_ms, 0.99) if latencies_ms else None,
        "grpc_batch_wall_seconds": round(acknowledgments_finished - started, 3),
        "all_outputs_observed_wall_seconds": round(time.perf_counter() - started, 3),
        "offsets_before": before, "offsets_after": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--wait-seconds", type=int, default=90)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_probe(args.count, args.workers, args.wait_seconds)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
