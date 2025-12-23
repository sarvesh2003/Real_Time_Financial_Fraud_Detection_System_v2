FROM python:3.10-slim
WORKDIR /app

COPY python-producer/requirements.txt .
RUN pip install -r requirements.txt

COPY python-producer/producer.py .
COPY python-producer/transaction-generator-models/model_gaussian_20L.pkl .

RUN mkdir -p proto/fraud/v1
COPY proto/fraud/v1/fraud_pb2.py proto/fraud/v1/
COPY proto/fraud/v1/fraud_pb2_grpc.py proto/fraud/v1/

RUN touch proto/__init__.py proto/fraud/__init__.py proto/fraud/v1/__init__.py

CMD ["python", "-u", "producer.py"]