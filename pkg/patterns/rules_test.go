package patterns

import (
	"reflect"
	"testing"
)

func TestLoadRules(t *testing.T) {
	rules, err := LoadRules("patterns.yaml")
	if err != nil {
		t.Fatalf("LoadRules failed: %v", err)
	}
	if len(rules) == 0 {
		t.Fatalf("expected rules, got 0")
	}

	countRules, err := LoadCountRules("patterns.yaml")
	if err != nil {
		t.Fatalf("LoadCountRules failed: %v", err)
	}
	if len(countRules) <= len(rules) {
		t.Fatalf("expected countRules > rules, got %d <= %d", len(countRules), len(rules))
	}
}

func TestDeriveRequiredPrefixes(t *testing.T) {
	cases := []struct {
		pattern  string
		expected []string
	}{
		{"gh[pousr]_[A-Za-z0-9]{20,}", []string{"gho_", "ghp_", "ghr_", "ghs_", "ghu_"}},
		{"github_pat_[A-Za-z0-9_]{20,}", []string{"github_pat_"}},
		{"sk-(?:ant-(?:admin01-|api03-|oat01-)?|proj-|svcacct-|admin-|or-v1-)?[A-Za-z0-9_-]{20,}", []string{"sk-"}},
		{"AKIA[0-9A-Z]{16}", []string{"AKIA"}},
		{"AGE-SECRET-KEY-1[A-Z0-9]{50,}", []string{"AGE-SECRET-KEY-1"}},
		{`\x24argon2id\x24v=19\x24m=[0-9]+`, []string{"$argon2id$v=19$m="}},
	}

	for _, c := range cases {
		got := DeriveRequired(c.pattern)
		if !reflect.DeepEqual(got, c.expected) {
			t.Errorf("DeriveRequired(%q) = %v; want %v", c.pattern, got, c.expected)
		}
	}
}
