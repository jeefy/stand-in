package main

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	extprocv3 "github.com/envoyproxy/go-control-plane/envoy/service/ext_proc/v3"
	"github.com/jeefy/stand-in/pkg/config"
	"github.com/jeefy/stand-in/pkg/extproc"
	"github.com/jeefy/stand-in/pkg/metrics"
	"github.com/jeefy/stand-in/pkg/patterns"
	"github.com/jeefy/stand-in/pkg/tokenizer"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/keepalive"
)

const (
	maxMessageSize = 64 * 1024 * 1024
	serviceName    = "envoy.service.ext_proc.v3.ExternalProcessor"
)

func main() {
	setupLogging()

	cfg, err := config.Load()
	if err != nil {
		slog.Error("configuration error", "error", err)
		os.Exit(2)
	}

	if cfg.Salt == "" {
		slog.Warn("STANDIN_SALT unset or empty; using the built-in default salt")
	}

	rules, err := patterns.LoadRules(cfg.PatternsFile)
	if err != nil {
		slog.Error("failed to load rules", "error", err)
		os.Exit(2)
	}

	var countRules []patterns.Rule
	if cfg.CountNew {
		var err error
		countRules, err = patterns.LoadCountRules(cfg.PatternsFile)
		if err != nil {
			slog.Error("failed to load count rules", "error", err)
			os.Exit(2)
		}
	}

	tok, err := tokenizer.NewTokenizer(rules, countRules, cfg.Salt, cfg.CacheEntries)
	if err != nil {
		slog.Error("failed to initialize tokenizer", "error", err)
		os.Exit(2)
	}

	srv := extproc.NewServer(tok, countRules, cfg)

	metricsMux := http.NewServeMux()
	metricsMux.Handle("/metrics", metrics.Handler())
	metricsServer := &http.Server{
		Addr:    fmt.Sprintf(":%d", cfg.MetricsPort),
		Handler: metricsMux,
	}

	go func() {
		if err := metricsServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("metrics server failed", "error", err)
		}
	}()

	grpcLis, err := net.Listen("tcp", fmt.Sprintf(":%d", cfg.GRPCPort))
	if err != nil {
		slog.Error("failed to listen on gRPC port", "port", cfg.GRPCPort, "error", err)
		os.Exit(2)
	}

	serverOptions := []grpc.ServerOption{
		grpc.MaxRecvMsgSize(maxMessageSize),
		grpc.MaxSendMsgSize(maxMessageSize),
		grpc.KeepaliveParams(keepalive.ServerParameters{
			Time:    60 * time.Second,
			Timeout: 20 * time.Second,
		}),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             10 * time.Second,
			PermitWithoutStream: true,
		}),
	}
	if cfg.MaxConcurrentStreams > 0 {
		serverOptions = append(serverOptions, grpc.MaxConcurrentStreams(uint32(cfg.MaxConcurrentStreams)))
	}

	grpcServer := grpc.NewServer(serverOptions...)
	extprocv3.RegisterExternalProcessorServer(grpcServer, srv)

	healthServer := health.NewServer()
	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthServer.SetServingStatus(serviceName, healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(grpcServer, healthServer)

	go func() {
		slog.Info("serving", "grpc_port", cfg.GRPCPort, "mode", cfg.Mode)
		if err := grpcServer.Serve(grpcLis); err != nil && err != grpc.ErrServerStopped {
			slog.Error("gRPC server failed", "error", err)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	<-sigCh

	healthServer.SetServingStatus("", healthpb.HealthCheckResponse_NOT_SERVING)
	healthServer.SetServingStatus(serviceName, healthpb.HealthCheckResponse_NOT_SERVING)

	drainDuration := time.Duration(cfg.DrainSeconds * float64(time.Second))
	slog.Info("draining", "drain_seconds", cfg.DrainSeconds)
	if drainDuration > 0 {
		time.Sleep(drainDuration)
	}

	slog.Info("shutting down", "grace_seconds", cfg.ShutdownGrace)
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), time.Duration(cfg.ShutdownGrace*float64(time.Second)))
	defer shutdownCancel()

	stopped := make(chan struct{})
	go func() {
		grpcServer.GracefulStop()
		close(stopped)
	}()

	select {
	case <-stopped:
	case <-shutdownCtx.Done():
		grpcServer.Stop()
	}

	_ = metricsServer.Shutdown(context.Background())
}

func setupLogging() {
	level := slog.LevelInfo
	if os.Getenv("LOG_LEVEL") == "DEBUG" {
		level = slog.LevelDebug
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: level,
	}))
	slog.SetDefault(logger)
}
