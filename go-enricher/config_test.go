package main

import (
	"testing"
	"time"
)

func TestConfigRejectsInvalidGeoCacheTTL(t *testing.T) {
	for _, value := range []string{"twelve", "0", "-3"} {
		t.Run(value, func(t *testing.T) {
			t.Setenv("GEO_TTL_HOURS", value)
			if _, err := configFromEnv(); err == nil {
				t.Fatalf("GEO_TTL_HOURS=%q must fail startup, not fall back silently", value)
			}
		})
	}
	t.Setenv("GEO_TTL_HOURS", "6")
	config, err := configFromEnv()
	if err != nil || config.geoCacheTTL != 6*time.Hour {
		t.Fatalf("want 6h, got %v %v", config.geoCacheTTL, err)
	}
}

func TestConfigIgnoresEmptyHostingKeywords(t *testing.T) {
	t.Setenv("HOSTING_KEYWORDS", "Hetzner, ,ovh,")
	config, err := configFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	// An empty keyword would match every organization.
	if isHostingProvider("Comcast Cable", config.hostingKeywords) {
		t.Fatalf("empty keyword matched a residential ISP: %q", config.hostingKeywords)
	}
	if !isHostingProvider("HETZNER Online GmbH", config.hostingKeywords) {
		t.Fatal("configured keyword did not match case-insensitively")
	}
}
