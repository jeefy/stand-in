package restorer

import (
	"strings"
	"testing"
)

func TestByteRestorer(t *testing.T) {
	mapping := map[string]string{
		"<<secret-1a2b3c4d5e6f>>": "SUPER_SECRET_KEY",
	}
	br := NewByteRestorer(mapping)

	// Feed in two chunks split across the placeholder
	c1 := br.Feed([]byte("hello <<secret-1a2b"))
	c2 := br.Feed([]byte("3c4d5e6f>> world"))
	c3 := br.Flush()

	result := string(c1) + string(c2) + string(c3)
	expected := "hello SUPER_SECRET_KEY world"
	if result != expected {
		t.Fatalf("ByteRestorer mismatch: got %q, want %q", result, expected)
	}
	if br.Restored != 1 {
		t.Errorf("expected 1 restored, got %d", br.Restored)
	}
}

func TestJsonRestorer(t *testing.T) {
	mapping := map[string]string{
		"<<secret-1a2b3c4d5e6f>>": "SUPER_SECRET_KEY",
		"<<secret-aaaaaaaaaaaa>>": "NESTED_\"QUOTED\"_ARG",
	}
	jr := NewJsonRestorer(mapping, 1024*1024)

	jsonInput := `{"message":{"content":"your secret is <<secret-1a2b3c4d5e6f>>"},"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\"arg\":\"<<secret-aaaaaaaaaaaa>>\"}"}}]}}]}`

	_ = jr.Feed([]byte(jsonInput))
	restored := jr.Flush()

	out := string(restored)
	if !strings.Contains(out, "SUPER_SECRET_KEY") {
		t.Errorf("expected SUPER_SECRET_KEY in out: %s", out)
	}
	if !strings.Contains(out, `NESTED_\\\"QUOTED\\\"_ARG`) {
		t.Errorf("expected double-escaped NESTED_\\\\\\\"QUOTED\\\\\\\"_ARG in out: %s", out)
	}
}

func TestSseRestorer(t *testing.T) {
	mapping := map[string]string{
		"<<secret-1a2b3c4d5e6f>>": "STREAMED_SECRET",
	}
	sr := NewSseRestorer(mapping)

	chunk1 := "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"start <<sec\"}}]}\n\n"
	chunk2 := "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"ret-1a2b\"}}]}\n\n"
	chunk3 := "data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"3c4d5e6f>> end\"}}]}\n\n"
	chunk4 := "data: [DONE]\n\n"

	out := string(sr.Feed([]byte(chunk1)))
	out += string(sr.Feed([]byte(chunk2)))
	out += string(sr.Feed([]byte(chunk3)))
	out += string(sr.Feed([]byte(chunk4)))
	out += string(sr.Flush())

	if !strings.Contains(out, "STREAMED_SECRET") {
		t.Fatalf("expected STREAMED_SECRET restored in SSE output: %s", out)
	}
}
