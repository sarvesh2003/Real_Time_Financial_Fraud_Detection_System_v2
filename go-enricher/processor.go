package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	"github.com/confluentinc/confluent-kafka-go/kafka"
	"github.com/redis/go-redis/v9"
	"google.golang.org/protobuf/proto"

	"fraud/internal/validation"
	pb "fraud/pb/proto/fraud/v1"
)

const (
	outcomeEnriched   = "enriched"
	outcomeDeadLetter = "dead_letter"
)

type transactionProcessor struct {
	redis       *redis.ClusterClient
	geo         *geoIP
	geoCacheTTL time.Duration
	// publish returns after the broker acknowledges the record (publishSync).
	publish func(*kafka.Message) error
}

// process returns only after the output broker acknowledges delivery. The
// caller commits the raw offset after this function succeeds. Redis changes
// and Kafka delivery are not one transaction, so replay can republish output.
func (p *transactionProcessor) process(ctx context.Context, source *kafka.Message) (string, error) {
	var txn pb.TransactionRequest
	if err := proto.Unmarshal(source.Value, &txn); err != nil {
		return p.deadLetter(source, "invalid_protobuf")
	}
	if err := validation.Transaction(&txn); err != nil {
		return p.deadLetter(source, "invalid_transaction")
	}

	geo, err := p.geoFor(ctx, txn.IpAddress)
	if err != nil {
		return "", err
	}

	signals, duplicate, err := updateFraudInRedis(ctx, p.redis, txn.IpAddress, txn.Amount, txn.TransactionId)
	if err != nil {
		enricherErrorsTotal.WithLabelValues("fraud_window").Inc()
		return "", err
	}
	if duplicate {
		duplicateMessagesTotal.Inc()
	} else {
		logThresholdAlerts(txn.IpAddress, signals)
	}

	payload, err := json.Marshal(buildEnriched(&txn, geo, signals))
	if err != nil {
		enricherErrorsTotal.WithLabelValues("json_marshal").Inc()
		return "", fmt.Errorf("encode enriched transaction: %w", err)
	}
	if err := p.publish(&kafka.Message{
		TopicPartition: kafka.TopicPartition{Topic: stringPointer(toKafkaTopic), Partition: kafka.PartitionAny},
		Key:            []byte(txn.TransactionId), Value: payload,
	}); err != nil {
		enricherErrorsTotal.WithLabelValues("kafka_delivery").Inc()
		return "", fmt.Errorf("deliver enriched transaction: %w", err)
	}
	log.Printf("enriched transaction_id=%s duplicate=%t count_2h=%d", txn.TransactionId, duplicate, signals.TxnCount)
	return outcomeEnriched, nil
}

// geoFor reads the Redis GeoIP cache and falls back to MaxMind on a miss.
func (p *transactionProcessor) geoFor(ctx context.Context, ip string) (GeoData, error) {
	geo, found, err := getGeoFromRedis(ctx, p.redis, ip)
	if err != nil {
		enricherErrorsTotal.WithLabelValues("geo_cache_read").Inc()
		return GeoData{}, fmt.Errorf("read GeoIP cache: %w", err)
	}
	if found {
		return geo, nil
	}
	geo, err = p.geo.lookup(ip)
	if err != nil {
		enricherErrorsTotal.WithLabelValues("geo_database").Inc()
		return GeoData{}, fmt.Errorf("GeoIP lookup: %w", err)
	}
	if err := setGeoToRedis(ctx, p.redis, ip, geo, p.geoCacheTTL); err != nil {
		enricherErrorsTotal.WithLabelValues("geo_cache_write").Inc()
		return GeoData{}, fmt.Errorf("write GeoIP cache: %w", err)
	}
	return geo, nil
}

func (p *transactionProcessor) deadLetter(source *kafka.Message, reason string) (string, error) {
	enricherErrorsTotal.WithLabelValues(reason).Inc()
	message, err := deadLetterMessage(source, reason)
	if err != nil {
		return "", err
	}
	if err := p.publish(message); err != nil {
		return "", fmt.Errorf("deliver dead-letter record (%s): %w", reason, err)
	}
	log.Printf("dead-lettered raw offset=%v reason=%s", source.TopicPartition, reason)
	return outcomeDeadLetter, nil
}

func logThresholdAlerts(ip string, signals *FraudSignals) {
	if signals.AmountVelocity > 50000 {
		log.Printf("HIGH VELOCITY ALERT: IP %s spending $%.2f/hour", ip, signals.AmountVelocity)
	}
	if signals.TxnCount > 20 {
		log.Printf("HIGH FREQUENCY ALERT: IP %s made %d transactions in 2h", ip, signals.TxnCount)
	}
	if signals.TotalAmount > 100000 {
		log.Printf("HIGH AMOUNT ALERT: IP %s spent $%.2f in 2h", ip, signals.TotalAmount)
	}
}

func buildEnriched(txn *pb.TransactionRequest, geo GeoData, signals *FraudSignals) EnrichedTransaction {
	return EnrichedTransaction{
		TransactionID: txn.TransactionId, UserID: txn.UserId, Amount: txn.Amount,
		Timestamp: txn.Timestamp, IsFraud: txn.IsFraud, Type: txn.Type,
		OldBalanceOrig: txn.OldBalanceOrig, NewBalanceOrig: txn.NewBalanceOrig,
		OldBalanceDest: txn.OldBalanceDest, NewBalanceDest: txn.NewBalanceDest,
		IsUnauthorizedOverdraft: txn.IsUnauthorizedOverdraft,
		IPAddress:               txn.IpAddress, City: geo.City, Country: geo.Country,
		CountryCode: geo.CountryCode, Latitude: geo.Latitude, Longitude: geo.Longitude,
		ASN: geo.ASN, ISP: geo.ISP, IsHosting: geo.IsHosting,
		TxnCount2h: signals.TxnCount, TotalAmount2h: signals.TotalAmount,
		AmountVelocity: signals.AmountVelocity, AvgAmount2h: signals.AvgAmount,
		MaxAmount2h: signals.MaxAmount,
	}
}
