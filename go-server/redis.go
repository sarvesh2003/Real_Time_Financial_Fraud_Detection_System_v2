package main

import (
	"context"
	"fmt"

	"github.com/redis/go-redis/v9"

	"fraud/internal/redisconfig"
)

// Token bucket per user, refilled from Redis server time. KEYS[1] carries a
// hash tag so the whole bucket lives on one cluster slot.
var tokenBucketRateLimiting = redis.NewScript(`
	local user_id = KEYS[1]
	local rate = tonumber(ARGV[1])
	local burst = tonumber(ARGV[2])
	local clock = redis.call("TIME")
	local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
	local info = redis.call("HMGET", user_id, "tokens", "last_refill")
	local tokens = tonumber(info[1]) or burst
	local last_refill = tonumber(info[2]) or now
	tokens = math.min(burst, tokens + math.max(0, now - last_refill) * rate)
	local allowed = 0
	if tokens >= 1 then
		tokens = tokens - 1
		allowed = 1
	end
	redis.call("HSET", user_id, "tokens", tokens, "last_refill", now)
	redis.call("EXPIRE", user_id, 60)
	return allowed
`)

const (
	rateLimitPerSecond = 500
	rateLimitBurst     = 1000
)

type redisRateLimiter struct {
	client *redis.ClusterClient
}

func (l redisRateLimiter) Allow(ctx context.Context, userID string) (bool, error) {
	key := fmt.Sprintf("rate_limit_user:{%s}", userID)
	allowed, err := tokenBucketRateLimiting.Run(ctx, l.client, []string{key}, rateLimitPerSecond, rateLimitBurst).Int()
	if err != nil {
		return false, err
	}
	return allowed == 1, nil
}

func initializeRedis(ctx context.Context) (*redis.ClusterClient, error) {
	options, err := redisconfig.FromEnv()
	if err != nil {
		return nil, err
	}
	client := redis.NewClusterClient(options)
	if err := client.Ping(ctx).Err(); err != nil {
		client.Close()
		return nil, fmt.Errorf("connect to Redis cluster: %w", err)
	}
	return client, nil
}
