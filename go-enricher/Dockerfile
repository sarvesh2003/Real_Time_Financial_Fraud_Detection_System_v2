FROM golang:alpine

# Installing the C compiler and development tools required by the Kafka library
RUN apk add --no-cache gcc libc-dev git

WORKDIR /app

# Copying pb files
COPY pb/proto/fraud/v1/ ./pb/

# Copy modules files
COPY go-enricher/go.mod go-enricher/go.sum ./

# Download modules
RUN go mod download

# Copy source code
COPY go-enricher/enricher.go .
COPY go-enricher/models.go .
COPY go-enricher/redis_functions.go .
COPY go-enricher/maxmind_functions.go .
COPY go-enricher/utils.go .

RUN go get google.golang.org/grpc
RUN go get google.golang.org/protobuf
RUN go mod tidy

# Force CGO enabled and use the 'musl' tag for the static build for Alpine
ENV CGO_ENABLED=1
ENV GOOS=linux

RUN go build -tags musl -o enricher .


CMD ["./enricher"]