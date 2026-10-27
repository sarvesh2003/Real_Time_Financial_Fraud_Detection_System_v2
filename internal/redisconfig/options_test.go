package redisconfig

import (
	"testing"
	"time"
)

func TestFromEnvValidatesAndSharesSettings(t *testing.T) {
	t.Setenv("REDIS_CLUSTER_ADDRS", "redis-a:6379, redis-b:6379")
	t.Setenv("REDIS_READ_TIMEOUT_MS", "25")
	t.Setenv("REDIS_POOL_SIZE", "2")
	options, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if len(options.Addrs) != 2 || options.Addrs[1] != "redis-b:6379" {
		t.Fatalf("addresses not normalized: %#v", options.Addrs)
	}
	if options.ReadTimeout != 25*time.Millisecond || options.PoolSize != 2 {
		t.Fatalf("settings not applied: timeout=%v pool=%d", options.ReadTimeout, options.PoolSize)
	}
}

func TestFromEnvRejectsMalformedConfiguration(t *testing.T) {
	cases := []struct{ key, value string }{
		{"REDIS_CLUSTER_ADDRS", "redis-a:6379,,redis-b:6379"},
		{"REDIS_READ_TIMEOUT_MS", "twenty"},
		{"REDIS_POOL_SIZE", "-1"},
	}
	for _, test := range cases {
		t.Run(test.key+"="+test.value, func(t *testing.T) {
			t.Setenv(test.key, test.value)
			if _, err := FromEnv(); err == nil {
				t.Fatal("expected invalid configuration error")
			}
		})
	}
}
