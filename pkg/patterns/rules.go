package patterns

import (
	_ "embed"
	"fmt"
	"os"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"
)

//go:embed patterns.yaml
var DefaultPatternsYAML []byte

const PatternsEnv = "STANDIN_PATTERNS_FILE"

type Handling string

const (
	HandlingWhole         Handling = "whole"
	HandlingValueOnly     Handling = "value_only"
	HandlingPEMSpan       Handling = "pem_span"
	HandlingDataBlockLine Handling = "data_block_line"
)

var validHandlings = map[Handling]bool{
	HandlingWhole:         true,
	HandlingValueOnly:     true,
	HandlingPEMSpan:       true,
	HandlingDataBlockLine: true,
}

type Rule struct {
	Tier     string
	Index    int
	Pattern  string
	Handling Handling
	Regex    *regexp.Regexp
	// Anchored matches Regex only at the start of the input (\A). The
	// prefilter and data-block scanners probe single offsets; running the
	// unanchored Regex there scans the whole remaining text on every miss,
	// which is quadratic on large bodies.
	Anchored    *regexp.Regexp
	Required    []string
	Lead        int
	Reject      []*regexp.Regexp
	KeyBoundary bool
	Checksum    string
}

func (r *Rule) IgnoreCase() bool {
	return strings.HasPrefix(r.Pattern, "(?i)")
}

type rawRule struct {
	Pattern     string   `yaml:"pattern"`
	Handling    string   `yaml:"handling"`
	Required    []string `yaml:"required"`
	Lead        *int     `yaml:"lead"`
	Reject      []string `yaml:"reject"`
	KeyBoundary *bool    `yaml:"key_boundary"`
	Checksum    string   `yaml:"checksum"`
}

type rawFile struct {
	TierA []rawRule `yaml:"tier_a"`
	TierB []rawRule `yaml:"tier_b"`
	TierC []rawRule `yaml:"tier_c"`
	TierD []rawRule `yaml:"tier_d"`
}

func loadYAMLBytes(path string) ([]byte, error) {
	if path != "" {
		return os.ReadFile(path)
	}
	if env := os.Getenv(PatternsEnv); env != "" {
		return os.ReadFile(env)
	}
	return DefaultPatternsYAML, nil
}

func LoadRules(path string) ([]Rule, error) {
	data, err := loadYAMLBytes(path)
	if err != nil {
		return nil, fmt.Errorf("read patterns: %w", err)
	}
	var rf rawFile
	if err := yaml.Unmarshal(data, &rf); err != nil {
		return nil, fmt.Errorf("unmarshal patterns: %w", err)
	}

	var rules []Rule
	for i, r := range rf.TierA {
		rule, err := buildRule("a", i, r)
		if err != nil {
			return nil, err
		}
		rules = append(rules, rule)
	}
	for i, r := range rf.TierB {
		rule, err := buildRule("b", i, r)
		if err != nil {
			return nil, err
		}
		rules = append(rules, rule)
	}
	if len(rules) == 0 {
		return nil, fmt.Errorf("patterns.yaml has no rules")
	}
	return rules, nil
}

func LoadCountRules(path string) ([]Rule, error) {
	data, err := loadYAMLBytes(path)
	if err != nil {
		return nil, fmt.Errorf("read patterns: %w", err)
	}
	var rf rawFile
	if err := yaml.Unmarshal(data, &rf); err != nil {
		return nil, fmt.Errorf("unmarshal patterns: %w", err)
	}

	var rules []Rule
	for i, r := range rf.TierA {
		rule, err := buildRule("a", i, r)
		if err != nil {
			return nil, err
		}
		rules = append(rules, rule)
	}
	for i, r := range rf.TierB {
		rule, err := buildRule("b", i, r)
		if err != nil {
			return nil, err
		}
		rules = append(rules, rule)
	}
	for i, r := range rf.TierC {
		rule, err := buildRule("c", i, r)
		if err != nil {
			return nil, err
		}
		rules = append(rules, rule)
	}
	for i, r := range rf.TierD {
		rule, err := buildRule("d", i, r)
		if err != nil {
			return nil, err
		}
		rules = append(rules, rule)
	}
	if len(rules) == 0 {
		return nil, fmt.Errorf("patterns.yaml has no count rules")
	}
	return rules, nil
}

func buildRule(tier string, index int, raw rawRule) (Rule, error) {
	where := fmt.Sprintf("tier %s rule %d", tier, index)
	if raw.Pattern == "" {
		return Rule{}, fmt.Errorf("%s: pattern is empty", where)
	}

	handling := HandlingWhole
	if raw.Handling != "" {
		h := Handling(raw.Handling)
		if !validHandlings[h] {
			return Rule{}, fmt.Errorf("%s: unknown handling %q", where, raw.Handling)
		}
		handling = h
	}

	re, err := regexp.Compile(raw.Pattern)
	if err != nil {
		return Rule{}, fmt.Errorf("%s: compile regex: %w", where, err)
	}

	anchored, err := regexp.Compile(`\A(?:` + raw.Pattern + `)`)
	if err != nil {
		return Rule{}, fmt.Errorf("%s: compile anchored regex: %w", where, err)
	}

	lead := 0
	if raw.Lead != nil {
		if *raw.Lead < 0 {
			return Rule{}, fmt.Errorf("%s: lead must be non-negative", where)
		}
		lead = *raw.Lead
	}

	keyBoundary := false
	if raw.KeyBoundary != nil {
		keyBoundary = *raw.KeyBoundary
	}

	if raw.Checksum != "" && Checksums[raw.Checksum] == nil {
		return Rule{}, fmt.Errorf("%s: unknown checksum %q", where, raw.Checksum)
	}

	var reject []*regexp.Regexp
	for _, rej := range raw.Reject {
		r, err := regexp.Compile(rej)
		if err != nil {
			return Rule{}, fmt.Errorf("%s: compile reject regex: %w", where, err)
		}
		reject = append(reject, r)
	}

	var required []string
	if raw.Required != nil {
		required = raw.Required
	} else {
		required = DeriveRequired(raw.Pattern)
	}

	if strings.HasPrefix(raw.Pattern, "(?i)") {
		seen := make(map[string]bool)
		var lowerReq []string
		for _, req := range required {
			l := strings.ToLower(req)
			if !seen[l] {
				seen[l] = true
				lowerReq = append(lowerReq, l)
			}
		}
		required = lowerReq
	}

	return Rule{
		Tier:        tier,
		Index:       index,
		Pattern:     raw.Pattern,
		Handling:    handling,
		Regex:       re,
		Anchored:    anchored,
		Required:    required,
		Lead:        lead,
		Reject:      reject,
		KeyBoundary: keyBoundary,
		Checksum:    raw.Checksum,
	}, nil
}
