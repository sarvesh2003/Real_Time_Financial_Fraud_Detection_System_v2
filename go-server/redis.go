package main

import (
	"context"
	"fmt"

	"github.com/redis/go-redis/v9"

	"fraud/internal/redisconfig"
)

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
