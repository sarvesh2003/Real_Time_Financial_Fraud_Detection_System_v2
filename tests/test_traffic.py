"""Synthetic traffic generator and send loop, without a running pipeline.

Needs the producer's dependencies (SDV, grpcio), so run it in its image with
the repository mounted read-only:
docker run --rm --network none -v <repo>:/src:ro -w /src --entrypoint python \
    <python-producer image> -m unittest tests.test_traffic -v
"""

import ipaddress
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python-producer"))
import traffic  # noqa: E402


def describe(request):
    return (request.transaction_id, request.user_id, request.ip_address, request.is_fraud,
            round(request.amount, 6), request.type)


class GeneratorTests(unittest.TestCase):
    def test_same_seed_gives_the_same_request_sequence(self):
        first = [describe(traffic.TransactionGenerator(11, 5).make_request()) for _ in range(1)]
        a = traffic.TransactionGenerator(11, 5)
        sequence_a = [describe(a.make_request()) for _ in range(6)]
        b = traffic.TransactionGenerator(11, 5)
        sequence_b = [describe(b.make_request()) for _ in range(6)]
        self.assertEqual(sequence_a, sequence_b)
        self.assertEqual(first[0], sequence_a[0])
        c = traffic.TransactionGenerator(12, 5)
        self.assertNotEqual(sequence_a, [describe(c.make_request()) for _ in range(6)])

    def test_requests_pass_ingress_validation(self):
        generator = traffic.TransactionGenerator(20261010, 50)
        requests = [generator.make_request(0.5) for _ in range(40)]
        self.assertEqual(len({r.transaction_id for r in requests}), 40)
        for request in requests:
            self.assertTrue(request.transaction_id and request.user_id)
            ipaddress.ip_address(request.ip_address)
            self.assertTrue(math.isfinite(request.amount) and request.amount >= 0)
        self.assertTrue({r.is_fraud for r in requests} == {True, False})

    def test_users_come_from_a_fixed_pool_with_home_ips(self):
        generator = traffic.TransactionGenerator(5, 3)
        requests = [generator.make_request(0.0) for _ in range(60)]
        self.assertLessEqual(len({r.user_id for r in requests}), 3)
        homes = {c.user_id: c.home_ip for c in generator.customers}
        at_home = sum(r.ip_address == homes[r.user_id] for r in requests)
        self.assertGreater(at_home, 40)  # 85% expected for legitimate traffic

    def test_rejects_invalid_arguments(self):
        with self.assertRaises(ValueError):
            traffic.TransactionGenerator(1, 0)
        with self.assertRaises(ValueError):
            traffic.TransactionGenerator(1, 2).make_request(1.5)


class AssetPathTests(unittest.TestCase):
    def test_loads_saved_model_from_any_working_directory(self):
        original = os.getcwd()
        with tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)
            try:
                generator = traffic.TransactionGenerator(3, 2)
            finally:
                os.chdir(original)
        self.assertEqual(len(generator.customers), 2)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        settings = traffic.TrafficSettings.from_env({})
        self.assertEqual((settings.seed, settings.user_limit, settings.interval_seconds,
                          settings.fraud_probability, settings.timeout_seconds, settings.address),
                         (20261010, 50, 0.2, 0.05, 10.0, "localhost:50051"))
        for bad in ({"TRAFFIC_INTERVAL_SECONDS": "-1"}, {"TRAFFIC_INTERVAL_SECONDS": "nan"},
                    {"TRAFFIC_FRAUD_PROBABILITY": "2"}, {"TRAFFIC_USER_LIMIT": "0"},
                    {"TRAFFIC_TIMEOUT_SECONDS": "0"}, {"TRAFFIC_SEED": "abc"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                traffic.TrafficSettings.from_env(bad)


class FakeRpcError(grpc.RpcError):
    def __str__(self):
        return "UNAVAILABLE: Kafka delivery failed"


class Stub:
    def __init__(self, outcomes):
        self.outcomes, self.timeouts = list(outcomes), []

    def SendTransaction(self, request, timeout):
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return traffic.fraud_pb2.IngestionResponse(success=outcome, message="" if outcome else "rejected")


class Generator:
    def make_request(self, fraud_probability):
        return traffic.fraud_pb2.TransactionRequest(transaction_id="t", user_id="u", amount=1, ip_address="192.0.2.1")


class SendLoopTests(unittest.TestCase):
    def test_counts_outcomes_backs_off_on_failure_and_stops(self):
        stub = Stub([True, FakeRpcError(), False, True])
        sleeps = []
        settings = traffic.TrafficSettings.from_env({"TRAFFIC_INTERVAL_SECONDS": "0.5", "TRAFFIC_TIMEOUT_SECONDS": "3"})
        sent = traffic.send_loop(stub, Generator(), settings, should_stop=lambda: not stub.outcomes,
                                 sleep=sleeps.append)
        self.assertEqual(sent, {"acknowledged": 2, "failed": 2})
        self.assertEqual(stub.timeouts, [3.0] * 4)
        self.assertEqual(sleeps.count(traffic.ERROR_BACKOFF_SECONDS), 2)
        self.assertEqual(sleeps.count(0.5), 4)

    def test_programming_errors_are_not_swallowed(self):
        class Broken:
            def make_request(self, fraud_probability):
                raise KeyError("amount")

        with self.assertRaises(KeyError):
            traffic.send_loop(Stub([]), Broken(), traffic.TrafficSettings.from_env({}),
                              should_stop=lambda: False, sleep=lambda _: None)


if __name__ == "__main__":
    unittest.main()
