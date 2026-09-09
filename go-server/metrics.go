package main

import (
	"log"
	"net/http"
	"os"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	transactionsTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "fraud_server_transactions_total",
		Help: "Total number of transactions received by the ingestion server",
	})

	transactionErrorsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "fraud_server_transaction_errors_total",
		Help: "Total number of transaction processing errors, by reason",
	}, []string{"reason"})

	transactionDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "fraud_server_transaction_duration_seconds",
		Help:    "Time taken to handle a SendTransaction request",
		Buckets: prometheus.DefBuckets,
	})
)

func startMetricsServer() {
	addr := os.Getenv("METRICS_ADDR")
	if addr == "" {
		addr = ":9100"
	}

	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())

	go func() {
		log.Printf("Metrics server listening at %s", addr)
		if err := http.ListenAndServe(addr, mux); err != nil {
			log.Printf("Metrics server error: %v", err)
		}
	}()
}
