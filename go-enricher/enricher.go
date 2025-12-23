package main

import (
	"fmt"
	"os"
	"os/signal"
	"log"
	"syscall"
    "github.com/confluentinc/confluent-kafka-go/kafka"
	"google.golang.org/protobuf/proto"

	pb "fraud-enricher/pb"
)



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
					log.Printf("Transaction values: Txn Id=%s, UserId=%d, Amount=%.2f", txn.TransactionId, txn.UserId, txn.Amount)

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


	// Check if IP address is valid or not. If not valid push to DLQ and continue

	// Check whether we have that entry in Redis few mins back

	// If not in Redis, Use maxmind and enrich it

	// Push the enriched txn to Enriched transaction Kafka topic

}

