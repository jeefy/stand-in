package tokenizer

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"

	"golang.org/x/crypto/blake2b"
)

func (t *Tokenizer) TokenizeJSON(body []byte) (*TokenizeResult, error) {
	if !utf8.Valid(body) {
		return nil, nil
	}
	if !json.Valid(body) {
		return nil, nil
	}

	raw := string(body)
	matches := jsonStringRegex.FindAllStringIndex(raw, -1)
	if len(matches) == 0 {
		return &TokenizeResult{Text: raw, Mapping: make(map[string]string)}, nil
	}

	decoded := make(map[int]string)
	perLiteral := make(map[int][]Span)
	var scanIdx []int
	var scanStarts []int
	var scanParts []string
	toCache := make(map[int]string)
	pos := 0

	t.cacheMu.Lock()
	for idx, m := range matches {
		inner := raw[m[0]+1 : m[1]-1]
		if isBase64Data(raw, m, inner) {
			continue
		}
		if len(inner) >= CacheMinChars && t.cacheSize > 0 {
			h, err := blake2b.New(16, t.saltKey)
			if err == nil {
				h.Write([]byte(inner))
				key := hex.EncodeToString(h.Sum(nil))
				if cached, ok := t.cache[key]; ok {
					t.CacheHits++
					if len(cached) > 0 {
						val, err := decodeLiteral(raw[m[0]:m[1]], inner)
						if err == nil {
							decoded[idx] = val
							cachedCopy := make([]Span, len(cached))
							copy(cachedCopy, cached)
							perLiteral[idx] = cachedCopy
						}
					}
					continue
				}
				toCache[idx] = key
			}
		}

		val, err := decodeLiteral(raw[m[0]:m[1]], inner)
		if err != nil {
			continue
		}
		decoded[idx] = val
		scanIdx = append(scanIdx, idx)
		scanStarts = append(scanStarts, pos)
		scanParts = append(scanParts, val)
		pos += len(val) + len(LiteralSep)
	}
	t.cacheMu.Unlock()

	if len(scanParts) > 0 {
		joined := strings.Join(scanParts, LiteralSep)
		spans, err := t.FindSpans(joined, t.Rules, nil)
		if err != nil {
			return nil, err
		}

		for _, span := range spans {
			n := sort.Search(len(scanStarts), func(i int) bool {
				return scanStarts[i] > span.Start
			}) - 1
			if n < 0 {
				n = 0
			}
			idx := scanIdx[n]
			base := scanStarts[n]
			end := span.End - base
			decLen := len(decoded[idx])
			if end > decLen {
				end = decLen
			}
			spanCopy := Span{
				Start: span.Start - base,
				End:   end,
				Hits:  append([]Hit{}, span.Hits...),
			}
			perLiteral[idx] = append(perLiteral[idx], spanCopy)
		}
	}

	if len(toCache) > 0 && t.cacheSize > 0 {
		t.cacheMu.Lock()
		for idx, key := range toCache {
			spans := perLiteral[idx]
			if len(t.cache) >= t.cacheSize && len(t.cacheOrder) > 0 {
				oldest := t.cacheOrder[0]
				t.cacheOrder = t.cacheOrder[1:]
				delete(t.cache, oldest)
			}
			if _, exists := t.cache[key]; !exists {
				t.cacheOrder = append(t.cacheOrder, key)
			}
			t.cache[key] = spans
		}
		t.cacheMu.Unlock()
	}

	if len(perLiteral) == 0 {
		return &TokenizeResult{Text: raw, Mapping: make(map[string]string), Hits: nil, Replaced: 0}, nil
	}

	var sortedIndices []int
	for idx := range perLiteral {
		sortedIndices = append(sortedIndices, idx)
	}
	sort.Ints(sortedIndices)

	mapping := make(map[string]string)
	var hits []Hit
	var out strings.Builder
	cursor := 0
	replaced := 0

	for _, idx := range sortedIndices {
		text := decoded[idx]
		m := matches[idx]
		var pieces strings.Builder
		p := 0
		for _, span := range perLiteral[idx] {
			if span.Start < p || span.End > len(text) || span.Start > span.End {
				continue
			}
			val := text[span.Start:span.End]
			ph := t.Placeholder(val)
			if _, ok := mapping[ph]; !ok {
				mapping[ph] = val
			}
			hits = append(hits, span.Hits...)
			pieces.WriteString(text[p:span.Start])
			pieces.WriteString(ph)
			p = span.End
			replaced++
		}
		pieces.WriteString(text[p:])

		out.WriteString(raw[cursor:m[0]])
		encoded := encodeLiteral(pieces.String())
		out.WriteString(encoded)
		cursor = m[1]
	}
	out.WriteString(raw[cursor:])

	return &TokenizeResult{
		Text:     out.String(),
		Mapping:  mapping,
		Hits:     hits,
		Replaced: replaced,
	}, nil
}

func decodeLiteral(literal string, inner string) (string, error) {
	if !strings.ContainsRune(inner, '\\') {
		return inner, nil
	}
	var s string
	err := json.Unmarshal([]byte(literal), &s)
	if err != nil {
		return inner, err
	}
	return s, nil
}

func encodeLiteral(value string) string {
	b, err := json.Marshal(value)
	if err != nil {
		return fmt.Sprintf("%q", value)
	}
	b = bytes.ReplaceAll(b, []byte("\\u003c"), []byte("<"))
	b = bytes.ReplaceAll(b, []byte("\\u003e"), []byte(">"))
	b = bytes.ReplaceAll(b, []byte("\\u0026"), []byte("&"))
	return string(b)
}

func JSONEscaped(value string) string {
	b, err := json.Marshal(value)
	if err != nil {
		return value
	}
	s := string(b)
	if len(s) >= 2 && s[0] == '"' && s[len(s)-1] == '"' {
		return s[1 : len(s)-1]
	}
	return s
}

var (
	base64Only    = regexp.MustCompile(`^[A-Za-z0-9+/=]+$`)
	dataKeyBefore = regexp.MustCompile(`"data"\s*:\s*$`)
)

// isBase64Data matches Anthropic {"type":"base64","data":"..."} payloads.
// shortcut: key+charset+length heuristic, not a sibling "type" check; upgrade if false positives appear.
func isBase64Data(raw string, m []int, inner string) bool {
	return len(inner) >= 256 && base64Only.MatchString(inner) && dataKeyBefore.MatchString(raw[:m[0]])
}
