package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"

	"github.com/confluentinc/confluent-kafka-go/kafka"
	"github.com/oschwald/geoip2-golang"
	"github.com/redis/go-redis/v9"
	"google.golang.org/protobuf/proto"

	pb "fraud/pb/proto/fraud/v1"
)

type transactionProcessor struct {
	redis    *redis.ClusterClient
	producer *kafka.Producer
	cityDB   *geoip2.Reader
	asnDB    *geoip2.Reader
}

// process returns only after the output broker acknowledges delivery. The
// caller commits the raw offset after this function succeeds. Redis changes
// and Kafka delivery are not one transaction, so replay can republish output.
func (p *transactionProcessor) process(ctx context.Context, source *kafka.Message) (string, error) {
	var txn pb.TransactionRequest
	if err := proto.Unmarshal(source.Value, &txn); err != nil {
		return p.deadLetter(source, "invalid_protobuf")
	}
	if txn.TransactionId == "" || txn.UserId == "" || !validateIP(txn.IpAddress) ||
		math.IsNaN(txn.Amount) || math.IsInf(txn.Amount, 0) || txn.Amount < 0 {
		return p.deadLetter(source, "invalid_transaction")
	}

	geo, status, err := getGeoFromRedis(p.redis, ctx, txn.IpAddress)
	if err != nil {
		enricherErrorsTotal.WithLabelValues("geo_cache_read").Inc()
		return "", fmt.Errorf("read GeoIP cache: %w", err)
	}
	if status == "MISS" {
		city, asn, err := maxMindDBLookup(txn.IpAddress, p.cityDB, p.asnDB)
		if err != nil {
			enricherErrorsTotal.WithLabelValues("geo_database").Inc()
			return "", fmt.Errorf("GeoIP lookup: %w", err)
		}
		geo = &GeoData{
			City: city.City.Names["en"], Country: city.Country.Names["en"],
			CountryCode: city.Country.IsoCode,
			Latitude:    city.Location.Latitude, Longitude: city.Location.Longitude,
			ASN:       fmt.Sprintf("AS%d", asn.AutonomousSystemNumber),
			ISP:       asn.AutonomousSystemOrganization,
			IsHosting: isHostingProvider(asn.AutonomousSystemOrganization),
		}
		if _, err := setGeoToRedis(p.redis, ctx, txn.IpAddress, *geo); err != nil {
			enricherErrorsTotal.WithLabelValues("geo_cache_write").Inc()
			return "", fmt.Errorf("write GeoIP cache: %w", err)
		}
	}
	if geo == nil {
		return "", fmt.Errorf("GeoIP cache returned no value for %s", txn.IpAddress)
	}

	signals, duplicate, err := updateFraudInRedis(p.redis, ctx, txn.IpAddress, txn.Amount, txn.TransactionId)
	if err != nil {
		enricherErrorsTotal.WithLabelValues("fraud_window").Inc()
		return "", err
	}
	if duplicate {
		duplicateMessagesTotal.Inc()
	} else {
		logThresholdAlerts(txn.IpAddress, signals)
	}

	enriched := buildEnriched(&txn, geo, signals)
	payload, err := json.Marshal(enriched)
	if err != nil {
		enricherErrorsTotal.WithLabelValues("json_marshal").Inc()
		return "", fmt.Errorf("encode enriched transaction: %w", err)
	}
	if err := publishSync(p.producer, &kafka.Message{
		TopicPartition: kafka.TopicPartition{Topic: stringPointer(toKafkaTopic), Partition: kafka.PartitionAny},
		Key:            []byte(txn.TransactionId), Value: payload,
	}); err != nil {
		enricherErrorsTotal.WithLabelValues("kafka_delivery").Inc()
		return "", fmt.Errorf("deliver enriched transaction: %w", err)
	}
	log.Printf("enriched transaction_id=%s duplicate=%t count_2h=%d", txn.TransactionId, duplicate, signals.TxnCount)
	return "enriched", nil
}

func (p *transactionProcessor) deadLetter(source *kafka.Message, reason string) (string, error) {
	enricherErrorsTotal.WithLabelValues(reason).Inc()
	if err := publishDeadLetter(p.producer, source, reason); err != nil {
		return "", fmt.Errorf("deliver dead-letter record (%s): %w", reason, err)
	}
	log.Printf("dead-lettered raw offset=%v reason=%s", source.TopicPartition, reason)
	return "dead_letter", nil
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

func buildEnriched(txn *pb.TransactionRequest, geo *GeoData, signals *FraudSignals) EnrichedTransaction {
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
