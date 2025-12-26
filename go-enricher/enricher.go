package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"os/signal"
	"log"
	"strconv"
	"strings"
	"syscall"
	"time"
    "github.com/confluentinc/confluent-kafka-go/kafka"
	"google.golang.org/protobuf/proto"
	"github.com/redis/go-redis/v9"
	"github.com/oschwald/geoip2-golang"

	pb "fraud-enricher/pb"
)

type GeoData struct {
	City        string  `json:"city"`
	Country     string  `json:"country"`
	CountryCode string  `json:"country_code"`
	Latitude    float64 `json:"lat"`
	Longitude   float64 `json:"lon"`
	ASN         string  `json:"asn"`
	ISP         string  `json:"isp"`
	IsHosting   bool    `json:"is_hosting"`
}

type FraudSignals struct {
	FirstSeen      time.Time `json:"first_seen"`
	LastSeen       time.Time `json:"last_seen"`
	TxnCount       int       `json:"txn_count"`
	TotalAmount    float64   `json:"total_amount"`
	AmountVelocity float64   `json:"amount_velocity"`
	AvgAmount      float64   `json:"avg_amount"`
	MaxAmount      float64   `json:"max_amount"`
}

func isHostingProvider(isp string) bool {
	keywordsEnv := os.Getenv("HOSTING_KEYWORDS")
	var keywords []string
	
	if keywordsEnv != "" {
		keywords = strings.Split(keywordsEnv, ",")
	} else {
		keywords = []string{
			"amazon", "aws", "google", "azure", "microsoft",
			"digitalocean", "ovh", "hetzner", "linode",
			"vultr", "cloudflare", "hosting", "datacenter",
			"vpn", "proxy", "colocation",
		}
	}
	
	ispLower := strings.ToLower(isp)
	for _, keyword := range keywords {
		if strings.Contains(ispLower, strings.TrimSpace(keyword)) {
			return true
		}
	}
	return false
}

func getGeoFromRedis(client *redis.ClusterClient, ctx context.Context, ip string) (*GeoData, string, error) {
	geoIp := "geo:" + ip
	value, err := client.Get(ctx, geoIp).Result()
	if err == redis.Nil {
		// Entry not found
		return nil, "MISS", nil
	} else if err != nil {
		// Some issue
		return nil, "REDDIS_ISSUE", err
	}
	geo := &GeoData{}
	err = json.Unmarshal([]byte(value), geo)
	if err != nil {
		return nil, "UNMARSHALING_ISSUE", err
	}

	return geo, "HIT", nil
}

func setGeoToRedis(client *redis.ClusterClient, ctx context.Context, ip string, geodata GeoData) (string, error) {
	geoIp := "geo:" + ip
	geoJson, err := json.Marshal(geodata)
	if err != nil {
		return "MARSHAL_ISSUE", err
	}
	ttlHours := 24
	if ttlEnv := os.Getenv("GEO_TTL_HOURS"); ttlEnv != "" {
		if parsed, err := strconv.Atoi(ttlEnv); err == nil {
			ttlHours = parsed
		}
	}
	err = client.Set(ctx, geoIp, geoJson, time.Duration(ttlHours)*time.Hour).Err()
	if err != nil {
		return "REDIS_SET_FAILED", err
	}
	return "REDIS_SET_SUCCESS", nil
}

func getFraudFromRedis(client *redis.ClusterClient, ctx context.Context, ip string) (*FraudSignals, string, error) {
	fraudKey := "fraud:" + ip
	value, err := client.Get(ctx, fraudKey).Result()
	
	if err == redis.Nil {
		return nil, "MISS", nil
	} else if err != nil {
		return nil, "REDIS_ISSUE", err
	}
	
	fraud := &FraudSignals{}
	err = json.Unmarshal([]byte(value), fraud)
	if err != nil {
		return nil, "UNMARSHAL_ISSUE", err
	}
	
	return fraud, "HIT", nil
}

func updateFraudInRedis(client *redis.ClusterClient, ctx context.Context, ip string, amount float64) (*FraudSignals, error) {
	fraudKey := "fraud:" + ip	
	fraud, status, err := getFraudFromRedis(client, ctx, ip)
	
	if status == "MISS" {
		fraud = &FraudSignals{
			FirstSeen:      time.Now(),
			LastSeen:       time.Now(),
			TxnCount:       1,
			TotalAmount:    amount,
			AmountVelocity: 0,
			AvgAmount:      amount,
			MaxAmount:      amount,
		}
		log.Printf("NEW IP in fraud tracking: %s (Amount: $%.2f)", ip, amount)
		
	} else if err != nil {
		return nil, fmt.Errorf("redis get failed: %w", err)
		
	} else {		
		fraud.LastSeen = time.Now()
		fraud.TxnCount++
		fraud.TotalAmount += amount

		duration := time.Since(fraud.FirstSeen).Hours()
		if duration > 0 {
			fraud.AmountVelocity = fraud.TotalAmount / duration
		}
		
		fraud.AvgAmount = fraud.TotalAmount / float64(fraud.TxnCount)
		if amount > fraud.MaxAmount {
			fraud.MaxAmount = amount
		}
		
		log.Printf("UPDATED fraud data: IP=%s | Count=%d | Total=$%.2f | Velocity=$%.2f/h",
			ip, fraud.TxnCount, fraud.TotalAmount, fraud.AmountVelocity)
	}
	
	fraudJSON, err := json.Marshal(fraud)
	if err != nil {
		return nil, fmt.Errorf("marshal failed: %w", err)
	}

	err = client.Set(ctx, fraudKey, fraudJSON, 2*time.Hour).Err()
	if err != nil {
		return nil, fmt.Errorf("redis set failed: %w", err)
	}
	
	return fraud, nil
}

func validateIP(ip string) bool {
	  ans := net.ParseIP(ip)
	  if ans == nil {
		return false
	  }
	  return true
}

func initialize_redis() (*redis.ClusterClient, context.Context) {
	client := redis.NewClusterClient(&redis.ClusterOptions{
		Addrs: []string{
			"192.168.240.100:6379",
			"192.168.240.101:6379",
			"192.168.240.102:6379",
		},
		RouteByLatency: true,
	})

	ctx := context.Background()
	fmt.Println("Attempting to connect to Redis Cluster...")

    // Retry
	for i := 0; i < 30; i++ {
		err := client.Set(ctx, "health_check", "ok", 5*time.Second).Err()

		if err == nil {
			fmt.Println("SUCCESS: Connected to Redis Cluster!")
			break
		}

		fmt.Printf("Waiting for Cluster to stabilize (Attempt %d/30): %v\n", i+1, err)
		time.Sleep(2 * time.Second)
	}

	val, err := client.Get(ctx, "health_check").Result()
	if err != nil {
		fmt.Printf("Warning: Cluster might still be unstable: %v\n", err)
	} else {
		fmt.Println("Health check verify value:", val)
	}

	return client, ctx
}

func initialize_maxmindDB() (*geoip2.Reader, *geoip2.Reader, func(), error) {
	cityDb, err := geoip2.Open("/data/geoip/GeoLite2-City.mmdb")
	if err != nil {
		return nil, nil, nil, fmt.Errorf("Issues while opening City DB")
	}
	asnDB, err := geoip2.Open("/data/geoip/GeoLite2-ASN.mmdb")
	if err != nil {
		cityDb.Close()
		return nil, nil, nil, fmt.Errorf("Issues while opening the ans db")
	}

	cleanup := func() {
		fmt.Println("Cleaning up and closing all the DBs")
		cityDb.Close()
		asnDB.Close()
	}

	return cityDb, asnDB, cleanup, nil

}

func maxMindDBLookup(ip string, cityDb *geoip2.Reader, asnDB *geoip2.Reader) (*geoip2.City, *geoip2.ASN) {
	ans := net.ParseIP(ip)
	city, _ := cityDb.City(ans)
	asn, _ := asnDB.ASN(ans)
	return city, asn
}


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
		kafkaConsumerTopics = "raw_transactions"
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

	

	// If not in Redis, Use maxmind and enrich it

	// Push the enriched txn to Enriched transaction Kafka topic
	// /data/geoip/GeoLite2-City.mmdb
	// /data/geoip/GeoLite2-ASN.mmdb
}

