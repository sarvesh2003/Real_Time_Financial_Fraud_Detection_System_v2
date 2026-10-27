FROM golang:alpine

WORKDIR /app

RUN apk add --no-cache git build-base

COPY go.mod go.sum ./
RUN go mod download

COPY pb/ ./pb/
COPY internal/ ./internal/

COPY go-server/ ./go-server/

ENV CGO_ENABLED=1
RUN go build -tags musl -o fraud-server ./go-server

EXPOSE 50051
CMD ["./fraud-server"]
