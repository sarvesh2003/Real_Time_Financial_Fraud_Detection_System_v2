package main

import (
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/confluentinc/confluent-kafka-go/kafka"
	"google.golang.org/protobuf/proto"

	pb "fraud-enricher/pb"
)

const (
	fromKafkaTopic = "raw_transactions"
	toKafkaTopic   = "enriched_transactions"
)

func main() {

	// Kafka Consumer setup
	kafkaAddr := os.Getenv("KAFKA_BROKER")
	if kafkaAddr == "" {
		kafkaAddr = "localhost:9092"
	}
	consumer, err := kafka.NewConsumer(&kafka.ConfigMap{
     "bootstrap.servers":    kafkaAddr,
     "group.id":             "foo",
     "auto.offset.reset":    "smallest",
	"enable.auto.commit": "false"})
	if err != nil {
		fmt.Println(err)
	}

	// Subscribe to Raw transactions Kafka topic
	kafkaConsumerTopics := os.Getenv("KAFKA_CONSUMER_TOPICS_ENRICHER")
	if kafkaConsumerTopics == "" {
		kafkaConsumerTopics = fromKafkaTopic
	}
	err = consumer.SubscribeTopics([]string {kafkaConsumerTopics}, nil)
	
	// Process message
	MIN_COMMIT_COUNT := 20
	msg_count := 0
	run := true

	// Handle Ctrl+C
	sigchan := make(chan os.Signal, 1)
	signal.Notify(sigchan, syscall.SIGINT, syscall.SIGTERM)
	fmt.Println("Starting Enricher Agent")
	fmt.Println("Initializing Redis Connections in Client")
	client, ctx := initialize_redis()
	time.Sleep(10 * time.Second)

	// Initializing MaxMindDB
	cityDb, asnDB, cleanUpDB, err := initialize_maxmindDB()
	if err != nil {
		fmt.Println("WARNING: SOME ISSUE WITH THE DB CONNECTIONS... RESOLVE THIS ASAP")
	}

	defer cleanUpDB()

	for run == true {
		select {

			case sig := <-sigchan:
			fmt.Printf("Caught signal %v: terminating\n", sig)
			run = false

			default:
				ev := consumer.Poll(100)
				
				if ev == nil {
					continue
				}

				switch e := ev.(type) {
				case *kafka.Message:
					msg_count += 1
					if msg_count % MIN_COMMIT_COUNT == 0 {
						consumer.Commit()
					}

					var txn pb.TransactionRequest
					err := proto.Unmarshal(e.Value, &txn)
					if err != nil {
						fmt.Println("Failed to Unmarshal")
						// Push to DLQ (to do later)
						continue;
					}
					log.Printf("Transaction values: IP Address=%s, Txn Id=%s, UserId=%d, Amount=%.2f", txn.IpAddress, txn.TransactionId, txn.UserId, txn.Amount)
					
					// Check if IP address is valid or not. If not valid push to DLQ and continue
					var is_valid_ip bool = validateIP(txn.IpAddress)
					
					// If valid, Check whether we have that entry in Redis few mins back, else add it
					if is_valid_ip {
						var geoData *GeoData
						// GEOIP ENRICHEMENT
						data, status, err := getGeoFromRedis(client, ctx, txn.IpAddress)
						if err != nil {
							fmt.Println("WARNING: SOMETHING IS WRONG WITH THE GET GEO FUNCTION...")
							continue
						} else {
							if status == "MISS" {
							// Not present in cache, so check maxmind and update cache
							cityRecord, asnRecord := maxMindDBLookup(txn.IpAddress, cityDb, asnDB)
							geoData = &GeoData{
								City:        cityRecord.City.Names["en"],
								Country:     cityRecord.Country.Names["en"],
								CountryCode: cityRecord.Country.IsoCode,
								Latitude:    cityRecord.Location.Latitude,
								Longitude:   cityRecord.Location.Longitude,
								ASN:         fmt.Sprintf("AS%d", asnRecord.AutonomousSystemNumber),
								ISP:         asnRecord.AutonomousSystemOrganization,
								IsHosting:   isHostingProvider(asnRecord.AutonomousSystemOrganization),
							}
							// Update cache
							status, err := setGeoToRedis(client, ctx, txn.IpAddress, *geoData)
							if err != nil {
								fmt.Println("SOME ISSUE WITH THE REDIS SET.......")
							} else {
								if status == "REDIS_SET_SUCCESS" {
									fmt.Println("REDIS SET SUCCESSFUL...........")
								}
							}
							
						} else if status == "HIT" {
							geoData = data
							fmt.Printf("FOUND IN REDIS CACHE....... sample data: city = %s", geoData.City)
						}
						
						// FRAUD METRICS ENRICHMENT
						fraudData, err := updateFraudInRedis(client, ctx, txn.IpAddress, float64(txn.Amount))
						if err != nil {
							log.Printf("Fraud update failed for IP %s: %v", txn.IpAddress, err)
							continue
						}

						log.Printf("ENRICHED_TXN ip=%s city=%s country=%s isp=%s hosting=%t txn_count_2h=%d total_2h=%.2f velocity=%.2f avg=%.2f max=%.2f",
							txn.IpAddress, 
							geoData.City,
							geoData.Country,
							geoData.ISP,
							geoData.IsHosting,
							fraudData.TxnCount,
							fraudData.TotalAmount,
							fraudData.AmountVelocity,
							fraudData.AvgAmount,
							fraudData.MaxAmount,
						)


						// Few suspicious patterns
						if fraudData.AmountVelocity > 50000 {
							log.Printf("HIGH VELOCITY ALERT: IP %s spending $%.2f/hour!", 
								txn.IpAddress, fraudData.AmountVelocity)
						}

						if fraudData.TxnCount > 20 {
							log.Printf("HIGH FREQUENCY ALERT: IP %s made %d transactions in 2h!", 
								txn.IpAddress, fraudData.TxnCount)
						}

						if fraudData.TotalAmount > 100000 {
							log.Printf("HIGH AMOUNT ALERT: IP %s spent $%.2f in 2h!", 
								txn.IpAddress, fraudData.TotalAmount)
						}

						// PUSH TO KAFKA AS PRODUCER

					}

					} else {
						// Push to DLQ
					}


				case kafka.PartitionEOF:
					fmt.Printf("%% Reached %v\n", e)
				case kafka.Error:
					fmt.Fprintf(os.Stderr, "%% Error: %v\n", e)
				default:
					fmt.Printf("Ignored %v\n", e)
				}
		}
		
	}
	consumer.Close()
}

