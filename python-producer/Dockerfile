FROM python:3.10-slim
WORKDIR /app

RUN pip install --no-cache-dir "torch==2.5.1+cpu" \
    --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple
COPY python-producer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# RUN pip install -r requirements.txt

COPY python-producer/traffic.py .
COPY python-producer/transaction-generator-models/model_gaussian_20L.pkl .
COPY python-producer/transaction-generator-models/userIds_10k.csv .

RUN mkdir -p proto/fraud/v1
COPY proto/fraud/v1/fraud_pb2.py proto/fraud/v1/
COPY proto/fraud/v1/fraud_pb2_grpc.py proto/fraud/v1/

RUN touch proto/__init__.py proto/fraud/__init__.py proto/fraud/v1/__init__.py

CMD ["python", "-u", "traffic.py"]
