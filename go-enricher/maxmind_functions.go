package main

import (
	"fmt"
	"net"

	"github.com/oschwald/geoip2-golang"
)

func initialize_maxmindDB() (*geoip2.Reader, *geoip2.Reader, func(), error) {
	cityDb, err := geoip2.Open("/data/geoip/GeoLite2-City.mmdb")
	if err != nil {
		return nil, nil, nil, fmt.Errorf("open City DB: %w", err)
	}
	asnDB, err := geoip2.Open("/data/geoip/GeoLite2-ASN.mmdb")
	if err != nil {
		cityDb.Close()
		return nil, nil, nil, fmt.Errorf("open ASN DB: %w", err)
	}

	cleanup := func() {
		fmt.Println("Cleaning up and closing all the DBs")
		cityDb.Close()
		asnDB.Close()
	}

	return cityDb, asnDB, cleanup, nil

}

func maxMindDBLookup(ip string, cityDb *geoip2.Reader, asnDB *geoip2.Reader) (*geoip2.City, *geoip2.ASN, error) {
	parsed := net.ParseIP(ip)
	if parsed == nil || cityDb == nil || asnDB == nil {
		return nil, nil, fmt.Errorf("invalid IP or unavailable GeoIP database")
	}
	city, err := cityDb.City(parsed)
	if err != nil {
		return nil, nil, fmt.Errorf("City lookup: %w", err)
	}
	asn, err := asnDB.ASN(parsed)
	if err != nil {
		return nil, nil, fmt.Errorf("ASN lookup: %w", err)
	}
	if city == nil || asn == nil {
		return nil, nil, fmt.Errorf("GeoIP database returned no record")
	}
	return city, asn, nil
}
