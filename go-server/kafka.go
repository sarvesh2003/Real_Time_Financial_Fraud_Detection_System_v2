package main

import (
	"context"
	"errors"
	"fmt"
	"log"

	"github.com/confluentinc/confluent-kafka-go/kafka"
)

var (
	errKafkaEnqueue  = errors.New("Kafka enqueue failed")
	errKafkaDelivery = errors.New("Kafka delivery failed")
)

func producerConfig(broker string) *kafka.ConfigMap {
	return &kafka.ConfigMap{
		"bootstrap.servers":   broker,
		"enable.idempotence":  true,
		"acks":                "all",
		"delivery.timeout.ms": 10000,
	}
}

type kafkaPublisher struct {
	producer *kafka.Producer
	topic    string
}

// Publish returns only after the broker's delivery report. Produce merely
// queues the record locally. If ctx ends first the record may still be
// delivered later, so the caller cannot know the outcome.
func (p kafkaPublisher) Publish(ctx context.Context, key, value []byte) error {
	delivery := make(chan kafka.Event, 1)
	err := p.producer.Produce(&kafka.Message{
		TopicPartition: kafka.TopicPartition{Topic: &p.topic, Partition: kafka.PartitionAny},
		Key:            key,
		Value:          value,
	}, delivery)
	if err != nil {
		return fmt.Errorf("%w: %v", errKafkaEnqueue, err)
	}
	select {
	case event := <-delivery:
		message, ok := event.(*kafka.Message)
		if !ok {
			return fmt.Errorf("%w: unexpected event %T", errKafkaDelivery, event)
		}
		if message.TopicPartition.Error != nil {
			return fmt.Errorf("%w: %v", errKafkaDelivery, message.TopicPartition.Error)
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

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
