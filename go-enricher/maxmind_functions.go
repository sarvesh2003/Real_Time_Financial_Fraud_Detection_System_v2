package main

import (
	"fmt"
	"net"

	"github.com/oschwald/geoip2-golang"
)



func initialize_maxmindDB() (*geoip2.Reader, *geoip2.Reader, func(), error) {
	cityDb, err := geoip2.Open("/data/geoip/GeoLite2-City.mmdb")
	if err != nil {
		return nil, nil, nil, fmt.Errorf("Issues while opening City DB")
	}
	asnDB, err := geoip2.Open("/data/geoip/GeoLite2-ASN.mmdb")
	if err != nil {
		cityDb.Close()
		return nil, nil, nil, fmt.Errorf("Issues while opening the ans db")
	}

	cleanup := func() {
		fmt.Println("Cleaning up and closing all the DBs")
		cityDb.Close()
		asnDB.Close()
	}

	return cityDb, asnDB, cleanup, nil

}

func maxMindDBLookup(ip string, cityDb *geoip2.Reader, asnDB *geoip2.Reader) (*geoip2.City, *geoip2.ASN) {
	ans := net.ParseIP(ip)
	city, _ := cityDb.City(ans)
	asn, _ := asnDB.ASN(ans)
	return city, asn
}

