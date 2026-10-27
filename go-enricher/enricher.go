package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/confluentinc/confluent-kafka-go/kafka"
)

const (
	fromKafkaTopic = "raw_transactions"
	toKafkaTopic   = "enriched_transactions"
)

func main() {
	if err := run(); err != nil {
		log.Fatalf("enricher stopped: %v", err)
	}
}

func run() error {
	broker := os.Getenv("KAFKA_BROKER")
	if broker == "" {
		broker = "localhost:9092"
	}
	group := os.Getenv("KAFKA_CONSUMER_GROUP")
	if group == "" {
		group = "fraud-enricher"
	}
	offsetReset := os.Getenv("KAFKA_OFFSET_RESET")
	if offsetReset == "" {
		offsetReset = "earliest"
	}
	if offsetReset != "earliest" && offsetReset != "latest" && offsetReset != "error" {
		return fmt.Errorf("invalid KAFKA_OFFSET_RESET %q", offsetReset)
	}

	client, err := initializeRedis()
	if err != nil {
		return err
	}
	defer client.Close()

	cityDB, asnDB, closeDB, err := initialize_maxmindDB()
	if err != nil {
		return fmt.Errorf("initialize GeoIP: %w", err)
	}
	defer closeDB()

	producer, err := kafka.NewProducer(&kafka.ConfigMap{
		"bootstrap.servers":   broker,
		"enable.idempotence":  true,
		"acks":                "all",
		"delivery.timeout.ms": 10000,
	})
	if err != nil {
		return fmt.Errorf("create Kafka producer: %w", err)
	}
	defer producer.Close()

	consumer, err := kafka.NewConsumer(&kafka.ConfigMap{
		"bootstrap.servers":        broker,
		"group.id":                 group,
		"auto.offset.reset":        offsetReset,
		"enable.auto.commit":       false,
		"enable.auto.offset.store": false,
	})
	if err != nil {
		return fmt.Errorf("create Kafka consumer: %w", err)
	}
	defer consumer.Close()
	if err := consumer.SubscribeTopics([]string{fromKafkaTopic}, nil); err != nil {
		return fmt.Errorf("subscribe to %s: %w", fromKafkaTopic, err)
	}

	startMetricsServer()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	processor := transactionProcessor{redis: client, producer: producer, cityDB: cityDB, asnDB: asnDB}
	log.Printf("enricher ready: topic=%s group=%s", fromKafkaTopic, group)

	for ctx.Err() == nil {
		event := consumer.Poll(100)
		if event == nil {
			continue
		}
		switch message := event.(type) {
		case *kafka.Message:
			started := time.Now()
			outcome, err := processor.process(ctx, message)
			enrichmentDuration.Observe(time.Since(started).Seconds())
			if err != nil {
				enricherErrorsTotal.WithLabelValues("processing").Inc()
				return fmt.Errorf("process offset %v before commit: %w", message.TopicPartition, err)
			}
			if _, err := consumer.CommitMessage(message); err != nil {
				return fmt.Errorf("commit offset %v after %s delivery: %w", message.TopicPartition, outcome, err)
			}
			if outcome == "enriched" {
				messagesProcessedTotal.Inc()
			}
		case kafka.Error:
			log.Printf("Kafka consumer error: %v", message)
		}
	}
	log.Printf("enricher stopped after signal")
	return nil
}
