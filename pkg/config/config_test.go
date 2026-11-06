package config

import (
	"os"
	"testing"
)

func TestConfigValidation(t *testing.T) {
	origMode := os.Getenv("STANDIN_MODE")
	origChunk := os.Getenv("STANDIN_CHUNK_BYTES")
	origDrain := os.Getenv("STANDIN_DRAIN_SECONDS")
	defer func() {
		os.Setenv("STANDIN_MODE", origMode)
		os.Setenv("STANDIN_CHUNK_BYTES", origChunk)
		os.Setenv("STANDIN_DRAIN_SECONDS", origDrain)
	}()

	os.Setenv("STANDIN_MODE", "  Enforce ")
	os.Setenv("STANDIN_SALT", "test-salt")
	os.Setenv("STANDIN_CHUNK_BYTES", "0")
	os.Setenv("STANDIN_DRAIN_SECONDS", "2.5")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("unexpected error loading config: %v", err)
	}

	if cfg.Mode != "enforce" {
		t.Errorf("expected normalized mode enforce, got %s", cfg.Mode)
	}
	if cfg.ChunkBytes != DefaultChunkBytes {
		t.Errorf("expected chunk bytes fallback %d, got %d", DefaultChunkBytes, cfg.ChunkBytes)
	}
	if cfg.DrainSeconds != 2.5 {
		t.Errorf("expected float drain seconds 2.5, got %f", cfg.DrainSeconds)
	}

	os.Setenv("STANDIN_MODE", "invalid-mode")
	_, err = Load()
	if err == nil {
		t.Errorf("expected error on invalid mode")
	}

	os.Setenv("STANDIN_MODE", "shadow")
	os.Setenv("STANDIN_CHUNK_BYTES", "-5")
	_, err = Load()
	if err == nil {
		t.Errorf("expected error on negative chunk bytes")
	}
}
