FROM golang:alpine

WORKDIR /app

RUN apk add --no-cache git build-base

COPY go-server/go.mod go-server/go.sum ./
RUN go mod download

COPY pb/proto/fraud/v1/ ./pb/

COPY go-server/server.go .

ENV CGO_ENABLED=1
RUN go build -tags musl -o fraud-server server.go

EXPOSE 50051
CMD ["./fraud-server"]