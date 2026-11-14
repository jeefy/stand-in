package tokenizer

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/jeefy/stand-in/pkg/patterns"
)

func TestPlaceholderDeterminism(t *testing.T) {
	tok1, err := NewTokenizer(nil, nil, "my-secret-salt", 0)
	if err != nil {
		t.Fatalf("NewTokenizer failed: %v", err)
	}
	tok2, err := NewTokenizer(nil, nil, "my-secret-salt", 0)
	if err != nil {
		t.Fatalf("NewTokenizer failed: %v", err)
	}

	secret := "ghp_1234567890abcdef1234567890abcdef"
	ph1 := tok1.Placeholder(secret)
	ph2 := tok2.Placeholder(secret)

	if ph1 != ph2 {
		t.Fatalf("Placeholders differ for same salt: %s != %s", ph1, ph2)
	}
	if !strings.HasPrefix(ph1, PlaceholderPrefix) || !strings.HasSuffix(ph1, ">>") {
		t.Fatalf("Invalid placeholder format: %s", ph1)
	}
	if len(ph1) != PlaceholderLen {
		t.Fatalf("Expected len %d, got %d for %s", PlaceholderLen, len(ph1), ph1)
	}
}

func TestTokenizeJSON(t *testing.T) {
	rules, err := patterns.LoadRules("../patterns/patterns.yaml")
	if err != nil {
		t.Fatalf("LoadRules: %v", err)
	}
	tok, err := NewTokenizer(rules, nil, "test-salt", 100)
	if err != nil {
		t.Fatalf("NewTokenizer: %v", err)
	}

	rawJSON := `{"prompt": "here is my token ghp_0123456789abcdef0123456789abcdef and done"}`
	res, err := tok.TokenizeJSON([]byte(rawJSON))
	if err != nil {
		t.Fatalf("TokenizeJSON error: %v", err)
	}
	if res == nil {
		t.Fatalf("expected non-nil result")
	}

	if len(res.Mapping) != 1 {
		t.Fatalf("expected 1 mapped placeholder, got %d", len(res.Mapping))
	}

	var parsed map[string]string
	if err := json.Unmarshal([]byte(res.Text), &parsed); err != nil {
		t.Fatalf("failed to parse tokenized json: %v", err)
	}

	for ph, original := range res.Mapping {
		if original != "ghp_0123456789abcdef0123456789abcdef" {
			t.Errorf("wrong original secret: %s", original)
		}
		if !strings.Contains(parsed["prompt"], ph) {
			t.Errorf("expected placeholder %s in output: %s", ph, parsed["prompt"])
		}
	}
}

func TestPartialHoldPoint(t *testing.T) {
	cases := []struct {
		input string
		want  int
	}{
		{"hello world", 11},
		{"hello <", 6},
		{"hello <<", 6},
		{"hello <<secret-", 6},
		{"hello <<secret-1a2b", 6},
		{"hello <<secret-1a2b3c4d5e6f>", 6},
		{"hello <<secret-1a2b3c4d5e6f>>", 29}, // len is 29, complete placeholder is not held
		{"something <other", 16},
	}

	for _, c := range cases {
		got := PartialHoldPoint(c.input)
		if got != c.want {
			t.Errorf("PartialHoldPoint(%q) = %d; want %d", c.input, got, c.want)
		}
	}
}

func TestChecksumRules(t *testing.T) {
	rules, err := patterns.LoadRules("../patterns/patterns.yaml")
	if err != nil {
		t.Fatalf("LoadRules: %v", err)
	}
	tok, err := NewTokenizer(rules, nil, "test-salt", 0)
	if err != nil {
		t.Fatalf("NewTokenizer: %v", err)
	}
	for _, s := range []string{"4111 1111 1111 1111", "DE89 3704 0044 0532 0130 00"} {
		res, err := tok.TokenizeText("pay " + s + " now")
		if err != nil || res.Replaced != 1 {
			t.Errorf("%q: replaced=%d err=%v", s, res.Replaced, err)
		}
	}
	for _, s := range []string{"4111 1111 1111 1112", "1731340800001", "DE88 3704 0044 0532 0130 00", "a4111111111111111"} {
		res, err := tok.TokenizeText("pay " + s + " now")
		if err != nil || res.Replaced != 0 {
			t.Errorf("%q: replaced=%d err=%v", s, res.Replaced, err)
		}
	}
}

func TestBase64ImageNotTokenized(t *testing.T) {
	rules, err := patterns.LoadRules("../patterns/patterns.yaml")
	if err != nil {
		t.Fatal(err)
	}
	tok, err := NewTokenizer(rules, nil, "test-salt", 100)
	if err != nil {
		t.Fatal(err)
	}
	blob := "iVBORw0KGgoAAAANSUhEUgAAAlEAAAGk" + strings.Repeat("CAYAAAARy3DpAA", 12) // contains EAA + 100+ alnum
	body := `{"image_url":{"url":"data:image/png;base64,` + blob + `"}}`
	res, err := tok.TokenizeJSON([]byte(body))
	if err != nil {
		t.Fatal(err)
	}
	if res.Text != body || len(res.Mapping) != 0 {
		t.Fatalf("base64 image was rewritten: %d mappings", len(res.Mapping))
	}
	real := "EAA" + strings.Repeat("Z", 120)
	res, _ = tok.TokenizeJSON([]byte(`{"t":"tok ` + real + `"}`))
	if len(res.Mapping) != 1 {
		t.Fatalf("standalone Meta token not detected")
	}
}

func TestSkipBase64Payloads(t *testing.T) {
	rules, err := patterns.LoadRules("../patterns/patterns.yaml")
	if err != nil {
		t.Fatalf("LoadRules: %v", err)
	}
	tok, err := NewTokenizer(rules, nil, "test-salt", 100)
	if err != nil {
		t.Fatalf("NewTokenizer: %v", err)
	}
	blob := strings.Repeat("iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", 10) + "EAA" + strings.Repeat("a1B2c3D4e5", 10) + "AKIA" + "ABCDEFGHIJKLMNOP" + strings.Repeat("QRSTuvwx", 5)
	fake := "ghp_" + strings.Repeat("aB3dE6gH9j", 3) + "abcdef"
	openai := `{"messages":[{"content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,` + blob + `"}}]}]}`
	anth := `{"messages":[{"content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":"` + blob + `"}}]}]}`
	for name, body := range map[string]string{"openai": openai, "anthropic": anth} {
		res, err := tok.TokenizeJSON([]byte(body))
		if err != nil || res == nil {
			t.Fatalf("%s: %v", name, err)
		}
		if res.Text != body || res.Replaced != 0 {
			t.Errorf("%s: payload modified (replaced=%d)", name, res.Replaced)
		}
		with := strings.Replace(body, `{"messages"`, `{"note":"tok `+fake+`","messages"`, 1)
		res, err = tok.TokenizeJSON([]byte(with))
		if err != nil || res == nil {
			t.Fatalf("%s: %v", name, err)
		}
		if strings.Contains(res.Text, fake) || !strings.Contains(res.Text, blob) {
			t.Errorf("%s: ghp token not tokenized or blob altered", name)
		}
	}
	// Secrets in non-image payloads must still be scanned.
	secret := "EAA" + strings.Repeat("a1B2c3D4e5", 12)
	for name, body := range map[string]string{
		"data-field": `{"data":"` + strings.Repeat("QUJD", 70) + "+" + secret + `"}`,
		"text-uri":   `{"u":"data:text/plain;base64,` + secret + `"}`,
	} {
		res, err := tok.TokenizeJSON([]byte(body))
		if err != nil || res == nil {
			t.Fatalf("%s: %v", name, err)
		}
		if strings.Contains(res.Text, secret) {
			t.Errorf("%s: secret in non-image payload was skipped", name)
		}
	}
}

// Many required-literal hits with no match must stay linear (was O(n^2): 60KB log took ~60s).
func TestManyPrefilterHitsLinear(t *testing.T) {
	rules, err := patterns.LoadRules("../patterns/patterns.yaml")
	if err != nil {
		t.Fatal(err)
	}
	tok, err := NewTokenizer(rules, nil, "test-salt", 100)
	if err != nil {
		t.Fatal(err)
	}
	text := strings.Repeat("ts=12:34:56 level=info msg=x: y\n", 4000) + "token ghp_" + strings.Repeat("a", 36)
	start := time.Now()
	res, err := tok.TokenizeText(text)
	if err != nil {
		t.Fatal(err)
	}
	if d := time.Since(start); d > 2*time.Second {
		t.Fatalf("scrubbing 128KB took %v", d)
	}
	if len(res.Mapping) != 1 {
		t.Fatalf("expected 1 secret, got %d", len(res.Mapping))
	}
}
