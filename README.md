# Real-Time Financial Fraud Detection System - Version 2

A production-grade distributed fraud detection platform processing 9,000+ transactions per second with sub-20ms P99 latency. This version introduces a complete architectural redesign from V1, featuring Go microservices, gRPC communication, Redis Cluster, and stateful stream enrichment.

> **Architecture Evolution:** V1 focused on ML inference and batch processing. V2 rebuilds the ingestion and enrichment layer with emphasis on performance, scalability, and operational robustness. ML components from V1 will be migrated in V3.

> **Version 1:** [Available here](https://github.com/sarvesh2003/Fraud-Detection-System) - includes PyFlink ML inference, Airflow MLOps pipeline, and model versioning with DVC/MLflow.

## Performance Metrics

| Metric | Value | Test Conditions |
|--------|-------|-----------------|
| **Peak Throughput** | 9,152 TPS | 50K requests, 100 concurrent connections |
| **Sustained Throughput** | 6,903 TPS | 60-second load test |
| **P99 Latency** | 16.9ms | Under sustained load |
| **Success Rate** | 99.99% | 414,163 total requests |
| **Error Rate** | 0.01% | Includes transient network issues |

Load tested using `ghz` gRPC benchmarking tool with varied transaction amounts, random IP addresses, and distributed user load to simulate realistic conditions.

## System Architecture
```
Python Producer (Mesa + SDV) 
    ↓ gRPC
Go Ingestion Server (Rate Limiting + Dedup)
    ↓ Kafka
Go Enrichment Service (GeoIP + Fraud Metrics)
    ↓ Redis Cluster (3 nodes)
    ↓ Kafka
Enriched Transaction Stream
```
<img width="683" height="433" alt="image" src="https://github.com/user-attachments/assets/55c096e4-add8-47b4-ab2a-2d8e0149e228" />


### Data Flow

1. **Generation Layer**: Mesa agent-based simulation with SDV Gaussian Copula generates statistically realistic transactions
2. **Ingestion Layer**: gRPC server with distributed token-bucket rate limiting (500 req/sec per user)
3. **Enrichment Layer**: Stream processor adds GeoIP data (city, country, ISP, ASN) and computes fraud velocity metrics
4. **Storage Layer**: Redis Cluster for sub-millisecond caching and atomic metric aggregation
5. **Streaming Layer**: Kafka topics with exactly-once semantics for downstream processing

## What's New in V2

### Architecture Changes

| Component | V1 | V2 |
|-----------|----|----|
| **Language** | Python monolith | Go microservices |
| **Communication** | Direct Kafka | gRPC + Kafka |
| **Data Generation** | Random Go simulation | Mesa agent-based + SDV Gaussian Copula |
| **User Behavior** | Static random patterns | Agent-based behavioral modeling (home/travel IPs) |
| **Caching** | Single Redis | 3-node Redis Cluster with hash slots |
| **Enrichment** | Basic GeoIP | GeoIP + ASN + fraud velocity tracking |
| **Rate Limiting** | None | Distributed token bucket (Redis Lua) |
| **Deduplication** | None | Transaction-level idempotency |
| **Schema** | JSON | Protocol Buffers |

### Key Technical Improvements

**Data Generation:**
- **Mesa agent-based framework**: Each user is an independent agent with persistent behavioral context (home IP, domestic travel pool, transaction patterns)
- **SDV Gaussian Copula**: Pre-trained on 2M transactions from Databricks FSI fraud dataset using PySpark, generating statistically realistic amounts, types, and correlations
- **Context-aware IP generation**: Legitimate transactions (85% home, 10% domestic, 5% foreign) vs. fraud (80% foreign, 20% spoofed)
- **Behavioral realism**: Agents maintain state across transactions, simulating real user patterns rather than random data

**Performance Optimizations:**
- Atomic Redis Lua scripts reduce network round trips by 4x (7 operations → 1)
- Protocol Buffers provide 3-5x faster serialization than JSON
- Connection pooling for Kafka and Redis eliminates connection overhead
- Manual offset commits batch 20 messages for reduced Kafka overhead

**Reliability Features:**
- Exactly-once Kafka semantics with manual offset management
- Transaction deduplication prevents double-processing
- Distributed rate limiting protects downstream services
- Graceful shutdown with in-flight request handling

## Technology Stack

| Layer | Technologies |
|-------|--------------|
| **Data Generation** | Python, Mesa (agent-based modeling), SDV Gaussian Copula, Faker |
| **Ingestion** | Go, gRPC, Protocol Buffers, Redis Cluster |
| **Stream Processing** | Go, Apache Kafka, confluent-kafka-go |
| **Enrichment** | MaxMind GeoIP2 (City + ASN), Redis Cluster |
| **Infrastructure** | Docker, Docker Compose, Zookeeper |
| **Load Testing** | ghz (gRPC benchmarking) |

## Component Overview

### 1. Synthetic Data Generator (Python)

**Technology:** Mesa agent-based framework + SDV Gaussian Copula

**Model Training:**
- Dataset: Databricks FSI fraud dataset (2M+ transactions)
- Training platform: PySpark in Databricks
- Model: Gaussian Copula synthesizer (`model_gaussian_20L.pkl`)
- Preserves statistical correlations between transaction features

**Features:**
- Context-aware IP generation based on fraud probability
- Behavioral realism with persistent agent state
- 5% fraud rate matching real-world distribution
- ~5 TPS continuous stream (configurable)

### 2. Go Ingestion Server (gRPC)

**Protocol:** gRPC with Protocol Buffers

**Request Processing:**
1. Deduplication check via Redis (10-minute TTL)
2. Rate limiting via token bucket algorithm
3. Kafka produce with protobuf serialization

### 3. Go Enrichment Service (Stream Processor)

**Input Topic:** `raw_transactions`  
**Output Topic:** `enriched_transactions`

**GeoIP Enrichment:**
Adds city, country, ISP, ASN using MaxMind databases with 24-hour Redis caching.

**Fraud Velocity Metrics:**
Tracks transaction patterns per IP address over 2-hour windows: transaction frequency, spending velocity ($/hour), statistical aggregates (average, max amounts). Computes all metrics atomically using Redis Lua scripts to prevent race conditions.

**Real-time Alerts:**
Detects suspicious patterns including high-velocity spending, unusual transaction frequencies, and anomalous amounts. Alerts logged for downstream investigation.

### 4. Redis Cluster

3-node cluster providing distributed caching, atomic operations via Lua scripts, and sub-millisecond lookups. Handles GeoIP cache, fraud metrics, deduplication, and rate limiting state.

### 5. Apache Kafka

Provides message streaming with exactly-once semantics. Topics include `raw_transactions` (protobuf) and `enriched_transactions` (JSON).

## Transaction Schema

### Protocol Buffers (Input)
```protobuf
message TransactionRequest {
  string transaction_id = 1;
  string user_id = 2;
  double amount = 3;
  int64 timestamp = 4;
  bool is_fraud = 5;
  string type = 6;
  double old_balance_orig = 7;
  double new_balance_orig = 8;
  double old_balance_dest = 9;
  double new_balance_dest = 10;
  double is_unauthorized_overdraft = 11;
  string ip_address = 12;
}
```

### Enriched Transaction (Output)
```json
{
  "transaction_id": "uuid",
  "user_id": "string",
  "amount": 0.0,
  "ip_address": "1.2.3.4",
  "city": "San Francisco",
  "country": "United States",
  "asn": "AS15169",
  "isp": "Google LLC",
  "is_hosting": true,
  "txn_count_2h": 5,
  "total_amount_2h": 5000.0,
  "amount_velocity": 2500.0,
  "avg_amount_2h": 1000.0,
  "max_amount_2h": 2000.0
}
```

## Getting Started

### Prerequisites

**Required:**
- Docker 20.10+
- Docker Compose 2.0+
- 8GB+ RAM available
- 5GB+ free disk space

**Optional (for local development):**
- Go 1.19+
- Python 3.9+
- Protocol Buffers compiler (protoc)

### Quick Start
```bash
# Clone repository
git clone https://github.com/yourusername/fraud-detection-v2.git
cd fraud-detection-v2

# Set MaxMind environment variables
export GEOIPUPDATE_ACCOUNT_ID=your_account_id
export GEOIPUPDATE_LICENSE_KEY=your_license_key
export GEOIPUPDATE_EDITION_IDS="GeoLite2-City GeoLite2-ASN"
export GEOIPUPDATE_FREQUENCY=24

# Start all services
docker-compose up -d

# Wait for services to initialize
sleep 90

# Verify services
docker-compose ps
```

### Using Pre-built Images
```bash
docker pull sarvesh3006/fraud-detection-server:latest
docker pull sarvesh3006/fraud-detection-producer:latest
docker pull sarvesh3006/fraud-detection-enricher:latest

docker-compose -f docker-compose.public.yml up -d
```

### Verify System
```bash
# Check gRPC server
docker-compose logs go-server | grep listening

# Check enricher
docker-compose logs go-enricher | grep SUCCESS

# View Kafka topics
docker exec $(docker-compose ps -q kafka) \
  kafka-topics --bootstrap-server localhost:9092 --list

# Monitor transactions
docker exec $(docker-compose ps -q kafka) \
  kafka-console-consumer \
  --bootstrap-server localhost:9092 \
  --topic enriched_transactions \
  --from-beginning \
  --max-messages 5
```

## Load Testing

### Setup
```bash
# Install ghz
go install github.com/bojand/ghz/cmd/ghz@latest
```

### Run Tests
```bash
cd load-tests

# Run complete test suite
./run_load_tests.sh

# Analyze results
./analyze.sh
```

### Example Results
```
Test            | Requests | RPS   | Avg Latency | P99 Latency | Success
----------------|----------|-------|-------------|-------------|----------
1K              | 1,000    | 2820  | 6.08ms      | 58.67ms     | 100.00%
10K             | 10,000   | 6034  | 6.85ms      | 36.44ms     | 100.00%
50K             | 50,000   | 9152  | 9.46ms      | 20.81ms     | 100.00%
100K            | 100,000  | 6863  | 12.51ms     | 34.06ms     | 100.00%
Sustained 60s   | 414,163  | 6903  | 6.58ms      | 16.92ms     | 99.99%
```

## Local Development

### Regenerate Protocol Buffers
```bash
# Go
protoc --go_out=. --go-grpc_out=. proto/fraud/v1/fraud.proto

# Python
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. proto/fraud/v1/fraud.proto
```
### Environment Variables

**Go Server:**
- `KAFKA_BROKER` (default: `localhost:9092`)
- `REDIS_CLUSTER_ADDRS` (default: Docker IPs)

**Go Enricher:**
- `KAFKA_BROKER` (default: `localhost:9092`)
- `KAFKA_CONSUMER_TOPICS_ENRICHER` (default: `raw_transactions`)
- `REDIS_CLUSTER_ADDRS` (default: Docker IPs)
- `GEO_TTL_HOURS` (default: `24`)

**Python Producer:**
- `GRPC_SERVER_ADDRESS` (default: `localhost:50051`)

## Project Structure
```
.
├── docker-compose.yaml              # Local development
├── docker-compose.public.yml        # Pre-built images
├── proto/fraud/v1/fraud.proto       # Schema definition
├── go-server/                       # gRPC ingestion service
├── go-enricher/                     # Stream processor
├── python-producer/                 # Mesa + SDV generator
└── load-tests/                      # Performance testing scripts
```

## References

[1] [Redis Cluster with Go and Docker Compose](https://medium.com/@aasefeh/setting-up-a-redis-cluster-in-a-go-application-using-docker-compose-0e8044dfb6d1)

[2] [Redis Go Client Documentation - Connect](https://redis.io/docs/latest/develop/clients/go/connect/)

[3] [Redis Go Client Documentation](https://redis.io/docs/latest/develop/clients/go/)

[4] [MaxMind GeoIP2 Go Package](https://pkg.go.dev/github.com/oschwald/geoip2-golang)

[5] [Sidecar Pattern - Azure Architecture](https://learn.microsoft.com/en-us/azure/architecture/patterns/sidecar)

[6] [Redis Locking with Lua Scripts](https://medium.com/@nikhi.unni/redis-locking-with-lua-scripts-solving-race-conditions-in-threaded-applications-2e7c789dc235)

[7] [Fixing Race Conditions in Go with Redis](https://hackernoon.com/fixing-race-conditions-in-go-with-redis-based-distributed-locks)

[8] [Build a Token Bucket Limiter in Go](https://dev.to/leapcell/build-a-token-bucket-limiter-in-go-in-under-100-lines-4f61)
