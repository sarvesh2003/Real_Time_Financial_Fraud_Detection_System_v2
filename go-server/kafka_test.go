package main

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/confluentinc/confluent-kafka-go/kafka"
)

// These use real librdkafka against a closed port, so the delivery report
// comes from the client library rather than from a fake.
func unreachableProducer(t *testing.T, deliveryTimeoutMs int) *kafka.Producer {
	t.Helper()
	config := producerConfig("127.0.0.1:1")
	if err := config.SetKey("delivery.timeout.ms", deliveryTimeoutMs); err != nil {
		t.Fatal(err)
	}
	producer, err := kafka.NewProducer(config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(producer.Close)
	return producer
}

func TestPublishReportsBrokerDeliveryFailure(t *testing.T) {
	publisher := kafkaPublisher{producer: unreachableProducer(t, 300), topic: kafkaTopic}
	err := publisher.Publish(context.Background(), []byte("u"), []byte("v"))
	if !errors.Is(err, errKafkaDelivery) {
		t.Fatalf("want errKafkaDelivery from the delivery report, got %v", err)
	}
}

func TestPublishReturnsContextErrorWhenCallerGivesUp(t *testing.T) {
	publisher := kafkaPublisher{producer: unreachableProducer(t, 10000), topic: kafkaTopic}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if err := publisher.Publish(ctx, []byte("u"), []byte("v")); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("want DeadlineExceeded (outcome unknown), got %v", err)
	}
}
