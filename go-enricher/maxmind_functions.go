package main

import (
	"fmt"
	"net"
	"path/filepath"
	"strings"

	"github.com/oschwald/geoip2-golang"
)

type geoIP struct {
	city            *geoip2.Reader
	asn             *geoip2.Reader
	hostingKeywords []string
}

// openGeoIP opens the City and ASN databases written by geoip-updater.
func openGeoIP(dir string, hostingKeywords []string) (*geoIP, error) {
	city, err := geoip2.Open(filepath.Join(dir, "GeoLite2-City.mmdb"))
	if err != nil {
		return nil, fmt.Errorf("open City DB: %w", err)
	}
	asn, err := geoip2.Open(filepath.Join(dir, "GeoLite2-ASN.mmdb"))
	if err != nil {
		city.Close()
		return nil, fmt.Errorf("open ASN DB: %w", err)
	}
	return &geoIP{city: city, asn: asn, hostingKeywords: hostingKeywords}, nil
}

func (g *geoIP) Close() {
	g.city.Close()
	g.asn.Close()
}

// lookup returns empty fields, not an error, for an address absent from the
// databases (for example a reserved range).
func (g *geoIP) lookup(ip string) (GeoData, error) {
	parsed := net.ParseIP(ip)
	if parsed == nil {
		return GeoData{}, fmt.Errorf("invalid IP %q", ip)
	}
	city, err := g.city.City(parsed)
	if err != nil {
		return GeoData{}, fmt.Errorf("City lookup: %w", err)
	}
	asn, err := g.asn.ASN(parsed)
	if err != nil {
		return GeoData{}, fmt.Errorf("ASN lookup: %w", err)
	}
	return GeoData{
		City: city.City.Names["en"], Country: city.Country.Names["en"],
		CountryCode: city.Country.IsoCode,
		Latitude:    city.Location.Latitude, Longitude: city.Location.Longitude,
		ASN:       fmt.Sprintf("AS%d", asn.AutonomousSystemNumber),
		ISP:       asn.AutonomousSystemOrganization,
		IsHosting: isHostingProvider(asn.AutonomousSystemOrganization, g.hostingKeywords),
	}, nil
}

func isHostingProvider(organization string, keywords []string) bool {
	organization = strings.ToLower(organization)
	for _, keyword := range keywords {
		if strings.Contains(organization, strings.TrimSpace(keyword)) {
			return true
		}
	}
	return false
}
