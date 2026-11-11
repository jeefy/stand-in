package tokenizer

import (
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/jeefy/stand-in/pkg/patterns"
	"golang.org/x/crypto/blake2b"
)

const (
	PlaceholderPrefix = "<<secret-"
	PlaceholderHexLen = 12
	PlaceholderLen    = len(PlaceholderPrefix) + PlaceholderHexLen + 2 // 23
	MaxHold           = PlaceholderLen - 1                            // 22

	SaltEnv              = "STANDIN_SALT"
	DefaultSalt          = "stand-in-default-salt"
	CacheEnv             = "STANDIN_CACHE_ENTRIES"
	DefaultCacheEntries  = 50000
	CacheMinChars        = 512
	LiteralSep           = "\n\x00\n"
	DefaultBudgetSeconds = 2.0
)

var (
	PlaceholderRegex = regexp.MustCompile(`<<secret-[0-9a-f]{12}>>`)
	PartialRegex     = regexp.MustCompile(`^<(?:<(?:s(?:e(?:c(?:r(?:e(?:t(?:-(?:[0-9a-f]{12}>|[0-9a-f]{0,12}))?)?)?)?)?)?)?)?$`)

	pemTailRegex    = regexp.MustCompile(`^[A-Za-z0-9+/=\\ \t\r\n:,._-]{0,1000}-----END[ A-Z0-9_-]{0,40}PRIVATE KEY(?: BLOCK)?-----`)
	pemLinesRegex   = regexp.MustCompile(`^(?:(?:\r?\n|(?:\\r)?\\n)[ \t]*[A-Za-z0-9+/=]{16,})+`)
	afterDataKeyRe  = regexp.MustCompile(`^["']?[ \t]*:[ \t]*(?:[|>][-+0-9]*)?[ \t]*\r?$`)
	valueSepRegex   = regexp.MustCompile(`[:=][\s"']*`)
	jsonStringRegex = regexp.MustCompile(`"[^"\\]*(?:\\.[^"\\]*)*"`)
)

type Hit struct {
	Tier  string
	Index int
}

type Span struct {
	Start int
	End   int
	Hits  []Hit
}

type TokenizeResult struct {
	Text     string
	Mapping  map[string]string
	Hits     []Hit
	Replaced int
}

func (tr *TokenizeResult) Distinct() int {
	return len(tr.Mapping)
}

func (tr *TokenizeResult) TierCounts() map[string]int {
	counts := map[string]int{"a": 0, "b": 0}
	for _, h := range tr.Hits {
		counts[h.Tier]++
	}
	return counts
}

type NewCounts struct {
	Scope string
	Hits  []Hit
	Error string
}

func (nc *NewCounts) TierCounts() map[string]int {
	counts := map[string]int{"a": 0, "b": 0, "c": 0, "d": 0}
	for _, h := range nc.Hits {
		counts[h.Tier]++
	}
	return counts
}

type Tokenizer struct {
	Rules        []patterns.Rule
	CountRules   []patterns.Rule
	saltKey      []byte
	cacheMu      sync.RWMutex
	cache        map[string][]Span
	cacheOrder   []string
	cacheSize    int
	CacheHits    int
	countRulesMu sync.Mutex
}

func NewTokenizer(rules []patterns.Rule, countRules []patterns.Rule, salt string, cacheEntries int) (*Tokenizer, error) {
	if rules == nil {
		var err error
		rules, err = patterns.LoadRules("")
		if err != nil {
			return nil, err
		}
	}

	key := []byte(salt)
	if len(key) == 0 {
		key = []byte(DefaultSalt)
	}
	if len(key) > 64 {
		h, err := blake2b.New512(nil)
		if err != nil {
			return nil, err
		}
		h.Write(key)
		key = h.Sum(nil)
	}

	if cacheEntries < 0 {
		cacheEntries = DefaultCacheEntries
	}

	return &Tokenizer{
		Rules:      rules,
		CountRules: countRules,
		saltKey:    key,
		cache:      make(map[string][]Span),
		cacheSize:  cacheEntries,
	}, nil
}

func (t *Tokenizer) Placeholder(value string) string {
	h, err := blake2b.New(6, t.saltKey)
	if err != nil {
		panic(err)
	}
	h.Write([]byte(value))
	digest := hex.EncodeToString(h.Sum(nil))
	return fmt.Sprintf("%s%s>>", PlaceholderPrefix, digest)
}

func (t *Tokenizer) FindSpans(text string, rules []patterns.Rule, deadline *time.Time) ([]Span, error) {
	if rules == nil {
		rules = t.Rules
	}
	var found []Span
	var pem [][2]int
	var lineRules []patterns.Rule
	folded := newFolded(text)

	for _, rule := range rules {
		if rule.Handling == patterns.HandlingDataBlockLine {
			lineRules = append(lineRules, rule)
			continue
		}

		matches, err := ruleMatches(rule, text, folded, deadline)
		if err != nil {
			return nil, err
		}

		for _, m := range matches {
			start, end := m[0], m[1]
			if rule.KeyBoundary && start > 0 && isAsciiAlnum(text[start-1]) {
				continue
			}

			if rule.Handling == patterns.HandlingValueOnly {
				start, end = valueSpan(text, start, end, rule.Regex)
			} else if rule.Handling == patterns.HandlingPEMSpan {
				end = pemEnd(text, end)
			}

			if rule.Checksum != "" && !patterns.Checksums[rule.Checksum](text[start:end]) {
				continue
			}

			if len(rule.Reject) > 0 {
				val := text[start:end]
				rejected := false
				for _, rej := range rule.Reject {
					if rej.FindString(val) == val {
						rejected = true
						break
					}
				}
				if rejected {
					continue
				}
			}

			if rule.Handling == patterns.HandlingPEMSpan {
				pem = append(pem, [2]int{start, end})
			}

			found = append(found, Span{
				Start: start,
				End:   end,
				Hits:  []Hit{{Tier: rule.Tier, Index: rule.Index}},
			})
		}
	}

	for _, rule := range lineRules {
		found = append(found, dataBlockLines(rule, text, folded, pem)...)
	}

	return mergeSpans(found), nil
}

func (t *Tokenizer) TokenizeText(text string) (TokenizeResult, error) {
	spans, err := t.FindSpans(text, t.Rules, nil)
	if err != nil {
		return TokenizeResult{}, err
	}

	mapping := make(map[string]string)
	var hits []Hit
	var sb strings.Builder
	pos := 0

	for _, span := range spans {
		val := text[span.Start:span.End]
		ph := t.Placeholder(val)
		if _, ok := mapping[ph]; !ok {
			mapping[ph] = val
		}
		hits = append(hits, span.Hits...)
		sb.WriteString(text[pos:span.Start])
		sb.WriteString(ph)
		pos = span.End
	}
	sb.WriteString(text[pos:])

	return TokenizeResult{
		Text:     sb.String(),
		Mapping:  mapping,
		Hits:     hits,
		Replaced: len(spans),
	}, nil
}

func isAsciiAlnum(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9')
}

func valueSpan(text string, start, end int, re *regexp.Regexp) (int, int) {
	subnames := re.SubexpNames()
	valIdx := -1
	for i, name := range subnames {
		if name == "value" {
			valIdx = i
			break
		}
	}
	sub := text[start:end]
	if valIdx != -1 {
		indices := re.FindStringSubmatchIndex(sub)
		if indices != nil && indices[2*valIdx] >= 0 {
			vstart := start + indices[2*valIdx]
			vend := start + indices[2*valIdx+1]
			if vend > vstart {
				return vstart, vend
			}
		}
	}

	loc := valueSepRegex.FindStringIndex(sub)
	if loc == nil {
		return start, end
	}
	vstart := start + loc[1]
	vend := end
	for vend > vstart {
		last := text[vend-1]
		if last == '"' || last == '\'' || last == ' ' || last == '\t' {
			vend--
		} else {
			break
		}
	}
	if vend > vstart {
		return vstart, vend
	}
	return start, end
}

func pemEnd(text string, headerEnd int) int {
	tailLoc := pemTailRegex.FindStringIndex(text[headerEnd:])
	if tailLoc != nil {
		return headerEnd + tailLoc[1]
	}
	linesLoc := pemLinesRegex.FindStringIndex(text[headerEnd:])
	if linesLoc != nil {
		return headerEnd + linesLoc[1]
	}
	return headerEnd
}

type foldedText struct {
	raw    string
	folded string
	once   sync.Once
}

func newFolded(s string) *foldedText {
	return &foldedText{raw: s}
}

func (f *foldedText) Text() string {
	f.once.Do(func() {
		f.folded = strings.ToLower(f.raw)
	})
	return f.folded
}

func findAll(haystack, needle string) []int {
	var out []int
	i := strings.Index(haystack, needle)
	for i != -1 {
		out = append(out, i)
		next := strings.Index(haystack[i+1:], needle)
		if next == -1 {
			break
		}
		i = i + 1 + next
	}
	return out
}

func ruleMatches(rule patterns.Rule, text string, folded *foldedText, deadline *time.Time) ([][2]int, error) {
	if deadline != nil && time.Now().After(*deadline) {
		return nil, fmt.Errorf("count budget exceeded")
	}

	if len(rule.Required) == 0 {
		locs := rule.Regex.FindAllStringIndex(text, -1)
		var out [][2]int
		for _, loc := range locs {
			out = append(out, [2]int{loc[0], loc[1]})
		}
		return out, nil
	}

	haystack := text
	if rule.IgnoreCase() {
		haystack = folded.Text()
	}

	posSet := make(map[int]bool)
	for _, req := range rule.Required {
		for _, p := range findAll(haystack, req) {
			posSet[p] = true
		}
	}

	if len(posSet) == 0 {
		return nil, nil
	}

	var positions []int
	for p := range posSet {
		positions = append(positions, p)
	}
	sort.Ints(positions)

	var out [][2]int
	lastEnd := 0

	for _, q := range positions {
		if deadline != nil && time.Now().After(*deadline) {
			return nil, fmt.Errorf("count budget exceeded")
		}

		startMin := q - rule.Lead
		if lastEnd > startMin {
			startMin = lastEnd
		}

		for start := startMin; start <= q; start++ {
			if start >= len(text) {
				break
			}
			loc := rule.Regex.FindStringIndex(text[start:])
			if loc != nil && loc[0] == 0 {
				mStart := start
				mEnd := start + loc[1]
				out = append(out, [2]int{mStart, mEnd})
				if mEnd > start+1 {
					lastEnd = mEnd
				} else {
					lastEnd = start + 1
				}
				break
			}
		}
	}

	return out, nil
}

func dataBlockLines(rule patterns.Rule, text string, folded *foldedText, pem [][2]int) []Span {
	tag := Hit{Tier: rule.Tier, Index: rule.Index}
	var accepted []Span

	for _, p := range pem {
		sub := text[p[0]:p[1]]
		locs := rule.Regex.FindAllStringIndex(sub, -1)
		for _, loc := range locs {
			s, e := stripBlank(text, p[0]+loc[0], p[0]+loc[1])
			if e > s {
				accepted = append(accepted, Span{Start: s, End: e, Hits: []Hit{tag}})
			}
		}
	}

	foldedStr := folded.Text()
	for _, q := range findAll(foldedStr, "data") {
		if q+4 >= len(text) {
			continue
		}
		nextC := text[q+4]
		if nextC != ':' && nextC != '"' && nextC != '\'' {
			continue
		}
		lineEnd := strings.IndexByte(text[q:], '\n')
		if lineEnd == -1 {
			continue
		}
		absLineEnd := q + lineEnd
		lineHeader := text[q+4 : absLineEnd]
		if !afterDataKeyRe.MatchString(lineHeader) {
			continue
		}
		if !isDataKey(text, foldedStr, q) {
			continue
		}

		pos := absLineEnd + 1
		for pos < len(text) {
			loc := rule.Regex.FindStringIndex(text[pos:])
			if loc == nil || loc[0] != 0 {
				break
			}
			mEnd := pos + loc[1]
			s, e := stripBlank(text, pos, mEnd)
			if e > s {
				accepted = append(accepted, Span{Start: s, End: e, Hits: []Hit{tag}})
			}
			if mEnd >= len(text) || text[mEnd] != '\n' {
				break
			}
			pos = mEnd + 1
		}
	}

	return accepted
}

func isDataKey(text string, folded string, q int) bool {
	k := q
	for k > 0 && isKeyChar(text[k-1]) {
		k--
	}
	if k > 0 && !isKeyBoundary(text[k-1]) {
		return false
	}
	prefix := folded[k:q]
	return prefix == "" || prefix == "binary" || strings.HasSuffix(prefix, "-") || strings.HasSuffix(prefix, "_") || strings.HasSuffix(prefix, ".")
}

func isKeyChar(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9') || b == '_' || b == '.' || b == '-'
}

func isKeyBoundary(b byte) bool {
	return b == ' ' || b == '\t' || b == '\n' || b == '\r' || b == '"' || b == '\'' || b == '{' || b == ','
}

func stripBlank(text string, start, end int) (int, int) {
	for start < end && (text[start] == ' ' || text[start] == '\t') {
		start++
	}
	for end > start && (text[end-1] == ' ' || text[end-1] == '\t' || text[end-1] == '\r') {
		end--
	}
	return start, end
}

func mergeSpans(spans []Span) []Span {
	sort.Slice(spans, func(i, j int) bool {
		if spans[i].Start != spans[j].Start {
			return spans[i].Start < spans[j].Start
		}
		return spans[i].End > spans[j].End
	})

	var merged []Span
	for _, span := range spans {
		if span.End <= span.Start {
			continue
		}
		if len(merged) > 0 && span.Start < merged[len(merged)-1].End {
			last := &merged[len(merged)-1]
			if span.End > last.End {
				last.End = span.End
			}
			last.Hits = append(last.Hits, span.Hits...)
		} else {
			hitsCopy := make([]Hit, len(span.Hits))
			copy(hitsCopy, span.Hits)
			merged = append(merged, Span{
				Start: span.Start,
				End:   span.End,
				Hits:  hitsCopy,
			})
		}
	}
	return merged
}

func PartialHoldPoint(data string) int {
	n := len(data)
	start := n - MaxHold
	if start < 0 {
		start = 0
	}
	i := strings.IndexByte(data[start:], '<')
	for i != -1 {
		idx := start + i
		if PartialRegex.MatchString(data[idx:]) {
			return idx
		}
		next := strings.IndexByte(data[idx+1:], '<')
		if next == -1 {
			break
		}
		i = (idx + 1 + next) - start
	}
	return n
}

func PartialHoldPointBytes(data []byte) int {
	return PartialHoldPoint(string(data))
}

func SecureCompare(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}
