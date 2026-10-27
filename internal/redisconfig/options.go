// Package redisconfig reads the shared Redis Cluster client settings.
package redisconfig

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

func FromEnv() (*redis.ClusterOptions, error) {
	addresses := os.Getenv("REDIS_CLUSTER_ADDRS")
	if addresses == "" {
		addresses = "redis1:6379,redis2:6379,redis3:6379"
	}
	addrs := strings.Split(addresses, ",")
	for i, addr := range addrs {
		addrs[i] = strings.TrimSpace(addr)
		if addrs[i] == "" {
			return nil, fmt.Errorf("REDIS_CLUSTER_ADDRS contains an empty address")
		}
	}
	options := &redis.ClusterOptions{Addrs: addrs, RouteByLatency: true}
	var err error
	for _, setting := range []struct {
		name  string
		value *time.Duration
	}{
		{"REDIS_DIAL_TIMEOUT_MS", &options.DialTimeout},
		{"REDIS_READ_TIMEOUT_MS", &options.ReadTimeout},
		{"REDIS_WRITE_TIMEOUT_MS", &options.WriteTimeout},
		{"REDIS_MIN_RETRY_BACKOFF_MS", &options.MinRetryBackoff},
		{"REDIS_MAX_RETRY_BACKOFF_MS", &options.MaxRetryBackoff},
	} {
		*setting.value, err = millis(setting.name)
		if err != nil {
			return nil, err
		}
	}
	options.PoolSize, err = integer("REDIS_POOL_SIZE")
	if err != nil {
		return nil, err
	}
	options.MaxRetries, err = integer("REDIS_MAX_RETRIES")
	if err != nil {
		return nil, err
	}
	return options, nil
}

func millis(name string) (time.Duration, error) {
	value := os.Getenv(name)
	if value == "" {
		return 0, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return 0, fmt.Errorf("%s must be a non-negative integer in milliseconds", name)
	}
	return time.Duration(parsed) * time.Millisecond, nil
}

func integer(name string) (int, error) {
	value := os.Getenv(name)
	if value == "" {
		return 0, nil
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed < 0 {
		return 0, fmt.Errorf("%s must be a non-negative integer", name)
	}
	return parsed, nil
}
