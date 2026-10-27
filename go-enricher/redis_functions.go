package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	"github.com/redis/go-redis/v9"

	"fraud/internal/redisconfig"
)

// Two-hour processing-time window per IP. KEYS share the {ip} hash tag so the
// script runs atomically on one cluster slot; the dedup key lasts seven days.
var updateFraud = redis.NewScript(`
	local times_key = KEYS[1]
	local amounts_key = KEYS[2]
	local dedup_key = KEYS[3]
	local amount = tonumber(ARGV[1])
	local now_ms = tonumber(ARGV[2])
	local txn_id = ARGV[3]
	local window_ms = tonumber(ARGV[4])
	local dedup_ttl_seconds = tonumber(ARGV[5])
	local duplicate = redis.call("EXISTS", dedup_key) == 1
	if not duplicate then
		redis.call("ZADD", times_key, now_ms, txn_id)
		redis.call("HSET", amounts_key, txn_id, amount)
		redis.call("SET", dedup_key, "1", "EX", dedup_ttl_seconds)
	end

	local cutoff = now_ms - window_ms
	local expired = redis.call("ZRANGEBYSCORE", times_key, "-inf", "(" .. cutoff)
	for _, id in ipairs(expired) do
		redis.call("HDEL", amounts_key, id)
	end
	redis.call("ZREMRANGEBYSCORE", times_key, "-inf", "(" .. cutoff)
	local ttl_seconds = math.ceil(window_ms / 1000) + 60
	redis.call("EXPIRE", times_key, ttl_seconds)
	redis.call("EXPIRE", amounts_key, ttl_seconds)

	local entries = redis.call("ZRANGE", times_key, 0, -1, "WITHSCORES")
	local count = #entries / 2
	local total = 0
	local maximum = 0
	for i = 1, #entries, 2 do
		local value = tonumber(redis.call("HGET", amounts_key, entries[i])) or 0
		total = total + value
		maximum = math.max(maximum, value)
	end
	local first_seen = 0
	local last_seen = 0
	if count > 0 then
		first_seen = math.floor(tonumber(entries[2]) / 1000)
		last_seen = math.floor(tonumber(entries[#entries]) / 1000)
	end
	return cjson.encode({
		data = {
			first_seen = first_seen,
			last_seen = last_seen,
			txn_count = count,
			total_amount = total,
			amount_velocity = total / (window_ms / 3600000),
			avg_amount = count > 0 and total / count or 0,
			max_amount = maximum
		},
		duplicate = duplicate
	})
`)

// getGeoFromRedis reports found=false on a cache miss.
func getGeoFromRedis(ctx context.Context, client *redis.ClusterClient, ip string) (GeoData, bool, error) {
	value, err := client.Get(ctx, fmt.Sprintf("geo:{%s}", ip)).Result()
	if err == redis.Nil {
		return GeoData{}, false, nil
	}
	if err != nil {
		return GeoData{}, false, err
	}
	var geo GeoData
	if err := json.Unmarshal([]byte(value), &geo); err != nil {
		return GeoData{}, false, fmt.Errorf("decode cached GeoIP for %s: %w", ip, err)
	}
	return geo, true, nil
}

func setGeoToRedis(ctx context.Context, client *redis.ClusterClient, ip string, geo GeoData, ttl time.Duration) error {
	encoded, err := json.Marshal(geo)
	if err != nil {
		return err
	}
	return client.Set(ctx, fmt.Sprintf("geo:{%s}", ip), encoded, ttl).Err()
}

func updateFraudInRedis(ctx context.Context, client *redis.ClusterClient, ip string, amount float64, transactionid string) (*FraudSignals, bool, error) {
	timesKey := fmt.Sprintf("fraud:{%s}:times", ip)
	amountsKey := fmt.Sprintf("fraud:{%s}:amounts", ip)
	dedupKey := fmt.Sprintf("dedup:{%s}:%s", ip, transactionid)

	data, err := updateFraud.Run(ctx, client, []string{timesKey, amountsKey, dedupKey},
		amount, time.Now().UnixMilli(), transactionid, 2*time.Hour.Milliseconds(), int((7 * 24 * time.Hour).Seconds())).Result()
	if err != nil {
		return nil, false, fmt.Errorf("rolling fraud window update: %w", err)
	}

	var response struct {
		Data      FraudSignals `json:"data"`
		Duplicate bool         `json:"duplicate"`
	}

	encoded, ok := data.(string)
	if !ok {
		return nil, false, fmt.Errorf("rolling fraud window returned %T", data)
	}
	err = json.Unmarshal([]byte(encoded), &response)
	if err != nil {
		return nil, false, fmt.Errorf("decode rolling fraud window: %w", err)
	}

	if response.Duplicate {
		log.Printf("DUPLICATE: txn=%s ip=%s (counter unchanged, output replayed)", transactionid, ip)
	} else if response.Data.TxnCount == 1 {
		log.Printf("NEW IP in fraud tracking: %s (Amount: $%.2f)", ip, amount)
	} else {
		log.Printf("UPDATED fraud data: IP=%s | Count=%d | Total=$%.2f | Velocity=$%.2f/h",
			ip, response.Data.TxnCount, response.Data.TotalAmount, response.Data.AmountVelocity)
	}

	return &response.Data, response.Duplicate, nil
}

func initializeRedis() (*redis.ClusterClient, error) {
	options, err := redisconfig.FromEnv()
	if err != nil {
		return nil, err
	}
	client := redis.NewClusterClient(options)
	for attempt := 1; attempt <= 30; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		err = client.Set(ctx, "health_check", "ok", 5*time.Second).Err()
		cancel()
		if err == nil {
			log.Printf("Redis Cluster ready after %d attempt(s)", attempt)
			return client, nil
		}
		log.Printf("Redis Cluster unavailable (attempt %d/30): %v", attempt, err)
		time.Sleep(2 * time.Second)
	}
	client.Close()
	return nil, fmt.Errorf("Redis Cluster unavailable after 30 attempts: %w", err)
}
