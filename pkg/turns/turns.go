package turns

import (
	"strings"
)

type Scope string

const (
	ScopeTail      Scope = "tail"
	ScopeFirst     Scope = "first"
	ScopeInputOnly Scope = "input_only"
	ScopeNone      Scope = "none"
)

var skipKeys = map[string]bool{
	"image_url": true,
	"file_data": true,
}

func NewTurnStrings(doc any) (Scope, []string) {
	docMap, ok := doc.(map[string]any)
	if !ok {
		return ScopeFirst, ExtractStrings(doc)
	}

	if messages, ok := docMap["messages"].([]any); ok {
		return tailOrFirst(doc, messages, isAssistantMessage)
	}

	if items := docMap["input"]; items != nil {
		if prevID, ok := docMap["previous_response_id"].(string); ok && prevID != "" {
			return ScopeInputOnly, ExtractStrings(items)
		}
		if itemList, ok := items.([]any); ok {
			return tailOrFirst(doc, itemList, isResponsesBoundary)
		}
	}

	return ScopeFirst, ExtractStrings(doc)
}

func ExtractStrings(value any) []string {
	var out []string
	var stack []any
	stack = append(stack, value)

	for len(stack) > 0 {
		n := len(stack) - 1
		node := stack[n]
		stack = stack[:n]

		switch v := node.(type) {
		case string:
			out = append(out, v)
		case map[string]any:
			base64Source := v["type"] == "base64"
			var children []any
			for k, val := range v {
				if skipKeys[k] {
					continue
				}
				if base64Source && k == "data" {
					continue
				}
				children = append(children, val)
			}
			for i := len(children) - 1; i >= 0; i-- {
				stack = append(stack, children[i])
			}
		case []any:
			for i := len(v) - 1; i >= 0; i-- {
				stack = append(stack, v[i])
			}
		}
	}

	return out
}

func tailOrFirst(doc any, items []any, isBoundary func(any) bool) (Scope, []string) {
	boundary := lastBoundary(items, isBoundary)
	if boundary == -1 {
		return ScopeFirst, ExtractStrings(doc)
	}
	var tailItems []any
	for i := boundary + 1; i < len(items); i++ {
		tailItems = append(tailItems, items[i])
	}
	return ScopeTail, ExtractStrings(tailItems)
}

func lastBoundary(items []any, isBoundary func(any) bool) int {
	for i := len(items) - 2; i >= 0; i-- {
		if isBoundary(items[i]) {
			return i
		}
	}
	return -1
}

func isAssistantMessage(item any) bool {
	m, ok := item.(map[string]any)
	if !ok {
		return false
	}
	return m["role"] == "assistant"
}

func isResponsesBoundary(item any) bool {
	m, ok := item.(map[string]any)
	if !ok {
		return false
	}
	kind, hasKind := m["type"].(string)
	if !hasKind || kind == "message" {
		return m["role"] == "assistant"
	}
	return kind == "reasoning" || strings.HasSuffix(kind, "_call")
}
