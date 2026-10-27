# confluent-kafka-go needs CGO; the musl tag links librdkafka for Alpine.
FROM golang:1.27.3-alpine3.24
RUN apk add --no-cache build-base git
WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY pb/ ./pb/
COPY internal/ ./internal/
COPY go-server/ ./go-server/

ENV CGO_ENABLED=1
RUN go build -tags musl -o fraud-server ./go-server

CMD ["./fraud-server"]
