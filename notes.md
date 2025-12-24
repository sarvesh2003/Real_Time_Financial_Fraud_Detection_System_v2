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
go mod tidy
```

## Flow which we are trying to achieve
- Python producers - DONE
- Go using buffered channels to accept requests and push the messages to Kafka using a Worker Pool - DONE
- Integrating Dockerized Kafka with Go and complete the flow for raw transactions - TO DO


## COMMON ISSUES:
- When protoc compiles your .proto files into Go code, it automatically changes your field names from snake_case (used in Protobuf) to CamelCase (used in Go). This is required because in Go, a field must start with a Capital Letter to be visible (exported) outside the struct.