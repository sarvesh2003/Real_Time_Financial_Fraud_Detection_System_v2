package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/confluentinc/confluent-kafka-go/kafka"
)

const (
	fromKafkaTopic = "raw_transactions"
	toKafkaTopic   = "enriched_transactions"
	geoIPDir       = "/data/geoip"
)

var defaultHostingKeywords = []string{
	"amazon", "aws", "google", "azure", "microsoft",
	"digitalocean", "ovh", "hetzner", "linode",
	"vultr", "cloudflare", "hosting", "datacenter",
	"vpn", "proxy", "colocation",
}

type enricherConfig struct {
	broker          string
	group           string
	offsetReset     string
	geoCacheTTL     time.Duration
	hostingKeywords []string
}

func configFromEnv() (enricherConfig, error) {
	config := enricherConfig{
		broker:          envOr("KAFKA_BROKER", "localhost:9092"),
		group:           envOr("KAFKA_CONSUMER_GROUP", "fraud-enricher"),
		offsetReset:     envOr("KAFKA_OFFSET_RESET", "earliest"),
		geoCacheTTL:     24 * time.Hour,
		hostingKeywords: defaultHostingKeywords,
	}
	if config.offsetReset != "earliest" && config.offsetReset != "latest" && config.offsetReset != "error" {
		return config, fmt.Errorf("invalid KAFKA_OFFSET_RESET %q", config.offsetReset)
	}
	if value := os.Getenv("GEO_TTL_HOURS"); value != "" {
		hours, err := strconv.Atoi(value)
		if err != nil || hours < 1 {
			// Zero would cache forever in Redis; reject rather than guess.
			return config, fmt.Errorf("GEO_TTL_HOURS must be a positive integer, got %q", value)
		}
		config.geoCacheTTL = time.Duration(hours) * time.Hour
	}
	if value := os.Getenv("HOSTING_KEYWORDS"); value != "" {
		config.hostingKeywords = nil
		for _, keyword := range strings.Split(value, ",") {
			if keyword = strings.ToLower(strings.TrimSpace(keyword)); keyword != "" {
				config.hostingKeywords = append(config.hostingKeywords, keyword)
			}
		}
	}
	return config, nil
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func main() {
	if err := run(); err != nil {
		log.Fatalf("enricher stopped: %v", err)
	}
}

func run() error {
	config, err := configFromEnv()
	if err != nil {
		return err
	}

	client, err := initializeRedis()
	if err != nil {
		return err
	}
	defer client.Close()

	geo, err := openGeoIP(geoIPDir, config.hostingKeywords)
	if err != nil {
		return fmt.Errorf("initialize GeoIP: %w", err)
	}
	defer geo.Close()

	producer, err := kafka.NewProducer(producerConfig(config.broker))
	if err != nil {
		return fmt.Errorf("create Kafka producer: %w", err)
	}
	defer producer.Close()
	go logProducerErrors(producer)

	consumer, err := kafka.NewConsumer(&kafka.ConfigMap{
		"bootstrap.servers":        config.broker,
		"group.id":                 config.group,
		"auto.offset.reset":        config.offsetReset,
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
	processor := transactionProcessor{
		redis: client, geo: geo, geoCacheTTL: config.geoCacheTTL,
		publish: func(message *kafka.Message) error { return publishSync(producer, message) },
	}
	log.Printf("enricher ready: topic=%s group=%s", fromKafkaTopic, config.group)

	for ctx.Err() == nil {
		switch event := consumer.Poll(100).(type) {
		case *kafka.Message:
			if err := handleMessage(ctx, processor.process, consumer.CommitMessage, event); err != nil {
				return err
			}
		case kafka.Error:
			log.Printf("Kafka consumer error: %v", event)
		}
	}
	log.Printf("enricher stopped after signal")
	return nil
}

// handleMessage commits the raw offset only after process has delivered the
// enriched or dead-letter record. On any error it returns before the commit;
// main exits, Compose restarts the enricher, and the message is replayed.
func handleMessage(
	ctx context.Context,
	process func(context.Context, *kafka.Message) (string, error),
	commit func(*kafka.Message) ([]kafka.TopicPartition, error),
	message *kafka.Message,
) error {
	started := time.Now()
	outcome, err := process(ctx, message)
	enrichmentDuration.Observe(time.Since(started).Seconds())
	if err != nil {
		enricherErrorsTotal.WithLabelValues("processing").Inc()
		return fmt.Errorf("process offset %v before commit: %w", message.TopicPartition, err)
	}
	if _, err := commit(message); err != nil {
		return fmt.Errorf("commit offset %v after %s delivery: %w", message.TopicPartition, outcome, err)
	}
	if outcome == outcomeEnriched {
		messagesProcessedTotal.Inc()
	}
	return nil
}
