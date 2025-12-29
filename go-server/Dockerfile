FROM golang:alpine

WORKDIR /app

RUN apk add --no-cache git build-base

COPY go-server/go.mod go-server/go.sum ./
RUN go mod download

COPY pb/proto/fraud/v1/ ./pb/

COPY go-server/server.go .
COPY go-enricher/redis_functions.go .
COPY go-enricher/models.go .


RUN go get github.com/redis/go-redis/v9

ENV CGO_ENABLED=1
RUN go build -tags musl -o fraud-server .

EXPOSE 50051
CMD ["./fraud-server"]