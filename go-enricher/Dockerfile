FROM golang:alpine

# Installing the C compiler and development tools required by the Kafka library
RUN apk add --no-cache gcc libc-dev git

WORKDIR /app

# Copying pb files
COPY pb/ ./pb/
COPY internal/ ./internal/

# Copy modules files
COPY go.mod go.sum ./

# Download modules
RUN go mod download

# Copy source code
COPY go-enricher/ ./go-enricher/

# Force CGO enabled and use the 'musl' tag for the static build for Alpine
ENV CGO_ENABLED=1
ENV GOOS=linux

RUN go build -tags musl -o enricher ./go-enricher


CMD ["./enricher"]
