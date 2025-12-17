package main

import (
	"context"
	"fmt"
	"log"
	"net"
	"os"

	"github.com/confluentinc/confluent-kafka-go/kafka"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"

	pb "fraud/pb"
)

const (
	port       = ":50051"
	kafkaTopic = "raw_transactions"
)

type server struct {
	pb.UnimplementedFraudIngestionServer
	producer *kafka.Producer
}

func (s *server) SendTransaction(ctx context.Context, req *pb.TransactionRequest) (*pb.IngestionResponse, error) {
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

	fmt.Printf("Received & Pushed: User=%d | Amt=%.2f\n", req.UserId, req.Amount)

	return &pb.IngestionResponse{Success: true, Message: "Stored in Kafka"}, nil
}

func main() {
	kafkaAddr := os.Getenv("KAFKA_BROKER")
	if kafkaAddr == "" {
		kafkaAddr = "localhost:9092"
	}
	p, err := kafka.NewProducer(&kafka.ConfigMap{"bootstrap.servers": kafkaAddr})
	if err != nil {
		log.Fatalf("Failed to create producer: %v", err)
	}
	defer p.Close()

	
	// Starting TCP Listener
	lis, err := net.Listen("tcp", port)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	// Start gRPC Server
	s := grpc.NewServer()
	pb.RegisterFraudIngestionServer(s, &server{producer: p})
	
	log.Printf("Go gRPC Server listening at %v", lis.Addr())
	if err := s.Serve(lis); err != nil {
		log.Fatalf("failed to serve: %v", err)
	}
}