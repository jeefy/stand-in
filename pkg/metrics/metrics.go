package metrics

import (
	"fmt"
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	RequestsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "standin_requests_total",
			Help: "ext_proc exchanges by mode and outcome",
		},
		[]string{"mode", "outcome"},
	)

	MatchesTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "standin_matches_total",
			Help: "Rule matches in request bodies (patterns.yaml tier and index)",
		},
		[]string{"tier", "pattern_index"},
	)

	RestoredTotal = prometheus.NewCounter(
		prometheus.CounterOpts{
			Name: "standin_restored_total",
			Help: "Placeholders restored to their original value in responses",
		},
	)

	ErrorsTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "standin_errors_total",
			Help: "Exceptions handled by passing the exchange through unchanged",
		},
		[]string{"phase"},
	)

	HeldBytes = prometheus.NewHistogram(
		prometheus.HistogramOpts{
			Name:    "standin_held_bytes",
			Help:    "Bytes held back by the response restorer after each response chunk",
			Buckets: []float64{0, 1, 2, 4, 8, 16, 22, 23, 64, 256, 1024, 4096, 16384},
		},
	)

	BodyBytes = prometheus.NewHistogram(
		prometheus.HistogramOpts{
			Name:    "standin_body_bytes",
			Help:    "Request body size",
			Buckets: bodyBuckets(),
		},
	)

	TokenizeSeconds = prometheus.NewHistogram(
		prometheus.HistogramOpts{
			Name:    "standin_tokenize_seconds",
			Help:    "Time spent tokenizing one request body",
			Buckets: []float64{0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5},
		},
	)

	ActiveStreams = prometheus.NewGauge(
		prometheus.GaugeOpts{
			Name: "standin_active_streams",
			Help: "ext_proc streams currently open",
		},
	)

	CacheHitsTotal = prometheus.NewCounter(
		prometheus.CounterOpts{
			Name: "standin_cache_hits_total",
			Help: "String literals whose scan result came from the per-literal cache",
		},
	)

	NewMatchesTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "standin_new_matches_total",
			Help: "Rule matches in the newest turn of request bodies, Tier A-D (STANDIN_COUNT_NEW)",
		},
		[]string{"tier", "pattern_index"},
	)

	TurnScopeTotal = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "standin_turn_scope_total",
			Help: "What the new-turn count scanned: tail, first (whole body), input_only, none",
		},
		[]string{"scope"},
	)
)

func bodyBuckets() []float64 {
	var b []float64
	cur := 1024.0
	for i := 0; i < 10; i++ {
		b = append(b, cur)
		cur *= 4.0
	}
	return b
}

func init() {
	prometheus.MustRegister(
		RequestsTotal,
		MatchesTotal,
		RestoredTotal,
		ErrorsTotal,
		HeldBytes,
		BodyBytes,
		TokenizeSeconds,
		ActiveStreams,
		CacheHitsTotal,
		NewMatchesTotal,
		TurnScopeTotal,
	)
}

func Handler() http.Handler {
	return promhttp.Handler()
}

func RecordMatch(tier string, index int) {
	MatchesTotal.WithLabelValues(tier, fmt.Sprintf("%d", index)).Inc()
}

func RecordNewMatch(tier string, index int) {
	NewMatchesTotal.WithLabelValues(tier, fmt.Sprintf("%d", index)).Inc()
}

func RecordRequest(mode, outcome string) {
	RequestsTotal.WithLabelValues(mode, outcome).Inc()
}

func RecordError(phase string) {
	ErrorsTotal.WithLabelValues(phase).Inc()
}
