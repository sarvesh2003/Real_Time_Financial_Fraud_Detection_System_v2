package main
import "time"

type GeoData struct {
	City        string  `json:"city"`
	Country     string  `json:"country"`
	CountryCode string  `json:"country_code"`
	Latitude    float64 `json:"lat"`
	Longitude   float64 `json:"lon"`
	ASN         string  `json:"asn"`
	ISP         string  `json:"isp"`
	IsHosting   bool    `json:"is_hosting"`
}

type FraudSignals struct {
	FirstSeen      time.Time `json:"first_seen"`
	LastSeen       time.Time `json:"last_seen"`
	TxnCount       int       `json:"txn_count"`
	TotalAmount    float64   `json:"total_amount"`
	AmountVelocity float64   `json:"amount_velocity"`
	AvgAmount      float64   `json:"avg_amount"`
	MaxAmount      float64   `json:"max_amount"`
}
