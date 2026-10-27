FROM python:3.10.22-slim-trixie
WORKDIR /app

# CPU-only torch first, so SDV's dependency does not pull CUDA wheels.
RUN pip install --no-cache-dir "torch==2.5.1+cpu" \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple
COPY python-producer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Same layout as the repository: traffic.py finds its assets beside itself.
COPY python-producer/traffic.py .
COPY python-producer/transaction-generator-models/model_gaussian_20L.pkl transaction-generator-models/
COPY python-producer/transaction-generator-models/userIds_10k.csv transaction-generator-models/
COPY proto/fraud/v1/fraud_pb2.py proto/fraud/v1/fraud_pb2_grpc.py proto/fraud/v1/
RUN touch proto/__init__.py proto/fraud/__init__.py proto/fraud/v1/__init__.py

CMD ["python", "-u", "traffic.py"]
