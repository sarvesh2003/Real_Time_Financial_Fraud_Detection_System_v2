package main

import (
	"context"
	"testing"
	"time"

	dto "github.com/prometheus/client_model/go"
)

func durationHistogram(t *testing.T) (uint64, float64) {
	t.Helper()
	var metric dto.Metric
	if err := transactionDuration.Write(&metric); err != nil {
		t.Fatal(err)
	}
	return metric.GetHistogram().GetSampleCount(), metric.GetHistogram().GetSampleSum()
}

// The histogram must cover the whole handler, including the wait for the
// Kafka acknowledgment, not just the instant the handler was entered.
func TestRequestDurationIncludesKafkaAcknowledgment(t *testing.T) {
	const kafkaWait = 80 * time.Millisecond
	countBefore, sumBefore := durationHistogram(t)
	s := &server{limiter: &fakeLimiter{allowed: true}, publisher: &fakePublisher{delay: kafkaWait}}
	if _, err := s.SendTransaction(context.Background(), validRequest()); err != nil {
		t.Fatal(err)
	}
	countAfter, sumAfter := durationHistogram(t)
	if countAfter != countBefore+1 {
		t.Fatalf("want one observation, got %d", countAfter-countBefore)
	}
	if observed := sumAfter - sumBefore; observed < kafkaWait.Seconds() {
		t.Fatalf("observed %.4fs, want at least the %.3fs Kafka wait", observed, kafkaWait.Seconds())
	}
}
