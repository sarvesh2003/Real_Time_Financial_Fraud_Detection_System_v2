package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"time"

	"github.com/confluentinc/confluent-kafka-go/kafka"
)

const deadLetterTopic = "raw_transactions_dlq"

func producerConfig(broker string) *kafka.ConfigMap {
	return &kafka.ConfigMap{
		"bootstrap.servers":   broker,
		"enable.idempotence":  true,
		"acks":                "all",
		"delivery.timeout.ms": 10000,
	}
}

// A successful Produce call only means that librdkafka queued the message.
// The delivery event confirms that the broker accepted it.
func publishSync(producer *kafka.Producer, message *kafka.Message) error {
	delivery := make(chan kafka.Event, 1)
	if err := producer.Produce(message, delivery); err != nil {
		return fmt.Errorf("enqueue Kafka message: %w", err)
	}
	select {
	case event := <-delivery:
		result, ok := event.(*kafka.Message)
		if !ok {
			return fmt.Errorf("unexpected Kafka delivery event %T", event)
		}
		if result.TopicPartition.Error != nil {
			return fmt.Errorf("deliver Kafka message: %w", result.TopicPartition.Error)
		}
		return nil
	case <-time.After(15 * time.Second):
		return fmt.Errorf("Kafka delivery timed out; broker outcome is unknown")
	}
}

// deadLetterMessage wraps the unmodified raw bytes with their source position.
func deadLetterMessage(source *kafka.Message, reason string) (*kafka.Message, error) {
	record := struct {
		Reason          string `json:"reason"`
		SourceTopic     string `json:"source_topic"`
		SourcePartition int32  `json:"source_partition"`
		SourceOffset    int64  `json:"source_offset"`
		PayloadBase64   string `json:"payload_base64"`
	}{
		Reason:          reason,
		SourceTopic:     *source.TopicPartition.Topic,
		SourcePartition: source.TopicPartition.Partition,
		SourceOffset:    int64(source.TopicPartition.Offset),
		PayloadBase64:   base64.StdEncoding.EncodeToString(source.Value),
	}
	payload, err := json.Marshal(record)
	if err != nil {
		return nil, fmt.Errorf("encode dead-letter record: %w", err)
	}
	return &kafka.Message{
		TopicPartition: kafka.TopicPartition{Topic: stringPointer(deadLetterTopic), Partition: kafka.PartitionAny},
		Key:            source.Key,
		Value:          payload,
	}, nil
}

func stringPointer(value string) *string { return &value }

// logProducerErrors drains client-level events. Per-record delivery reports
// go to their own channels; without this, broker errors would queue unread
// on Events() and never reach the service log.
func logProducerErrors(producer *kafka.Producer) {
	for event := range producer.Events() {
		if err, ok := event.(kafka.Error); ok {
			log.Printf("Kafka producer error: %v", err)
		}
	}
}
