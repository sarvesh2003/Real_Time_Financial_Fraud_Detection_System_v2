package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/signal"
	"log"
	"syscall"
	"time"
    "github.com/confluentinc/confluent-kafka-go/kafka"
	"google.golang.org/protobuf/proto"
	"github.com/redis/go-redis/v9"

	pb "fraud-enricher/pb"
)

func validateIP(ip string) bool {
	  ans := net.ParseIP(ip)
	  if ans == nil {
		return false
	  }
	  return true
	  
}

func initialize_redis() *redis.ClusterClient {
	client := redis.NewClusterClient(&redis.ClusterOptions{
		Addrs: []string{
			"192.168.240.100:6379",
			"192.168.240.101:6379",
			"192.168.240.102:6379",
		},
		RouteByLatency: true,
	})

	ctx := context.Background()
	fmt.Println("Attempting to connect to Redis Cluster...")

    // Retry
	for i := 0; i < 30; i++ {
		err := client.Set(ctx, "health_check", "ok", 5*time.Second).Err()

		if err == nil {
			fmt.Println("SUCCESS: Connected to Redis Cluster!")
			break
		}

		fmt.Printf("Waiting for Cluster to stabilize (Attempt %d/30): %v\n", i+1, err)
		time.Sleep(2 * time.Second)
	}

	val, err := client.Get(ctx, "health_check").Result()
	if err != nil {
		fmt.Printf("Warning: Cluster might still be unstable: %v\n", err)
	} else {
		fmt.Println("Health check verify value:", val)
	}

	return client
}

func main() {

	// Kafka Consumer setup
	kafkaAddr := os.Getenv("KAFKA_BROKER")
	if kafkaAddr == "" {
		kafkaAddr = "localhost:9092"
	}
	consumer, err := kafka.NewConsumer(&kafka.ConfigMap{
     "bootstrap.servers":    kafkaAddr,
     "group.id":             "foo",
     "auto.offset.reset":    "smallest",
	"enable.auto.commit": "false"})
	if err != nil {
		fmt.Println(err)
	}

	// Subscribe to Raw transactions Kafka topic
	kafkaConsumerTopics := os.Getenv("KAFKA_CONSUMER_TOPICS_ENRICHER")
	if kafkaConsumerTopics == "" {
		kafkaConsumerTopics = "raw_transactions"
	}
	err = consumer.SubscribeTopics([]string {kafkaConsumerTopics}, nil)
	
	// Process message
	MIN_COMMIT_COUNT := 20
	msg_count := 0
	run := true

	// Handle Ctrl+C
	sigchan := make(chan os.Signal, 1)
	signal.Notify(sigchan, syscall.SIGINT, syscall.SIGTERM)
	fmt.Println("Starting Enricher Agent")
	fmt.Println("Initializing Redis Connections in Client")
	client := initialize_redis()
	ctx := context.Background()
	time.Sleep(10 * time.Second)

	err = client.Set(ctx, "foo", "bar", 0).Err()
	if err != nil {
		panic(err)
	}

	for run == true {
		select {

			case sig := <-sigchan:
			fmt.Printf("Caught signal %v: terminating\n", sig)
			run = false

			default:
				ev := consumer.Poll(100)
				
				if ev == nil {
					continue
				}

				switch e := ev.(type) {
				case *kafka.Message:
					msg_count += 1
					if msg_count % MIN_COMMIT_COUNT == 0 {
						consumer.Commit()
					}

					var txn pb.TransactionRequest
					err := proto.Unmarshal(e.Value, &txn)
					if err != nil {
						fmt.Println("Failed to Unmarshal")
						// Push to DLQ (to do later)
						continue;
					}
					log.Printf("Transaction values: IP Address=%s, Txn Id=%s, UserId=%d, Amount=%.2f", txn.IpAddress, txn.TransactionId, txn.UserId, txn.Amount)
					
					// Check if IP address is valid or not. If not valid push to DLQ and continue
					var is_valid_ip bool = validateIP(txn.IpAddress)
					
					// If valid, Check whether we have that entry in Redis few mins back, else add it
					if is_valid_ip {
						
					}


				case kafka.PartitionEOF:
					fmt.Printf("%% Reached %v\n", e)
				case kafka.Error:
					fmt.Fprintf(os.Stderr, "%% Error: %v\n", e)
				default:
					fmt.Printf("Ignored %v\n", e)
				}
		}
		
	}
	consumer.Close()

	

	// If not in Redis, Use maxmind and enrich it

	// Push the enriched txn to Enriched transaction Kafka topic

}

