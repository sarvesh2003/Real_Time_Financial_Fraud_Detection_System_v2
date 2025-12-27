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
