import time
import uuid
import random
import os
import grpc
import pandas as pd
from sdv.single_table import GaussianCopulaSynthesizer

import fraud_pb2
import fraud_pb2_grpc

MODEL_PATH = 'model_gaussian_20L.pkl'
SERVER_ADDR = os.getenv('GRPC_SERVER_ADDRESS', 'localhost:50051')

USER_IDS = [1001, 1002, 1003, 1004, 1005, 9999]

def run_producer():
    print(f"...Loading model from {MODEL_PATH}...")
    try:
        model = GaussianCopulaSynthesizer.load(MODEL_PATH)
        print("Model loaded.")
    except Exception as e:
        print(f"Error loading model: {e}")
        return

    print(f"Connecting to gRPC server at {SERVER_ADDR}...")
    
    with grpc.insecure_channel(SERVER_ADDR) as channel:
        stub = fraud_pb2_grpc.FraudIngestionStub(channel)

        while True:
            try:
                sample = model.sample(num_rows=1).iloc[0]
                user_id = random.choice(USER_IDS)
                req = fraud_pb2.TransactionRequest(
                    transaction_id = str(uuid.uuid4()),
                    user_id = user_id,
                    timestamp = int(time.time() * 1000),
                    amount = float(sample['amount']),
                    is_fraud = bool(sample['is_fraud']),                    
                    type = str(sample['type']),
                    old_balance_orig = float(sample['oldBalanceOrig']),
                    new_balance_orig = float(sample['newBalanceOrig']),
                    old_balance_dest = float(sample['oldBalanceDest']),
                    new_balance_dest = float(sample['newBalanceDest']),
                    is_unauthorized_overdraft = float(sample['isUnauthorizedOverdraft'])
                )

                response = stub.SendTransaction(req)
                print(f"Sent: User {user_id} | Amt {req.amount:.2f} | {response.message}")

                time.sleep(0.2)

            except grpc.RpcError as e:
                print(f"gRPC Error: {e.code()} - {e.details()}")
                time.sleep(2)
            except Exception as e:
                print(f"Generator Error: {e}")
                time.sleep(1)

if __name__ == "__main__":
    print("Waiting 10s for system startup...")
    time.sleep(10)
    run_producer()