package main

import (
	"context"
	"errors"
	"log"
	"math"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/confluentinc/confluent-kafka-go/kafka"
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

type rateLimiter interface {
	Allow(ctx context.Context, userID string) (bool, error)
}

type rawPublisher interface {
	Publish(ctx context.Context, key, value []byte) error
}

type server struct {
	pb.UnimplementedFraudIngestionServer
	limiter   rateLimiter
	publisher rawPublisher
}

// SendTransaction succeeds only after Kafka acknowledges the raw record.
func (s *server) SendTransaction(ctx context.Context, req *pb.TransactionRequest) (*pb.IngestionResponse, error) {
	start := time.Now()
	transactionsTotal.Inc()
	defer transactionDuration.Observe(time.Since(start).Seconds())
	if req.GetTransactionId() == "" || req.GetUserId() == "" || net.ParseIP(req.GetIpAddress()) == nil ||
		math.IsNaN(req.GetAmount()) || math.IsInf(req.GetAmount(), 0) || req.GetAmount() < 0 {
		transactionErrorsTotal.WithLabelValues("invalid_request").Inc()
		return nil, status.Error(codes.InvalidArgument, "invalid transaction_id, user_id, ip_address, or amount")
	}

	allowed, err := s.limiter.Allow(ctx, req.GetUserId())
	if err != nil {
		transactionErrorsTotal.WithLabelValues("rate_limit_error").Inc()
		return nil, status.Error(codes.Unavailable, "rate limit unavailable")
	}
	if !allowed {
		transactionErrorsTotal.WithLabelValues("rate_limit_exceeded").Inc()
		return nil, status.Error(codes.ResourceExhausted, "rate limit exceeded")
	}

	payload, err := proto.Marshal(req)
	if err != nil {
		transactionErrorsTotal.WithLabelValues("serialization").Inc()
		return nil, status.Error(codes.Internal, "serialization failed")
	}

	err = s.publisher.Publish(ctx, []byte(req.GetUserId()), payload)
	switch {
	case err == nil:
		return &pb.IngestionResponse{Success: true, Message: "Kafka acknowledged transaction"}, nil
	case errors.Is(err, errKafkaEnqueue):
		transactionErrorsTotal.WithLabelValues("kafka_enqueue").Inc()
		return nil, status.Error(codes.Unavailable, "Kafka enqueue failed")
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		// The broker may still accept the record. A client retry can create
		// a raw duplicate; the enricher handles it by transaction_id.
		return nil, status.FromContextError(err).Err()
	default:
		transactionErrorsTotal.WithLabelValues("kafka_delivery").Inc()
		return nil, status.Error(codes.Unavailable, "Kafka delivery failed")
	}
}

func main() {
	kafkaAddr := os.Getenv("KAFKA_BROKER")
	if kafkaAddr == "" {
		kafkaAddr = "localhost:9092"
	}
	producer, err := kafka.NewProducer(producerConfig(kafkaAddr))
	if err != nil {
		log.Fatalf("Failed to create producer: %v", err)
	}
	defer producer.Close()

	redisCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	client, err := initializeRedis(redisCtx)
	cancel()
	if err != nil {
		log.Fatalf("Redis initialization failed: %v", err)
	}
	defer client.Close()

	startMetricsServer()

	lis, err := net.Listen("tcp", port)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	s := grpc.NewServer()
	pb.RegisterFraudIngestionServer(s, &server{
		limiter:   redisRateLimiter{client: client},
		publisher: kafkaPublisher{producer: producer, topic: kafkaTopic},
	})
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
