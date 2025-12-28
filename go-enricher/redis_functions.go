package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

// Lua script
var updateFraud = redis.NewScript(`
	local key = KEYS[1]
	local amount = tonumber(ARGV[1])
	local now = tonumber(ARGV[2])

	local value = redis.call("GET", key)

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
		redis.call("SET", key, cjson.encode(fraud_str), 'EX', 7200)
		return cjson.encode(fraud_str)
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

	redis.call("SET", key, cjson.encode(fraud_data), 'EX', 7200)
	return cjson.encode(fraud_data)	

`)


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
	now := time.Now().Unix()
	data, err := updateFraud.Run(ctx, client, []string{fraudKey}, amount, now).Result()
	if err != nil {
		return nil, fmt.Errorf("Lua script failed with %w", err)
	}

	var fraudData struct {
		FirstSeen int64 `json:"first_seen"`
		LastSeen int64 `json:"last_seen"`
		TxnCount int `json:"txn_count"`
		TotalAmount float64 `json:"total_amount"`
		AmountVelocity float64 `json:"amount_velocity"`
		AvgAmount float64 `json:"avg_amount"`
		MaxAmount float64 `json:"max_amount"`
	}

	err = json.Unmarshal([]byte(data.(string)), &fraudData)
	if err != nil {
		return nil, fmt.Errorf("Some issue with Unmarshalling the JSON: %w", err)
	}

	fraud := &FraudSignals{
			FirstSeen: time.Unix(fraudData.FirstSeen, 0),
			LastSeen: time.Unix(fraudData.LastSeen, 0),
			TxnCount: fraudData.TxnCount,
			TotalAmount: fraudData.TotalAmount,
			AmountVelocity: fraudData.AmountVelocity,
			AvgAmount: fraudData.AvgAmount,
			MaxAmount: fraudData.MaxAmount,
		}

	if fraudData.TxnCount == 1 {
		log.Printf("NEW IP in fraud tracking: %s (Amount: $%.2f)", ip, amount)
	} else {
		log.Printf("UPDATED fraud data: IP=%s | Count=%d | Total=$%.2f | Velocity=$%.2f/h",
			ip, fraud.TxnCount, fraud.TotalAmount, fraud.AmountVelocity)
	}

	return fraud, nil
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
