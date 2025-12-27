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

type EnrichedTransaction struct {
	TransactionID            string  `json:"transaction_id"`
	UserID                   string  `json:"user_id"`
	Amount                   float64 `json:"amount"`
	Timestamp                int64   `json:"timestamp"`
	IsFraud                  bool    `json:"is_fraud"`
	Type                     string  `json:"type"`
	OldBalanceOrig           float64 `json:"old_balance_orig"`
	NewBalanceOrig           float64 `json:"new_balance_orig"`
	OldBalanceDest           float64 `json:"old_balance_dest"`
	NewBalanceDest           float64 `json:"new_balance_dest"`
	IsUnauthorizedOverdraft  float64 `json:"is_unauthorized_overdraft"`
	
	IPAddress   string  `json:"ip_address"`
	City        string  `json:"city"`
	Country     string  `json:"country"`
	CountryCode string  `json:"country_code"`
	Latitude    float64 `json:"latitude"`
	Longitude   float64 `json:"longitude"`
	ASN         string  `json:"asn"`
	ISP         string  `json:"isp"`
	IsHosting   bool    `json:"is_hosting"`
	
	TxnCount2h     int     `json:"txn_count_2h"`
	TotalAmount2h  float64 `json:"total_amount_2h"`
	AmountVelocity float64 `json:"amount_velocity"`
	AvgAmount2h    float64 `json:"avg_amount_2h"`
	MaxAmount2h    float64 `json:"max_amount_2h"`
}