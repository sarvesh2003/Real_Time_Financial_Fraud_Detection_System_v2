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
	messagesProcessedTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "fraud_enricher_messages_processed_total",
		Help: "Total number of raw transactions successfully enriched and published",
	})

	enricherErrorsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "fraud_enricher_errors_total",
		Help: "Total number of enrichment errors, by reason",
	}, []string{"reason"})

	duplicateMessagesTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "fraud_enricher_duplicate_messages_total",
		Help: "Total number of duplicate transactions skipped by the enricher",
	})

	enrichmentDuration = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "fraud_enricher_message_duration_seconds",
		Help:    "Time taken to process a single raw transaction message",
		Buckets: prometheus.DefBuckets,
	})
)

func startMetricsServer() {
	addr := os.Getenv("METRICS_ADDR")
	if addr == "" {
		addr = ":9101"
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
