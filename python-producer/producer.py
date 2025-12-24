import time
import uuid
import random
import os
import logging
import signal
import sys
import grpc
import csv
from faker import Faker
import pandas as pd
from sdv.single_table import GaussianCopulaSynthesizer

from proto.fraud.v1 import fraud_pb2
from proto.fraud.v1 import fraud_pb2_grpc

from mesa import Agent, Model


# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [MESA-3.0] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


MODEL_PATH = 'model_gaussian_20L.pkl'
SERVER_ADDR = os.getenv('GRPC_SERVER_ADDRESS', 'localhost:50051')
USERIDS_PATH = "userIds_10k.csv"
USERIDS_LIMIT = 30
USER_IDS = []
CHANCE_OF_FRAUD = 0.05
SLEEP_INTERVAL = 0.2

if not os.path.exists(USERIDS_PATH):
    logger.warning(f"CSV {USERIDS_PATH} not found. Generating random User IDs.")
    USER_IDS = [str(1000 + i) for i in range(USERIDS_LIMIT)]
else:
    with open(USERIDS_PATH, mode='r', newline='', encoding='utf-8') as csvFile:
        reader = csv.reader(csvFile)
        next(reader)
        for row in reader:
            if row:
                try:
                    USER_IDS.append(row[0]) 
                except ValueError:
                    continue

USER_IDS = USER_IDS[:USERIDS_LIMIT]

# Mesa Agent - A single banking customer
class BankingAgent(Agent):
    def __init__(self, model, unique_id):
        super().__init__(model)
        self.unique_id = unique_id
        self.fake = Faker()
        self.home_ip = self.fake.ipv4_public(network=True)
        self.home_subnet = ".".join(self.home_ip.split('.')[:2])

    # Decides the IP address for a specific transaction based on fraud status.
    def generate_context_ip(self, is_fraud):
        if not is_fraud:
            scenario = random.choices(['HOME', 'DOMESTIC', 'FOREIGN'], weights=[0.85, 0.10, 0.05])[0]
        else:
            scenario = random.choices(['FOREIGN', 'HOME_SPOOF'], weights=[0.80, 0.20])[0]

        return self._get_ip_from_scenario(scenario)

    def _get_ip_from_scenario(self, scenario):
        if scenario in ['HOME', 'HOME_SPOOF']:
            return f"{self.home_subnet}.{random.randint(0,255)}.{random.randint(1,255)}"
        elif scenario == 'DOMESTIC':
            octet1 = self.home_ip.split('.')[0] # Same Country, different region
            return f"{octet1}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,255)}"
        else:
            return self.fake.ipv4_public() # Completely random

# Mesa Model
class FraudSimulationModel(Model):

    def __init__(self):
        
        super().__init__()

        self.sdv_model = None
        self.grpc_stub = None
        self.grpc_channel = None

        # Load ML Model
        self._load_sdv_model()

        # Connect gRPC
        self._connect_grpc()

        # Create Agents
        logger.info("Initializing Agents...")
        for uid in USER_IDS:
            BankingAgent(self, unique_id=uid)
    
    def _load_sdv_model(self):
        logger.info(f"...Loading model from {MODEL_PATH}...")
        try:
            self.sdv_model = GaussianCopulaSynthesizer.load(MODEL_PATH)
            logger.info("SDV Model Loaded")
        except Exception as e:
            logger.critical(f"Error loading model: {e}")
            sys.exit(1)
    
    def _connect_grpc(self):
        logger.info(f"Connecting to gRPC server at {SERVER_ADDR}...")
        self.grpc_channel = grpc.insecure_channel(SERVER_ADDR)
        self.grpc_stub = fraud_pb2_grpc.FraudIngestionStub(self.grpc_channel)

    # Picking a random agent -> Generate txn using SDV -> Generate context basedd IP address -> Send to system
    def step(self):
        agent = self.random.choice(self.agents)
        is_fraud = self.random.random() < CHANCE_OF_FRAUD
        
        # SDV generated txn based on is_fraud label
        condition = pd.DataFrame({'is_fraud': [is_fraud]})
        sample = self.sdv_model.sample_remaining_columns(condition).iloc[0]

        # Context IP generation
        ip_address = agent.generate_context_ip(is_fraud)

        # Send
        self._send_transaction(agent.unique_id, sample, ip_address, is_fraud)
    
    def _send_transaction(self, user_id, sample, ip_address, is_fraud):
        try:
            req = fraud_pb2.TransactionRequest(
                    transaction_id = str(uuid.uuid4()),
                    user_id = user_id,
                    timestamp = int(time.time() * 1000),
                    amount = float(sample['amount']),
                    is_fraud = is_fraud,                    
                    type = str(sample['type']),
                    old_balance_orig = float(sample['oldBalanceOrig']),
                    new_balance_orig = float(sample['newBalanceOrig']),
                    old_balance_dest = float(sample['oldBalanceDest']),
                    new_balance_dest = float(sample['newBalanceDest']),
                    is_unauthorized_overdraft = float(sample['isUnauthorizedOverdraft']),
                    ip_address = ip_address
                )

            self.grpc_stub.SendTransaction(req)

            logger.info(f"Sent: User {user_id} | Amt {req.amount:.2f} | IP: {ip_address} | Fraud: {is_fraud}")
    
        except grpc.RpcError as e:
            print(f"gRPC Error: {e.code()} - {e.details()}")
            time.sleep(2)
        except Exception as e:
            print(f"Generator Error: {e}")
            time.sleep(1)
        
    def cleanup(self):
        if self.grpc_channel:
            self.grpc_channel.close() # closing channel afterr usage

 

if __name__ == "__main__":
    print("Waiting 10s for system startup...")
    time.sleep(10)
    
    simulation = FraudSimulationModel()

    def handle_exit(signum, frame):
        logger.info("Stopping Simulation...")
        simulation.cleanup()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    logger.info("Starting the Simulation Loop...")

    while True:
        simulation.step()
        time.sleep(SLEEP_INTERVAL)
    