FROM python:3.14.0-alpine

WORKDIR /app

COPY python-producer/requirements.txt .
RUN pip install -r requirements.txt

COPY python-producer/producer.py .
COPY python-producer/transaction-generator-models/model_gaussian_20L.pkl .

COPY proto/fraud/v1/fraud_pb2.py .
COPY proto/fraud/v1/fraud_pb2_grpc.py .

CMD ["python", "-u", "producer.py"]