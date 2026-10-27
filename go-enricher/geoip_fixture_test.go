package main

import "testing"

// The record-free fixture used by credential-free stacks opens with the real
// reader and returns empty fields, not errors, for any address.
func TestGeoIPFixtureReturnsEmptyFields(t *testing.T) {
	geo, err := openGeoIP("../tests/fixtures/geoip", defaultHostingKeywords)
	if err != nil {
		t.Fatal(err)
	}
	defer geo.Close()
	for _, ip := range []string{"203.0.113.9", "2001:db8::1"} {
		data, err := geo.lookup(ip)
		if err != nil {
			t.Fatalf("%s: %v", ip, err)
		}
		if data.City != "" || data.Country != "" || data.ISP != "" || data.IsHosting {
			t.Fatalf("%s: fixture returned data %+v", ip, data)
		}
	}
	if _, err := geo.lookup("not-an-ip"); err == nil {
		t.Fatal("invalid IP must be an error")
	}
}
