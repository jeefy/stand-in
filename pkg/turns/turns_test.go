package turns

import (
	"encoding/json"
	"testing"
)

func TestNewTurnStrings(t *testing.T) {
	// First turn (no assistant message)
	doc1 := map[string]any{
		"messages": []any{
			map[string]any{"role": "user", "content": "hello there"},
		},
	}
	scope1, strings1 := NewTurnStrings(doc1)
	if scope1 != ScopeFirst {
		t.Errorf("expected ScopeFirst, got %v", scope1)
	}
	if len(strings1) != 2 || strings1[0] != "user" || strings1[1] != "hello there" {
		t.Errorf("unexpected strings: %v", strings1)
	}

	// Subsequent turn with assistant message
	doc2 := map[string]any{
		"messages": []any{
			map[string]any{"role": "user", "content": "first question"},
			map[string]any{"role": "assistant", "content": "first answer"},
			map[string]any{"role": "user", "content": "second question"},
		},
	}
	scope2, strings2 := NewTurnStrings(doc2)
	if scope2 != ScopeTail {
		t.Errorf("expected ScopeTail, got %v", scope2)
	}
	if len(strings2) != 2 || strings2[0] != "user" || strings2[1] != "second question" {
		t.Errorf("unexpected strings: %v", strings2)
	}

	// Trailing assistant message has nothing after it, so it should not be treated as boundary
	doc3 := map[string]any{
		"messages": []any{
			map[string]any{"role": "user", "content": "hello"},
			map[string]any{"role": "assistant", "content": "prefill"},
		},
	}
	scope3, _ := NewTurnStrings(doc3)
	if scope3 != ScopeFirst {
		t.Errorf("expected ScopeFirst for trailing assistant prefill, got %v", scope3)
	}

	// Input with previous_response_id
	doc4 := map[string]any{
		"previous_response_id": "resp_123",
		"input": []any{
			map[string]any{"type": "message", "role": "user", "content": "new input"},
		},
	}
	scope4, strings4 := NewTurnStrings(doc4)
	if scope4 != ScopeInputOnly {
		t.Errorf("expected ScopeInputOnly, got %v", scope4)
	}
	if len(strings4) != 3 {
		t.Fatalf("expected 3 strings, got %d: %v", len(strings4), strings4)
	}
	sSet := make(map[string]bool)
	for _, s := range strings4 {
		sSet[s] = true
	}
	if !sSet["message"] || !sSet["user"] || !sSet["new input"] {
		t.Errorf("unexpected strings set: %v", strings4)
	}

	// Base64 and image skip
	raw := `{"messages": [{"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "base64", "data": "SGVsbG8="}, {"image_url": "http://example.com/img.png"}]}]}`
	var doc5 any
	if err := json.Unmarshal([]byte(raw), &doc5); err != nil {
		t.Fatal(err)
	}
	_, strings5 := NewTurnStrings(doc5)
	if len(strings5) != 2 || strings5[0] != "look" || strings5[1] != "text" {
		// "look" and "text" are included; data and image_url are skipped
		for _, s := range strings5 {
			if s == "SGVsbG8=" || s == "http://example.com/img.png" {
				t.Errorf("found skipped string: %s", s)
			}
		}
	}
}
