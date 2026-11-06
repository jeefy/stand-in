package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

const (
	DefaultChunkBytes   = 1048576
	DefaultMaxBodyBytes = 33554432
	DefaultCacheEntries = 50000
)

type Config struct {
	Mode                 string
	Salt                 string
	AllowDefaultSalt     bool
	GRPCPort             int
	MetricsPort          int
	ChunkBytes           int
	MaxBodyBytes         int
	CacheEntries         int
	MaxConcurrentStreams int
	DrainSeconds         float64
	ShutdownGrace        float64
	DeferRequestHeaders  bool
	CountNew             bool
	PatternsFile         string
	LogLevel             string
}

func Load() (*Config, error) {
	mode := strings.ToLower(strings.TrimSpace(getEnv("STANDIN_MODE", "shadow")))
	if mode != "shadow" && mode != "enforce" {
		return nil, fmt.Errorf("invalid STANDIN_MODE: %s (must be shadow or enforce)", mode)
	}

	allowDefaultSalt, err := getEnvBool("STANDIN_ALLOW_DEFAULT_SALT", false)
	if err != nil {
		return nil, err
	}

	salt := getEnv("STANDIN_SALT", "")
	if mode == "enforce" && salt == "" && !allowDefaultSalt {
		return nil, fmt.Errorf("STANDIN_SALT is required in enforce mode (or set STANDIN_ALLOW_DEFAULT_SALT=true)")
	}

	grpcPort, err := getEnvInt("STANDIN_GRPC_PORT", 9101)
	if err != nil {
		return nil, err
	}

	metricsPort, err := getEnvInt("STANDIN_METRICS_PORT", 9102)
	if err != nil {
		return nil, err
	}

	chunkBytes, err := getEnvInt("STANDIN_CHUNK_BYTES", DefaultChunkBytes)
	if err != nil {
		return nil, err
	}
	if chunkBytes <= 0 {
		chunkBytes = DefaultChunkBytes
	}

	maxBodyBytes, err := getEnvInt("STANDIN_MAX_BODY_BYTES", DefaultMaxBodyBytes)
	if err != nil {
		return nil, err
	}

	cacheEntries, err := getEnvInt("STANDIN_CACHE_ENTRIES", DefaultCacheEntries)
	if err != nil {
		return nil, err
	}

	maxConcurrentStreams, err := getEnvInt("STANDIN_MAX_CONCURRENT_STREAMS", 0)
	if err != nil {
		return nil, err
	}

	drainSeconds, err := getEnvFloat("STANDIN_DRAIN_SECONDS", 10.0)
	if err != nil {
		return nil, err
	}

	shutdownGrace, err := getEnvFloat("STANDIN_SHUTDOWN_GRACE", 600.0)
	if err != nil {
		return nil, err
	}

	deferHeaders, err := getEnvBool("STANDIN_DEFER_REQUEST_HEADERS", true)
	if err != nil {
		return nil, err
	}

	countNew, err := getEnvBool("STANDIN_COUNT_NEW", false)
	if err != nil {
		return nil, err
	}

	return &Config{
		Mode:                 mode,
		Salt:                 salt,
		AllowDefaultSalt:     allowDefaultSalt,
		GRPCPort:             grpcPort,
		MetricsPort:          metricsPort,
		ChunkBytes:           chunkBytes,
		MaxBodyBytes:         maxBodyBytes,
		CacheEntries:         cacheEntries,
		MaxConcurrentStreams: maxConcurrentStreams,
		DrainSeconds:         drainSeconds,
		ShutdownGrace:        shutdownGrace,
		DeferRequestHeaders:  deferHeaders,
		CountNew:             countNew,
		PatternsFile:         getEnv("STANDIN_PATTERNS_FILE", ""),
		LogLevel:             getEnv("LOG_LEVEL", "INFO"),
	}, nil
}

func getEnv(key, defaultVal string) string {
	if val, ok := os.LookupEnv(key); ok {
		return val
	}
	return defaultVal
}

func getEnvInt(key string, defaultVal int) (int, error) {
	val, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(val) == "" {
		return defaultVal, nil
	}
	n, err := strconv.Atoi(strings.TrimSpace(val))
	if err != nil {
		return 0, fmt.Errorf("invalid integer for %s: %s", key, val)
	}
	if n < 0 {
		return 0, fmt.Errorf("%s must not be negative: %d", key, n)
	}
	return n, nil
}

func getEnvFloat(key string, defaultVal float64) (float64, error) {
	val, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(val) == "" {
		return defaultVal, nil
	}
	f, err := strconv.ParseFloat(strings.TrimSpace(val), 64)
	if err != nil {
		return 0, fmt.Errorf("invalid number for %s: %s", key, val)
	}
	if f < 0 {
		return 0, fmt.Errorf("%s must not be negative: %f", key, f)
	}
	return f, nil
}

func getEnvBool(key string, defaultVal bool) (bool, error) {
	val, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(val) == "" {
		return defaultVal, nil
	}
	s := strings.ToLower(strings.TrimSpace(val))
	switch s {
	case "1", "true", "yes", "on":
		return true, nil
	case "0", "false", "no", "off":
		return false, nil
	default:
		return false, fmt.Errorf("invalid boolean for %s: %s", key, val)
	}
}
