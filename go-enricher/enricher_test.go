package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"testing"

	"github.com/confluentinc/confluent-kafka-go/kafka"
	"google.golang.org/protobuf/proto"

	pb "fraud/pb/proto/fraud/v1"
)

func rawMessage(value []byte) *kafka.Message {
	return &kafka.Message{
		TopicPartition: kafka.TopicPartition{Topic: stringPointer(fromKafkaTopic), Partition: 2, Offset: 41},
		Key:            []byte("user-7"), Value: value,
	}
}

func TestHandleMessageCommitsOnlyAfterOutputDelivery(t *testing.T) {
	var steps []string
	process := func(outcome string, err error) func(context.Context, *kafka.Message) (string, error) {
		return func(context.Context, *kafka.Message) (string, error) {
			steps = append(steps, "deliver")
			return outcome, err
		}
	}
	commit := func(commitErr error) func(*kafka.Message) ([]kafka.TopicPartition, error) {
		return func(*kafka.Message) ([]kafka.TopicPartition, error) {
			steps = append(steps, "commit")
			return nil, commitErr
		}
	}

	steps = nil
	if err := handleMessage(context.Background(), process(outcomeEnriched, nil), commit(nil), rawMessage(nil)); err != nil {
		t.Fatal(err)
	}
	if len(steps) != 2 || steps[0] != "deliver" || steps[1] != "commit" {
		t.Fatalf("want deliver then commit, got %v", steps)
	}

	steps = nil
	if err := handleMessage(context.Background(), process("", errors.New("redis down")), commit(nil), rawMessage(nil)); err == nil {
		t.Fatal("processing failure must stop the enricher")
	}
	if len(steps) != 1 {
		t.Fatalf("offset committed after a failed delivery: %v", steps)
	}

	steps = nil
	if err := handleMessage(context.Background(), process(outcomeDeadLetter, nil), commit(errors.New("rebalance")), rawMessage(nil)); err == nil {
		t.Fatal("commit failure must be returned")
	}
}

type capturedPublish struct {
	messages []*kafka.Message
	err      error
}

func (c *capturedPublish) publish(message *kafka.Message) error {
	c.messages = append(c.messages, message)
	return c.err
}

func TestPoisonMessagesAreDeadLetteredWithoutRedis(t *testing.T) {
	badIP, err := proto.Marshal(&pb.TransactionRequest{TransactionId: "t", UserId: "u", Amount: 1, IpAddress: "nope"})
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string][]byte{
		"invalid_protobuf":    {0x0a, 0xff},
		"invalid_transaction": badIP,
	}
	for reason, value := range cases {
		t.Run(reason, func(t *testing.T) {
			captured := &capturedPublish{}
			// No Redis client or GeoIP: rejecting input must not touch them.
			processor := transactionProcessor{publish: captured.publish}
			outcome, err := processor.process(context.Background(), rawMessage(value))
			if err != nil || outcome != outcomeDeadLetter {
				t.Fatalf("want dead_letter, got %q %v", outcome, err)
			}
			if len(captured.messages) != 1 || *captured.messages[0].TopicPartition.Topic != deadLetterTopic {
				t.Fatalf("expected one DLQ record, got %d", len(captured.messages))
			}
			message := captured.messages[0]
			var record map[string]any
			if err := json.Unmarshal(message.Value, &record); err != nil {
				t.Fatal(err)
			}
			if record["reason"] != reason || record["source_topic"] != fromKafkaTopic ||
				record["source_partition"] != float64(2) || record["source_offset"] != float64(41) ||
				record["payload_base64"] != base64.StdEncoding.EncodeToString(value) || string(message.Key) != "user-7" {
				t.Fatalf("DLQ record does not preserve the source: %v key=%q", record, message.Key)
			}
		})
	}
}

func TestDeadLetterDeliveryFailureBlocksCommit(t *testing.T) {
	processor := transactionProcessor{publish: (&capturedPublish{err: errors.New("broker down")}).publish}
	committed := false
	err := handleMessage(context.Background(), processor.process,
		func(*kafka.Message) ([]kafka.TopicPartition, error) { committed = true; return nil, nil },
		rawMessage([]byte{0x0a, 0xff}))
	if err == nil || committed {
		t.Fatalf("DLQ delivery failure must return before commit (err=%v committed=%t)", err, committed)
	}
}

// Real librdkafka against a closed port: the failure comes from the client's
// own delivery report.
func TestPublishSyncReportsBrokerDeliveryFailure(t *testing.T) {
	config := producerConfig("127.0.0.1:1")
	if err := config.SetKey("delivery.timeout.ms", 300); err != nil {
		t.Fatal(err)
	}
	producer, err := kafka.NewProducer(config)
	if err != nil {
		t.Fatal(err)
	}
	defer producer.Close()
	message, err := deadLetterMessage(rawMessage([]byte("x")), "invalid_protobuf")
	if err != nil {
		t.Fatal(err)
	}
	if err := publishSync(producer, message); err == nil {
		t.Fatal("expected a delivery error from an unreachable broker")
	}
}

func TestIsHostingProvider(t *testing.T) {
	if !isHostingProvider("Amazon.com, Inc.", defaultHostingKeywords) {
		t.Fatal("AWS organization should match")
	}
	if isHostingProvider("Telstra Corporation", defaultHostingKeywords) {
		t.Fatal("residential ISP should not match")
	}
}
