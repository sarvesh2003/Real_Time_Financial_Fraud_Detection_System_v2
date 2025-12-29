package main

import (
	"context"
	"fmt"
	"log"
	"net"
	"os"
	"time"

	"github.com/confluentinc/confluent-kafka-go/kafka"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"github.com/redis/go-redis/v9"

	pb "fraud/pb"
)

const (
	port       = ":50051"
	kafkaTopic = "raw_transactions"
)

type server struct {
	pb.UnimplementedFraudIngestionServer
	producer *kafka.Producer
	redisClient *redis.ClusterClient
}

var tokenBucketRateLimiting = redis.NewScript(`
	local user_id = KEYS[1]
	local rate = tonumber(ARGV[1])
	local burst = tonumber(ARGV[2])
	local now = tonumber(ARGV[3])
	local window = tonumber(ARGV[4])

	-- Check in redis for already present tokens and last refill time
	local info = redis.call("HMGET", user_id, "tokens", "last_refill")
	local curr_tokens = tonumber(info[1])
	local last_refill = tonumber(info[2])

	-- No already present tokens, new user
	if not curr_tokens then
		curr_tokens = burst
		last_refill = now
	end

	local timeDiff = math.max(0, now - last_refill)
	local refill = timeDiff * rate
	curr_tokens = math.min(burst, curr_tokens + refill)

	if curr_tokens < 1 then
		return 0
	else
		-- I have sufficient tokens, so use it
		redis.call("HMSET", user_id, "tokens", curr_tokens-1, "last_refill", now)
		redis.call("EXPIRE", user_id, 60)
		return 1
	end

`)

func (s *server) SendTransaction(ctx context.Context, req *pb.TransactionRequest) (*pb.IngestionResponse, error) {

	// Dedup key check
	dedupKey := fmt.Sprintf("dedup:{%s}", req.TransactionId)
	isDuplicate, err := s.redisClient.SIsMember(ctx, dedupKey, req.TransactionId).Result()
	if err != nil {
		log.Printf("WARNING: Some issue with Redis Dedup check: {%s}", err)
	}
	if isDuplicate {
		log.Printf("Transaction already present, so Skipping...")
	}

	// No Dedup key is present, so do the processing
	rateLimitUser := fmt.Sprintf("rate_limit_user:{%s}", req.UserId)
	// rateLimitIp := fmt.Sprintf("rate_limit_ip:{%s}", req.IpAddress)
	burst_limit := 1
	rate_limit := 1

	allowed, err := tokenBucketRateLimiting.Run(ctx, s.redisClient, []string{rateLimitUser}, burst_limit, rate_limit, time.Now().Unix(), 60).Int()

	if err != nil {
		log.Printf("WARNING: Redis Rate Limit failed: %v", err)
	} else if allowed == 0 {
		log.Printf("Rate Limit Exceeded for userId: %s", req.UserId)
		return nil, status.Errorf(codes.ResourceExhausted, "Rate Limit Exceeded")
	}

	// Pushing to Kafka
	bytes, err := proto.Marshal(req)
	if err != nil {
		return &pb.IngestionResponse{Success: false, Message: "Serialization failed"}, err
	}

	err = s.producer.Produce(&kafka.Message{
		TopicPartition: kafka.TopicPartition{Topic: &[]string{kafkaTopic}[0], Partition: kafka.PartitionAny},
		Value: bytes,
	}, nil)

	if err != nil {
		fmt.Printf("Kafka Error: %v\n", err)
		return &pb.IngestionResponse{Success: false, Message: "Kafka push failed"}, nil
	}

	// Marking as processed
	s.redisClient.SAdd(ctx, dedupKey, req.TransactionId)
	s.redisClient.Expire(ctx, dedupKey, 10*time.Minute)

	fmt.Printf("Received & Pushed: User=%d | Amt=%.2f\n", req.UserId, req.Amount)

	return &pb.IngestionResponse{Success: true, Message: "Stored in Kafka"}, nil
}

func main() {
	// KAFKA SETUP
	kafkaAddr := os.Getenv("KAFKA_BROKER")
	if kafkaAddr == "" {
		kafkaAddr = "localhost:9092"
	}
	p, err := kafka.NewProducer(&kafka.ConfigMap{"bootstrap.servers": kafkaAddr})
	if err != nil {
		log.Fatalf("Failed to create producer: %v", err)
	}
	defer p.Close()

	// REDIS CLUSTER SETUP
	client, _ := initialize_redis()

	
	// Starting TCP Listener
	lis, err := net.Listen("tcp", port)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	// Start gRPC Server
	s := grpc.NewServer()
	pb.RegisterFraudIngestionServer(s, &server{producer: p, redisClient: client})
	
	log.Printf("Go gRPC Server listening at %v", lis.Addr())
	if err := s.Serve(lis); err != nil {
		log.Fatalf("failed to serve: %v", err)
	}
}