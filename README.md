# Real-Time Financial Fraud Detection System (V2)

A redesigned fraud detection platform featuring synthetic data generation and cross-language microservice architecture. This version introduces statistically realistic transaction simulation using SDV (Gaussian Copula) and Python ↔ Go communication via gRPC.

> **Active Development**: Core ingestion pipeline is complete. Enrichment, streaming inference, and MLOps components are being migrated from [Version 1](link-to-v1).

## What's New in V2

| Feature | V1 | V2 |
|---------|----|----|
| Data Generation | Random Go simulation | ✅ SDV Gaussian Copula (statistically realistic) |
| Ingestion | Go → Kafka direct | ✅ Python → gRPC → Go → Kafka |
| Schema | JSON | ✅ Protocol Buffers (strongly typed) |
| Training Data | Generic synthetic | ✅ Modeled on real fraud dataset patterns |

## What This Project Demonstrates

| Skills Demonstrated |
|---------------------|
| gRPC services, Protocol Buffers, Kafka streaming |
| Synthetic data pipelines, schema design |
|Statistical generative models (Gaussian Copula) |
|Python ↔ Go interoperability |


## Current Features

### Synthetic Data Generation (SDV + Gaussian Copula)
- **Pre-trained model**: `model_gaussian_20L.pkl` trained on 20 lakh samples of the financial fraud dataset
- **Realistic distributions**: Preserves statistical correlations between transaction features
- **Privacy-safe**: Generates unlimited test data without real financial records
- **Rich schema**: Amount, type, balance deltas, fraud labels, overdraft flags

### Cross-Language Integration (Python → Go via gRPC)
- **Protocol Buffers**: Strongly-typed `TransactionRequest` schema with 11 fields
- **Efficient serialization**: Binary protobuf encoding for high throughput
- **Service definition**: `FraudIngestion.SendTransaction` RPC

### Go Ingestion Service (gRPC → Kafka)
- **gRPC server**: Listens on port 50051
- **Kafka producer**: Publishes protobuf-serialized messages to `raw_transactions` topic
- **Environment configurable**: `KAFKA_BROKER` for flexible deployment

### Containerized Infrastructure
- **Docker Compose**: Single command startup for entire pipeline
- **Services**: Zookeeper, Kafka, Go server, Python producer
- **Network isolation**: Internal service discovery via Docker networking

## Technology Stack

| Layer | Technologies |
|-------|--------------|
| **Data Generation** | Python, SDV (Gaussian Copula), Pandas |
| **Communication** | gRPC, Protocol Buffers |
| **Ingestion** | Go, confluent-kafka-go |
| **Streaming** | Apache Kafka, Zookeeper |
| **Infrastructure** | Docker, Docker Compose |

## Transaction Schema

```protobuf
message TransactionRequest {
  string transaction_id = 1;
  int64 user_id = 2;
  double amount = 3;
  int64 timestamp = 4;
  bool is_fraud = 5;
  string type = 6;
  double old_balance_orig = 7;
  double new_balance_orig = 8;
  double old_balance_dest = 9;
  double new_balance_dest = 10;
  double is_unauthorized_overdraft = 11;
}
```

## Getting Started

### Prerequisites
- Docker & Docker Compose
- (Optional) Go 1.19+ and Python 3.9+ for local development

### Quick Start
```bash
# Clone the repository
git clone https://github.com/yourusername/fraud-detection-v2.git
cd fraud-detection-v2

# Start all services
docker-compose up --build

# View logs
docker-compose logs -f python-producer
docker-compose logs -f go-server
```

### Local Development
```bash
# Generate protobuf files (if modifying schema)
protoc --go_out=. --go-grpc_out=. proto/fraud/v1/fraud.proto

# Run Go server locally
cd go-server && go run server.go

# Run Python producer locally
cd python-producer && pip install -r requirements.txt && python producer.py
```

## Project Structure

```
├── go-server/
│   ├── server.go           # gRPC server + Kafka producer
│   └── Dockerfile
├── python-producer/
│   ├── producer.py         # SDV-based transaction generator
│   ├── model_gaussian_20L.pkl  # Pre-trained Gaussian Copula model
│   ├── fraud_pb2.py        # Generated protobuf classes
│   ├── fraud_pb2_grpc.py   # Generated gRPC stubs
│   └── Dockerfile
├── pb/
│   └── proto/fraud/v1/
│       ├── fraud.pb.go     # Generated Go protobuf
│       └── fraud_grpc.pb.go
├── proto/
│   └── fraud/v1/
│       └── fraud.proto     # Schema definition
├── docker-compose.yaml
└── README.md
```

## Coming Soon

Features being migrated from V1:

| Feature | Status |
|---------|--------|
| **GeoIP Enrichment** | In Progress |
| **Redis Caching Layer** | In Progress |
| **Flink ML Inference** | Planned |
| **Hot-Reload Model Serving** | Planned |
| **Airflow CT Pipeline** | Planned |
| **MLflow Experiment Tracking** | Planned |
| **DVC Data Versioning** | Planned |

