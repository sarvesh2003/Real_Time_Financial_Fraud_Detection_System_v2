"""Generate synthetic transaction traffic from the saved SDV model.

This is a traffic source, not a fraud classifier. The fraud flag is sampled
here and passed through the pipeline as input data.
"""

from __future__ import annotations

import csv
import contextlib
import io
import logging
import math
import os
import random
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import grpc
import numpy as np
import pandas as pd
from faker import Faker
from sdv.single_table import GaussianCopulaSynthesizer

from proto.fraud.v1 import fraud_pb2, fraud_pb2_grpc


LOG = logging.getLogger("transaction-producer")
MODEL_PATH = Path("model_gaussian_20L.pkl")
USERS_PATH = Path("userIds_10k.csv")


@dataclass(frozen=True)
class Customer:
    user_id: str
    home_ip: str
    domestic_ips: tuple[str, ...]


def load_users(path: Path, limit: int) -> list[str]:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if "customer_id" not in (reader.fieldnames or []):
            raise ValueError(f"{path} needs a customer_id column")
        users = [row["customer_id"].strip() for row in reader if row["customer_id"].strip()]
    if not users:
        raise ValueError(f"{path} contains no user IDs")
    return users[:limit]


class TransactionGenerator:
    def __init__(self, seed: int, user_limit: int = 50):
        if user_limit < 1:
            raise ValueError("user_limit must be positive")
        self.rng = random.Random(seed)
        self.fake = Faker()
        self.fake.seed_instance(seed)
        np.random.seed(seed)
        self.model = GaussianCopulaSynthesizer.load(MODEL_PATH)
        self.customers = [self._customer(user_id) for user_id in load_users(USERS_PATH, user_limit)]
        LOG.info("loaded SDV transaction model and %d customers", len(self.customers))

    def _customer(self, user_id: str) -> Customer:
        home_ip = self.fake.ipv4_public()
        first_octet = home_ip.split(".")[0]
        domestic = tuple(
            f"{first_octet}.{self.rng.randint(10, 99)}.{self.rng.randint(0, 255)}.{self.rng.randint(1, 254)}"
            for _ in range(3)
        )
        return Customer(user_id, home_ip, domestic)

    def _ip_for(self, customer: Customer, is_fraud: bool) -> str:
        if is_fraud:
            if self.rng.random() < 0.8:
                return self.fake.ipv4_public()
            first, second = customer.home_ip.split(".")[:2]
            return f"{first}.{second}.{self.rng.randint(0, 255)}.{self.rng.randint(1, 254)}"
        scenario = self.rng.random()
        if scenario < 0.85:
            return customer.home_ip
        if scenario < 0.95:
            return self.rng.choice(customer.domestic_ips)
        return self.fake.ipv4_public()

    def make_request(self, fraud_probability: float = 0.05) -> fraud_pb2.TransactionRequest:
        if not 0 <= fraud_probability <= 1:
            raise ValueError("fraud_probability must be between 0 and 1")
        customer = self.rng.choice(self.customers)
        is_fraud = self.rng.random() < fraud_probability
        # SDV prints a progress bar for every single-row sample. Keep logs for
        # acknowledgments and failures, not progress redraws.
        with contextlib.redirect_stderr(io.StringIO()):
            sample = self.model.sample_remaining_columns(pd.DataFrame({"is_fraud": [is_fraud]})).iloc[0]
        values = {name: float(sample[column]) for name, column in (
            ("amount", "amount"),
            ("old_balance_orig", "oldBalanceOrig"),
            ("new_balance_orig", "newBalanceOrig"),
            ("old_balance_dest", "oldBalanceDest"),
            ("new_balance_dest", "newBalanceDest"),
            ("is_unauthorized_overdraft", "isUnauthorizedOverdraft"),
        )}
        if values["amount"] < 0 or any(not math.isfinite(value) for value in values.values()):
            raise ValueError("SDV sampled a negative amount or non-finite value")
        return fraud_pb2.TransactionRequest(
            transaction_id=str(uuid.UUID(int=self.rng.getrandbits(128), version=4)),
            user_id=customer.user_id,
            timestamp=int(time.time() * 1000),
            is_fraud=is_fraud,
            type=str(sample["type"]),
            ip_address=self._ip_for(customer, is_fraud),
            **values,
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    seed = int(os.getenv("TRAFFIC_SEED", "20261010"))
    user_limit = int(os.getenv("TRAFFIC_USER_LIMIT", "50"))
    interval = float(os.getenv("TRAFFIC_INTERVAL_SECONDS", "0.2"))
    fraud_probability = float(os.getenv("TRAFFIC_FRAUD_PROBABILITY", "0.05"))
    if not math.isfinite(interval) or interval < 0:
        raise ValueError("TRAFFIC_INTERVAL_SECONDS must be finite and nonnegative")
    generator = TransactionGenerator(seed, user_limit)
    address = os.getenv("GRPC_SERVER_ADDRESS", "localhost:50051")
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    with grpc.insecure_channel(address) as channel:
        stub = fraud_pb2_grpc.FraudIngestionStub(channel)
        LOG.info("sending synthetic traffic to %s, seed=%d, interval=%.3fs", address, seed, interval)
        while not stopping:
            try:
                request = generator.make_request(fraud_probability)
                response = stub.SendTransaction(request, timeout=10)
                if not response.success:
                    raise RuntimeError(response.message or "server returned success=false")
                LOG.info("acknowledged transaction_id=%s amount=%.2f", request.transaction_id, request.amount)
            except (grpc.RpcError, RuntimeError, ValueError, KeyError, TypeError) as error:
                LOG.warning("transaction not acknowledged: %s", error)
                time.sleep(1)
            if interval:
                time.sleep(interval)
    LOG.info("traffic producer stopped")


if __name__ == "__main__":
    main()
