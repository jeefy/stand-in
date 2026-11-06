package restorer

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/jeefy/stand-in/pkg/tokenizer"
)

var (
	JSONFields = map[string]bool{
		"arguments":    true,
		"partial_json": true,
	}
	OpaqueKeys = map[string]bool{
		"input": true,
	}
	DefaultMaxBufferBytes = 64 << 20
)

type PlaceholderSubstituter struct {
	textTable  map[string]string
	jsonTable  map[string]string
	bytesTable map[string][]byte
	Restored   int
}

func NewPlaceholderSubstituter(mapping map[string]string) *PlaceholderSubstituter {
	textTable := make(map[string]string)
	jsonTable := make(map[string]string)
	bytesTable := make(map[string][]byte)

	for ph, v := range mapping {
		textTable[ph] = v
		escaped := tokenizer.JSONEscaped(v)
		jsonTable[ph] = escaped
		bytesTable[ph] = []byte(escaped)
	}

	return &PlaceholderSubstituter{
		textTable:  textTable,
		jsonTable:  jsonTable,
		bytesTable: bytesTable,
	}
}

func (ps *PlaceholderSubstituter) Replace(text string, kind string) string {
	if !strings.Contains(text, tokenizer.PlaceholderPrefix) {
		return text
	}
	table := ps.textTable
	if kind == "json" {
		table = ps.jsonTable
	}

	return tokenizer.PlaceholderRegex.ReplaceAllStringFunc(text, func(m string) string {
		val, ok := table[m]
		if !ok {
			return m
		}
		ps.Restored++
		return val
	})
}

func (ps *PlaceholderSubstituter) ReplaceBytes(data []byte) []byte {
	if !bytes.Contains(data, secretMarkerBytes) {
		return data
	}

	return PlaceholderRegexBytes.ReplaceAllFunc(data, func(m []byte) []byte {
		val, ok := ps.bytesTable[string(m)]
		if !ok {
			return m
		}
		ps.Restored++
		return val
	})
}

func (ps *PlaceholderSubstituter) ReplaceTree(node any, isOpaque bool) (any, bool) {
	changed := false
	switch v := node.(type) {
	case string:
		newVal := ps.Replace(v, "text")
		if newVal != v {
			return newVal, true
		}
		return v, false
	case map[string]any:
		for k, val := range v {
			opaque := isOpaque || OpaqueKeys[k]
			kind := "text"
			if !opaque && JSONFields[k] {
				kind = "json"
			}

			if s, ok := val.(string); ok {
				newS := ps.Replace(s, kind)
				if newS != s {
					v[k] = newS
					changed = true
				}
			} else {
				updated, subChanged := ps.ReplaceTree(val, opaque)
				if subChanged {
					v[k] = updated
					changed = true
				}
			}
		}
		return v, changed
	case []any:
		for i, val := range v {
			if s, ok := val.(string); ok {
				newS := ps.Replace(s, "text")
				if newS != s {
					v[i] = newS
					changed = true
				}
			} else {
				updated, subChanged := ps.ReplaceTree(val, isOpaque)
				if subChanged {
					v[i] = updated
					changed = true
				}
			}
		}
		return v, changed
	}
	return node, false
}

type JsonRestorer struct {
	*PlaceholderSubstituter
	mapping   map[string]string
	maxBuffer int
	chunks    [][]byte
	size      int
	fallback  *ByteRestorer
	MaxHeld   int
	Errors    int
}

func NewJsonRestorer(mapping map[string]string, maxBuffer int) *JsonRestorer {
	if maxBuffer <= 0 {
		maxBuffer = DefaultMaxBufferBytes
	}
	return &JsonRestorer{
		PlaceholderSubstituter: NewPlaceholderSubstituter(mapping),
		mapping:                mapping,
		maxBuffer:              maxBuffer,
	}
}

func (jr *JsonRestorer) Held() int {
	if jr.fallback != nil {
		return jr.fallback.Held()
	}
	return jr.size
}

func (jr *JsonRestorer) Feed(chunk []byte) []byte {
	if jr.fallback != nil {
		out := jr.fallback.Feed(chunk)
		jr.Restored = jr.fallback.Restored
		if jr.fallback.Held() > jr.MaxHeld {
			jr.MaxHeld = jr.fallback.Held()
		}
		return out
	}

	if len(chunk) > 0 {
		jr.chunks = append(jr.chunks, chunk)
		jr.size += len(chunk)
		if jr.size > jr.MaxHeld {
			jr.MaxHeld = jr.size
		}
	}

	if jr.size <= jr.maxBuffer {
		return nil
	}

	jr.fallback = NewByteRestorer(jr.mapping)
	all := jr.take()
	out := jr.fallback.Feed(all)
	jr.Restored = jr.fallback.Restored
	if jr.fallback.Held() > jr.MaxHeld {
		jr.MaxHeld = jr.fallback.Held()
	}
	return out
}

func (jr *JsonRestorer) Flush() []byte {
	if jr.fallback != nil {
		out := jr.fallback.Flush()
		jr.Restored = jr.fallback.Restored
		return out
	}

	data := jr.take()
	if len(data) == 0 {
		return nil
	}

	before := jr.Restored
	restored, err := jr.restore(data)
	if err != nil {
		jr.Restored = before
		jr.Errors++
		byteLevel := NewByteRestorer(jr.mapping)
		out := append(byteLevel.Feed(data), byteLevel.Flush()...)
		jr.Restored += byteLevel.Restored
		return out
	}
	return restored
}

func (jr *JsonRestorer) Abandon(chunk []byte) []byte {
	if jr.fallback != nil {
		return jr.fallback.Abandon(chunk)
	}
	return append(jr.take(), chunk...)
}

func (jr *JsonRestorer) RestoredCount() int {
	return jr.Restored
}

func (jr *JsonRestorer) take() []byte {
	if len(jr.chunks) == 0 {
		return nil
	}
	data := bytes.Join(jr.chunks, nil)
	jr.chunks = nil
	jr.size = 0
	return data
}

func (jr *JsonRestorer) restore(data []byte) ([]byte, error) {
	lower := bytes.ToLower(data)
	if !bytes.Contains(lower, []byte("<")) && !bytes.Contains(lower, []byte(`\u003c`)) {
		return data, nil
	}

	var root any
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	if err := d.Decode(&root); err != nil {
		return nil, err
	}

	switch v := root.(type) {
	case string:
		newVal := jr.Replace(v, "text")
		if newVal == v {
			return data, nil
		}
		return jsonDumps(newVal)
	case map[string]any:
		updated, changed := jr.ReplaceTree(v, false)
		if changed {
			return jsonDumps(updated)
		}
		return data, nil
	case []any:
		updated, changed := jr.ReplaceTree(v, false)
		if changed {
			return jsonDumps(updated)
		}
		return data, nil
	default:
		return data, nil
	}
}

func jsonDumps(obj any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(obj); err != nil {
		return nil, fmt.Errorf("marshal json: %w", err)
	}
	res := buf.Bytes()
	if len(res) > 0 && res[len(res)-1] == '\n' {
		res = res[:len(res)-1]
	}
	return res, nil
}
