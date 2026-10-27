package main

import (
	"context"
	"fmt"
	"log"
	"math"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/confluentinc/confluent-kafka-go/kafka"
	"github.com/redis/go-redis/v9"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "fraud/pb/proto/fraud/v1"
)

const (
	port       = ":50051"
	kafkaTopic = "raw_transactions"
)

type server struct {
	pb.UnimplementedFraudIngestionServer
	producer    *kafka.Producer
	redisClient *redis.ClusterClient
}

var tokenBucketRateLimiting = redis.NewScript(`
	local user_id = KEYS[1]
	local rate = tonumber(ARGV[1])
	local burst = tonumber(ARGV[2])
	local clock = redis.call("TIME")
	local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
	local info = redis.call("HMGET", user_id, "tokens", "last_refill")
	local tokens = tonumber(info[1]) or burst
	local last_refill = tonumber(info[2]) or now
	tokens = math.min(burst, tokens + math.max(0, now - last_refill) * rate)
	local allowed = 0
	if tokens >= 1 then
		tokens = tokens - 1
		allowed = 1
	end
	redis.call("HSET", user_id, "tokens", tokens, "last_refill", now)
	redis.call("EXPIRE", user_id, 60)
	return allowed
`)

func (s *server) SendTransaction(ctx context.Context, req *pb.TransactionRequest) (*pb.IngestionResponse, error) {
	start := time.Now()
	transactionsTotal.Inc()
	defer transactionDuration.Observe(time.Since(start).Seconds())
	if req.GetTransactionId() == "" || req.GetUserId() == "" || net.ParseIP(req.GetIpAddress()) == nil ||
		math.IsNaN(req.GetAmount()) || math.IsInf(req.GetAmount(), 0) || req.GetAmount() < 0 {
		transactionErrorsTotal.WithLabelValues("invalid_request").Inc()
		return nil, status.Error(codes.InvalidArgument, "invalid transaction_id, user_id, ip_address, or amount")
	}

	rateLimitUser := fmt.Sprintf("rate_limit_user:{%s}", req.GetUserId())
	allowed, err := tokenBucketRateLimiting.Run(ctx, s.redisClient, []string{rateLimitUser}, 500, 1000).Int()
	if err != nil {
		transactionErrorsTotal.WithLabelValues("rate_limit_error").Inc()
		return nil, status.Error(codes.Unavailable, "rate limit unavailable")
	}
	if allowed == 0 {
		transactionErrorsTotal.WithLabelValues("rate_limit_exceeded").Inc()
		return nil, status.Error(codes.ResourceExhausted, "rate limit exceeded")
	}

	payload, err := proto.Marshal(req)
	if err != nil {
		transactionErrorsTotal.WithLabelValues("serialization").Inc()
		return nil, status.Error(codes.Internal, "serialization failed")
	}

	// Produce only queues locally. Wait for the broker's delivery event.
	delivery := make(chan kafka.Event, 1)
	err = s.producer.Produce(&kafka.Message{
		TopicPartition: kafka.TopicPartition{Topic: &[]string{kafkaTopic}[0], Partition: kafka.PartitionAny},
		Key:            []byte(req.GetUserId()),
		Value:          payload,
	}, delivery)
	if err != nil {
		transactionErrorsTotal.WithLabelValues("kafka_enqueue").Inc()
		return nil, status.Error(codes.Unavailable, "Kafka enqueue failed")
	}
	select {
	case event := <-delivery:
		message, ok := event.(*kafka.Message)
		if !ok || message.TopicPartition.Error != nil {
			transactionErrorsTotal.WithLabelValues("kafka_delivery").Inc()
			return nil, status.Error(codes.Unavailable, "Kafka delivery failed")
		}
	case <-ctx.Done():
		// A timed-out request may still reach Kafka. Retries can create raw
		// duplicates; the enricher must handle them by transaction_id.
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	return &pb.IngestionResponse{Success: true, Message: "Kafka acknowledged transaction"}, nil
}

func main() {
	// KAFKA SETUP
	kafkaAddr := os.Getenv("KAFKA_BROKER")
	if kafkaAddr == "" {
		kafkaAddr = "localhost:9092"
	}
	p, err := kafka.NewProducer(&kafka.ConfigMap{
		"bootstrap.servers":   kafkaAddr,
		"enable.idempotence":  true,
		"acks":                "all",
		"delivery.timeout.ms": 10000,
	})
	if err != nil {
		log.Fatalf("Failed to create producer: %v", err)
	}
	defer p.Close()

	// REDIS CLUSTER SETUP
	redisCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	client, err := initializeRedis(redisCtx)
	cancel()
	if err != nil {
		log.Fatalf("Redis initialization failed: %v", err)
	}
	defer client.Close()

	// METRICS SERVER
	startMetricsServer()

	// Starting TCP Listener
	lis, err := net.Listen("tcp", port)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	// Start gRPC Server
	s := grpc.NewServer()
	pb.RegisterFraudIngestionServer(s, &server{producer: p, redisClient: client})
	shutdownCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-shutdownCtx.Done()
		s.GracefulStop()
	}()

	log.Printf("Go gRPC Server listening at %v", lis.Addr())
	if err := s.Serve(lis); err != nil {
		log.Fatalf("failed to serve: %v", err)
	}
}
