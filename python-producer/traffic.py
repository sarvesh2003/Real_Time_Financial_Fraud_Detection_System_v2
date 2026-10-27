"""Generate synthetic transaction traffic from the saved SDV model.

This is a traffic source, not a fraud classifier. The fraud flag is sampled
here and passed through the pipeline as input data.

Workload (all from TrafficSettings):
- A fixed pool of the first TRAFFIC_USER_LIMIT user IDs. Each customer gets a
  home IP and three IPs in the same /8 when the generator is built.
- Each request picks a customer at random and is flagged fraud with
  TRAFFIC_FRAUD_PROBABILITY. Legitimate requests use the home IP 85% of the
  time, a domestic IP 10%, a random public IP 5%; fraud uses a random public
  IP 80%, otherwise an address in the home /16.
- Amounts and balances are sampled from the SDV model conditioned on the flag.
- One request at a time, TRAFFIC_INTERVAL_SECONDS apart, each with a
  TRAFFIC_TIMEOUT_SECONDS deadline. A failed request waits an extra
  ERROR_BACKOFF_SECONDS. SIGINT/SIGTERM stop after the request in flight.

TRAFFIC_SEED fixes the customer pool and the sequence of users, IPs, flags,
transaction IDs and sampled amounts (SDV draws from NumPy's global random
state, so use one generator per process). Timestamps are wall-clock time.
"""

from __future__ import annotations

import contextlib
import csv
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
from typing import Callable, Mapping

import grpc
import numpy as np
import pandas as pd
from faker import Faker
from sdv.single_table import GaussianCopulaSynthesizer

from proto.fraud.v1 import fraud_pb2, fraud_pb2_grpc

LOG = logging.getLogger("transaction-producer")
ASSETS = Path(__file__).resolve().parent / "transaction-generator-models"
MODEL_PATH = ASSETS / "model_gaussian_20L.pkl"
USERS_PATH = ASSETS / "userIds_10k.csv"
ERROR_BACKOFF_SECONDS = 1.0


@dataclass(frozen=True)
class TrafficSettings:
    seed: int = 20261010
    user_limit: int = 50
    interval_seconds: float = 0.2
    fraud_probability: float = 0.05
    timeout_seconds: float = 10.0
    address: str = "localhost:50051"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TrafficSettings":
        env = os.environ if env is None else env
        defaults = cls()
        try:
            settings = cls(
                seed=int(env.get("TRAFFIC_SEED", defaults.seed)),
                user_limit=int(env.get("TRAFFIC_USER_LIMIT", defaults.user_limit)),
                interval_seconds=float(env.get("TRAFFIC_INTERVAL_SECONDS", defaults.interval_seconds)),
                fraud_probability=float(env.get("TRAFFIC_FRAUD_PROBABILITY", defaults.fraud_probability)),
                timeout_seconds=float(env.get("TRAFFIC_TIMEOUT_SECONDS", defaults.timeout_seconds)),
                address=env.get("GRPC_SERVER_ADDRESS", defaults.address),
            )
        except ValueError as error:
            raise ValueError(f"invalid traffic setting: {error}") from None
        if settings.user_limit < 1:
            raise ValueError("TRAFFIC_USER_LIMIT must be positive")
        if not math.isfinite(settings.interval_seconds) or settings.interval_seconds < 0:
            raise ValueError("TRAFFIC_INTERVAL_SECONDS must be finite and nonnegative")
        if not 0 <= settings.fraud_probability <= 1:
            raise ValueError("TRAFFIC_FRAUD_PROBABILITY must be between 0 and 1")
        if not math.isfinite(settings.timeout_seconds) or settings.timeout_seconds <= 0:
            raise ValueError("TRAFFIC_TIMEOUT_SECONDS must be positive")
        return settings


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


def send_loop(stub, generator, settings: TrafficSettings, should_stop: Callable[[], bool],
              sleep: Callable[[float], None] = time.sleep) -> dict[str, int]:
    """Send one request at a time until should_stop(); returns outcome counts.

    gRPC errors, success=false replies and rejected samples are logged and
    retried after a backoff. Any other exception is a bug and propagates.
    """
    sent = {"acknowledged": 0, "failed": 0}
    while not should_stop():
        try:
            request = generator.make_request(settings.fraud_probability)
            response = stub.SendTransaction(request, timeout=settings.timeout_seconds)
            if not response.success:
                raise RuntimeError(response.message or "server returned success=false")
            sent["acknowledged"] += 1
            LOG.info("acknowledged transaction_id=%s amount=%.2f", request.transaction_id, request.amount)
        except (grpc.RpcError, RuntimeError, ValueError) as error:
            sent["failed"] += 1
            LOG.warning("transaction not acknowledged: %s", error)
            sleep(ERROR_BACKOFF_SECONDS)
        if settings.interval_seconds:
            sleep(settings.interval_seconds)
    return sent


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = TrafficSettings.from_env()
    generator = TransactionGenerator(settings.seed, settings.user_limit)
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    with grpc.insecure_channel(settings.address) as channel:
        LOG.info("sending synthetic traffic to %s, seed=%d, interval=%.3fs",
                 settings.address, settings.seed, settings.interval_seconds)
        sent = send_loop(fraud_pb2_grpc.FraudIngestionStub(channel), generator, settings, lambda: stopping)
    LOG.info("traffic producer stopped: %s", sent)


if __name__ == "__main__":
    main()
