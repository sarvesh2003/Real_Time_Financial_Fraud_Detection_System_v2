## What each component is ?
- proto/ (The Contract)
    - It defines how services talk. We use versioning (v1) so we can make breaking changes later (e.g., v2) without crashing the system.

## Command Explanations
- The go mod tidy command ensures that the go.mod and go.sum files accurately reflect the dependencies required by the  project's source code.

## Installation & Setup
- Create a fraud.proto file in /proto/fraud/v1 folder path. The proto file is like a translator so that the Python program can talk with the Go program.
- Now, we need to convert the proto file into actual code.
```text
// To generate the go code in the pb directory
protoc --go_out=pb --go_opt=paths=source_relative --go-grpc_out=pb --go-grpc_opt=paths=source_relative proto/fraud/v1/fraud.proto
```
- Before trying to generate the python code, do the following
```text
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt

// To generate the python code
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. proto/fraud/v1/fraud.proto
```


## Sample request for Postman
```text
{
  "transaction_id": "manual-test-01",
  "user_id": 1001,
  "amount": 250.50,
  "timestamp": 1702780000000,
  "is_fraud": false,
  "type": "CASH_OUT",
  "old_balance_orig": 500.00,
  "new_balance_orig": 249.50,
  "old_balance_dest": 0.00,
  "new_balance_dest": 0.00,
  "is_unauthorized_overdraft": 0
}
```

## Commands to create mod and sum files in Go
```text
go mod init fraud
go get google.golang.org/grpc@latest
go get google.golang.org/protobuf@latest
go get github.com/confluentinc/confluent-kafka-go@latest
go get github.com/redis/go-redis/v9
go mod tidy
```

## Flow which we are trying to achieve
- Python producers - DONE
- Go using buffered channels to accept requests and push the messages to Kafka using a Worker Pool - DONE
- Integrating Dockerized Kafka with Go and complete the flow for raw transactions - TO DO


## COMMON ISSUES:
- When protoc compiles your .proto files into Go code, it automatically changes your field names from snake_case (used in Protobuf) to CamelCase (used in Go). This is required because in Go, a field must start with a Capital Letter to be visible (exported) outside the struct.

## Go-enricher
- Retry is required in initialize_reddis() so that the CLUSTERDOWN error can be mitigated. Along with that add depends on in Docker compose.
- Root cause of CLUSTERDOWN issue - script did not wait for the sequence of steps to complete
- Sequence of steps in the Redis startup
	- Redis - 1, 2, and 3 start up. Completely isolated and owns 0 hash slots
	- redis-cluster-setup container runs the cluster create command
	- The script sends a CLUSTER MEET command to all nodes, introducing them to each other
	- Slot assignment: Node 1 (slots 0 to 5460), Node 2 (slots 5461 - 10922), Node 3 (10923 - 16383)
	- Gossip protocol: Nodes use a separate port (cluster bus port: 16379) to rapidly exchange binary packets to make sure all the nodes agree with the slot assignment
	- Consensus: Once all the nodes have received updates from all peers and updated their interal cluster map, the state changes from FAIL/LOADING to OK
- Maxmind
  - geoip-data:/usr/share/GeoIP => HARDCODED VALUE IN MAXMIND'S official docker image
- Go's anonymous functions are similar to lambda's in Java but here, they are treated as functions, but in Java, lambdas are treated as objects that implement a functional interface.

## CHANGES
- Changing BankingAgent with a Pre-generated set of domestic locations which can be reused and to get cache hit in redis


## REFERENCES
[1] https://medium.com/@aasefeh/setting-up-a-redis-cluster-in-a-go-application-using-docker-compose-0e8044dfb6d1 [DOCKER-REDIS-GO CONNECTION]
[2] https://redis.io/docs/latest/develop/clients/go/connect/ [REDIS DOCUMENTATION - 1]
[3] https://redis.io/docs/latest/develop/clients/go/ [REDIS DOCUMENTATION - 2]
[4] https://pkg.go.dev/github.com/oschwald/geoip2-golang [FOR WRITING MMDB LOOKUP LOGIC IN ENRICHER]
[5] https://learn.microsoft.com/en-us/azure/architecture/patterns/sidecar [Sidecar pattern]
