package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

// Lua script
var updateFraud = redis.NewScript(`
	local fraud_key = KEYS[1]
	local dedup_key = KEYS[2]
	local amount = tonumber(ARGV[1])
	local now = tonumber(ARGV[2])
	local txn_id = ARGV[3]
	local ttl = tonumber(ARGV[4])

	-- Idempotency check
	if redis.call("SISMEMBER", dedup_key, txn_id) == 1 then
		local existing = redis.call("GET", fraud_key)
		if existing then
			return cjson.encode({data = cjson.decode(existing), duplicate = true})
		else
			return cjson.encode({data = {}, duplicate = true})
		end
	end

	-- Mark Txn id as processed
	redis.call("SADD", dedup_key, txn_id)
	redis.call("EXPIRE", dedup_key, ttl)

	local value = redis.call("GET", fraud_key)
	
	-- NEW IP, so add to redis
	if not value then
		local fraud_str = {
			first_seen = now,
			last_seen = now,
			txn_count = 1,
			total_amount = amount,
			amount_velocity = 0,
			avg_amount = amount,
			max_amount = amount
		}
		redis.call("SET", fraud_key, cjson.encode(fraud_str), 'EX', ttl)
		return cjson.encode({data = fraud_str, duplicate = false})
	end

	-- Already present, so update it
	local fraud_data = cjson.decode(value)
	fraud_data.last_seen = now
	fraud_data.txn_count = fraud_data.txn_count + 1
	fraud_data.total_amount = fraud_data.total_amount + amount
	
	local duration_hrs = (now - fraud_data.first_seen) / 3600
	if duration_hrs > 0 then
		fraud_data.amount_velocity = fraud_data.total_amount / duration_hrs
	end

	fraud_data.avg_amount = fraud_data.total_amount / fraud_data.txn_count

	if amount > fraud_data.max_amount then
		fraud_data.max_amount = amount
	end

	redis.call("SET", fraud_key, cjson.encode(fraud_data), 'EX', ttl)
	return cjson.encode({data = fraud_data, duplicate = false})

`)


func getGeoFromRedis(client *redis.ClusterClient, ctx context.Context, ip string) (*GeoData, string, error) {
	geoIp := fmt.Sprintf("geo:{%s}", ip)
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
	geoIp := fmt.Sprintf("geo:{%s}", ip)
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

func updateFraudInRedis(client *redis.ClusterClient, ctx context.Context, ip string, amount float64, transactionid string) (*FraudSignals, bool, error) {
	fraudKey := fmt.Sprintf("fraud:{%s}", ip)
	dedupKey := fmt.Sprintf("dedup:{%s}", ip)

	now := time.Now().Unix()
	ttl := 7200

	data, err := updateFraud.Run(ctx, client, []string{fraudKey, dedupKey}, amount, now, transactionid, ttl).Result()
	if err != nil {
		return nil, false, fmt.Errorf("Lua script failed with %w", err)
	}

	var response struct {
		Data      FraudSignals `json:"data"`
		Duplicate bool `json:"duplicate"`
	}

	// var fraudData struct {
	// 	FirstSeen int64 `json:"first_seen"`
	// 	LastSeen int64 `json:"last_seen"`
	// 	TxnCount int `json:"txn_count"`
	// 	TotalAmount float64 `json:"total_amount"`
	// 	AmountVelocity float64 `json:"amount_velocity"`
	// 	AvgAmount float64 `json:"avg_amount"`
	// 	MaxAmount float64 `json:"max_amount"`
	// }

	err = json.Unmarshal([]byte(data.(string)), &response)
	if err != nil {
		return nil, false, fmt.Errorf("Some issue with Unmarshalling the JSON: %w", err)
	}

	// fraud := &FraudSignals{
	// 		FirstSeen: time.Unix(fraudData.FirstSeen, 0),
	// 		LastSeen: time.Unix(fraudData.LastSeen, 0),
	// 		TxnCount: fraudData.TxnCount,
	// 		TotalAmount: fraudData.TotalAmount,
	// 		AmountVelocity: fraudData.AmountVelocity,
	// 		AvgAmount: fraudData.AvgAmount,
	// 		MaxAmount: fraudData.MaxAmount,
	// 	}

	if response.Duplicate {
		log.Printf("DUPLICATE: txn=%s ip=%s (skipped)", transactionid, ip)
	} else if response.Data.TxnCount == 1 {
		log.Printf("NEW IP in fraud tracking: %s (Amount: $%.2f)", ip, amount)
	} else {
		log.Printf("UPDATED fraud data: IP=%s | Count=%d | Total=$%.2f | Velocity=$%.2f/h",
			ip, response.Data.TxnCount, response.Data.TotalAmount, response.Data.AmountVelocity)
	}

	return &response.Data, response.Duplicate, nil
}


func initialize_redis() (*redis.ClusterClient, context.Context) {
	redisAddr := os.Getenv("REDIS_CLUSTER_ADDR")
	var addrs []string
	if redisAddr == "" {
		addrs = []string{
			"192.168.240.100:6379",
			"192.168.240.101:6379",
			"192.168.240.102:6379",
		}
		log.Println("Using default docker IPs...")
	} else {
		addrs = strings.Split(redisAddr, ",")
		log.Println("Using Redis cluster IPs...")
	}
	client := redis.NewClusterClient(&redis.ClusterOptions{
		Addrs: addrs,
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
