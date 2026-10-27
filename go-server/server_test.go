package main

import (
	"context"
	"errors"
	"math"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "fraud/pb/proto/fraud/v1"
)

type fakeLimiter struct {
	allowed bool
	err     error
	calls   int
}

func (l *fakeLimiter) Allow(context.Context, string) (bool, error) {
	l.calls++
	return l.allowed, l.err
}

type fakePublisher struct {
	err   error
	delay time.Duration
	key   []byte
	value []byte
	calls int
}

func (p *fakePublisher) Publish(ctx context.Context, key, value []byte) error {
	p.calls++
	p.key, p.value = key, value
	if p.delay > 0 {
		select {
		case <-time.After(p.delay):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return p.err
}

func validRequest() *pb.TransactionRequest {
	return &pb.TransactionRequest{TransactionId: "t-1", UserId: "u-1", Amount: 12.5, IpAddress: "203.0.113.7"}
}

func code(err error) codes.Code { return status.Code(err) }

func TestSendTransactionAcknowledgesAfterPublish(t *testing.T) {
	limiter, publisher := &fakeLimiter{allowed: true}, &fakePublisher{}
	response, err := (&server{limiter: limiter, publisher: publisher}).SendTransaction(context.Background(), validRequest())
	if err != nil || !response.GetSuccess() {
		t.Fatalf("expected success, got %v %v", response, err)
	}
	if string(publisher.key) != "u-1" {
		t.Fatalf("raw record must be keyed by user_id, got %q", publisher.key)
	}
	var sent pb.TransactionRequest
	if err := proto.Unmarshal(publisher.value, &sent); err != nil || !proto.Equal(&sent, validRequest()) {
		t.Fatalf("raw payload is not the request protobuf: %v", err)
	}
}

func TestSendTransactionRejectsInvalidInputBeforeRedisOrKafka(t *testing.T) {
	cases := map[string]func(*pb.TransactionRequest){
		"empty id":        func(r *pb.TransactionRequest) { r.TransactionId = "" },
		"empty user":      func(r *pb.TransactionRequest) { r.UserId = "" },
		"bad ip":          func(r *pb.TransactionRequest) { r.IpAddress = "not-an-ip" },
		"nan amount":      func(r *pb.TransactionRequest) { r.Amount = math.NaN() },
		"inf amount":      func(r *pb.TransactionRequest) { r.Amount = math.Inf(1) },
		"negative amount": func(r *pb.TransactionRequest) { r.Amount = -0.01 },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			limiter, publisher := &fakeLimiter{allowed: true}, &fakePublisher{}
			request := validRequest()
			mutate(request)
			_, err := (&server{limiter: limiter, publisher: publisher}).SendTransaction(context.Background(), request)
			if code(err) != codes.InvalidArgument {
				t.Fatalf("expected InvalidArgument, got %v", err)
			}
			if limiter.calls != 0 || publisher.calls != 0 {
				t.Fatalf("invalid input reached Redis (%d) or Kafka (%d)", limiter.calls, publisher.calls)
			}
		})
	}
}

func TestSendTransactionRateLimitOutcomes(t *testing.T) {
	cases := []struct {
		name    string
		limiter *fakeLimiter
		want    codes.Code
	}{
		{"redis error", &fakeLimiter{err: errors.New("CLUSTERDOWN")}, codes.Unavailable},
		{"bucket empty", &fakeLimiter{allowed: false}, codes.ResourceExhausted},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			publisher := &fakePublisher{}
			_, err := (&server{limiter: test.limiter, publisher: publisher}).SendTransaction(context.Background(), validRequest())
			if code(err) != test.want || publisher.calls != 0 {
				t.Fatalf("want %v without publish, got %v after %d publishes", test.want, err, publisher.calls)
			}
		})
	}
}

func TestSendTransactionKafkaOutcomes(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want codes.Code
	}{
		{"enqueue", errKafkaEnqueue, codes.Unavailable},
		{"delivery", errKafkaDelivery, codes.Unavailable},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			s := &server{limiter: &fakeLimiter{allowed: true}, publisher: &fakePublisher{err: test.err}}
			if _, err := s.SendTransaction(context.Background(), validRequest()); code(err) != test.want {
				t.Fatalf("want %v, got %v", test.want, err)
			}
		})
	}
}

func TestSendTransactionDeadlineIsReportedAsAmbiguous(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	s := &server{limiter: &fakeLimiter{allowed: true}, publisher: &fakePublisher{delay: time.Second}}
	if _, err := s.SendTransaction(ctx, validRequest()); code(err) != codes.DeadlineExceeded {
		t.Fatalf("want DeadlineExceeded, got %v", err)
	}
}
