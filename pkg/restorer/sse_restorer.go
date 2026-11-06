package restorer

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/jeefy/stand-in/pkg/tokenizer"
)

var (
	eventEndRegex   = regexp.MustCompile(`\r?\n\r?\n`)
	chatTextFields  = []string{"content", "reasoning_content", "reasoning", "refusal"}
	anthropicFields = []struct {
		name string
		kind string
	}{
		{"text", "text"},
		{"partial_json", "json"},
		{"thinking", "text"},
	}
	terminalTypes = map[string]bool{
		"content_block_stop":  true,
		"message_delta":       true,
		"message_stop":        true,
		"error":               true,
		"response.completed":  true,
		"response.incomplete": true,
		"response.failed":     true,
	}
)

type sseChannel struct {
	key       string
	container map[string]any
	field     string
	kind      string
	template  map[string]any
	path      []any
}

type sseHeld struct {
	text      string
	kind      string
	eventName []byte
	lead      []byte
	template  map[string]any
	path      []any
}

type SseRestorer struct {
	*PlaceholderSubstituter
	buf     []byte
	held    map[string]*sseHeld
	MaxHeld int
	Errors  int
}

func NewSseRestorer(mapping map[string]string) *SseRestorer {
	return &SseRestorer{
		PlaceholderSubstituter: NewPlaceholderSubstituter(mapping),
		held:                   make(map[string]*sseHeld),
	}
}

func (sr *SseRestorer) Held() int {
	total := len(sr.buf)
	for _, h := range sr.held {
		total += len(h.text)
	}
	return total
}

func (sr *SseRestorer) Feed(chunk []byte) []byte {
	data := append(sr.buf, chunk...)
	var out [][]byte
	pos := 0

	for {
		loc := eventEndRegex.FindIndex(data[pos:])
		if loc == nil {
			break
		}
		end := pos + loc[1]
		event := data[pos:end]
		pos = end

		res, err := sr.processEvent(event)
		if err != nil {
			sr.Errors++
			out = append(out, event)
		} else {
			out = append(out, res)
		}
	}

	sr.buf = append([]byte(nil), data[pos:]...)
	held := sr.Held()
	if held > sr.MaxHeld {
		sr.MaxHeld = held
	}
	return bytes.Join(out, nil)
}

func (sr *SseRestorer) Flush() []byte {
	rest := sr.buf
	sr.buf = nil
	var out []byte
	if len(rest) > 0 {
		res, err := sr.processEvent(rest)
		if err != nil {
			sr.Errors++
			out = rest
		} else {
			out = res
		}
	}
	return append(out, sr.drain()...)
}

func (sr *SseRestorer) Abandon(chunk []byte) []byte {
	drained := sr.drain()
	out := append(drained, sr.buf...)
	out = append(out, chunk...)
	sr.buf = nil
	return out
}

func (sr *SseRestorer) RestoredCount() int {
	return sr.Restored
}

func (sr *SseRestorer) processEvent(event []byte) ([]byte, error) {
	lines := bytes.Split(event, []byte("\n"))
	var dataLineIdx []int

	for i, line := range lines {
		trimmed := bytes.TrimPrefix(line, []byte("\r"))
		if bytes.HasPrefix(trimmed, []byte("data:")) {
			dataLineIdx = append(dataLineIdx, i)
		}
	}

	if len(dataLineIdx) != 1 {
		return sr.ReplaceBytes(event), nil
	}

	idx := dataLineIdx[0]
	line := lines[idx]
	lineTrimmed := bytes.TrimRight(line, "\r")
	suffix := line[len(lineTrimmed):]

	payload := lineTrimmed[5:]
	var lead []byte
	if len(payload) > 0 && payload[0] == ' ' {
		lead = []byte(" ")
		payload = payload[1:]
	}

	if bytes.Equal(bytes.TrimSpace(payload), []byte("[DONE]")) {
		return append(sr.drain(), event...), nil
	}

	lower := bytes.ToLower(payload)
	if len(sr.held) == 0 && !bytes.Contains(lower, []byte("<")) && !bytes.Contains(lower, []byte(`\u003c`)) {
		return event, nil
	}

	var obj map[string]any
	d := json.NewDecoder(bytes.NewReader(payload))
	d.UseNumber()
	if err := d.Decode(&obj); err != nil {
		return sr.ReplaceBytes(event), nil
	}

	var eventName []byte
	for _, ln := range lines {
		trimmed := bytes.TrimPrefix(ln, []byte("\r"))
		if bytes.HasPrefix(trimmed, []byte("event:")) {
			eventName = bytes.TrimSpace(trimmed[6:])
			break
		}
	}

	terminal := isTerminal(obj)
	changed := false
	channels := extractChannels(obj)

	for _, ch := range channels {
		origVal, _ := ch.container[ch.field].(string)
		held := sr.held[ch.key]
		delete(sr.held, ch.key)

		heldText := ""
		if held != nil {
			heldText = held.text
		}
		text := heldText + origVal

		cut := len(text)
		if !terminal {
			cut = tokenizer.PartialHoldPoint(text)
		}

		head, tail := text[:cut], text[cut:]
		if len(tail) > 0 {
			sr.held[ch.key] = &sseHeld{
				text:      tail,
				kind:      ch.kind,
				eventName: eventName,
				lead:      lead,
				template:  ch.template,
				path:      ch.path,
			}
		}

		newVal := sr.Replace(head, ch.kind)
		if newVal != origVal {
			ch.container[ch.field] = newVal
			changed = true
		}
	}

	_, treeChanged := sr.ReplaceTree(obj, false)
	changed = changed || treeChanged

	prefix := []byte(nil)
	if terminal {
		prefix = sr.drain()
	}

	if changed {
		dumped, err := jsonDumps(obj)
		if err == nil {
			newLine := append([]byte("data:"), lead...)
			newLine = append(newLine, dumped...)
			newLine = append(newLine, suffix...)
			lines[idx] = newLine
			event = bytes.Join(lines, []byte("\n"))
		}
	}

	return append(prefix, event...), nil
}

func (sr *SseRestorer) drain() []byte {
	var out []byte
	for _, held := range sr.held {
		obj := deepCopyMap(held.template)
		setPath(obj, held.path, sr.Replace(held.text, held.kind))
		dumped, err := jsonDumps(obj)
		if err != nil {
			continue
		}
		if len(held.eventName) > 0 {
			out = append(out, []byte("event: ")...)
			out = append(out, held.eventName...)
			out = append(out, '\n')
		}
		out = append(out, []byte("data:")...)
		out = append(out, held.lead...)
		out = append(out, dumped...)
		out = append(out, []byte("\n\n")...)
	}
	sr.held = make(map[string]*sseHeld)
	return out
}

func deepCopyMap(m map[string]any) map[string]any {
	data, _ := json.Marshal(m)
	var out map[string]any
	_ = json.Unmarshal(data, &out)
	return out
}

func setPath(obj any, path []any, val string) {
	curr := obj
	for i := 0; i < len(path)-1; i++ {
		step := path[i]
		switch k := step.(type) {
		case string:
			if m, ok := curr.(map[string]any); ok {
				curr = m[k]
			}
		case int:
			if arr, ok := curr.([]any); ok && k < len(arr) {
				curr = arr[k]
			}
		}
	}
	last := path[len(path)-1]
	switch k := last.(type) {
	case string:
		if m, ok := curr.(map[string]any); ok {
			m[k] = val
		}
	case int:
		if arr, ok := curr.([]any); ok && k < len(arr) {
			arr[k] = val
		}
	}
}

func isTerminal(obj map[string]any) bool {
	if choices, ok := obj["choices"].([]any); ok {
		for _, c := range choices {
			if cm, ok := c.(map[string]any); ok && cm["finish_reason"] != nil && cm["finish_reason"] != "" {
				return true
			}
		}
	}
	kind, ok := obj["type"].(string)
	if ok {
		if terminalTypes[kind] || strings.HasSuffix(kind, ".done") {
			return true
		}
	}
	return false
}

func extractChannels(obj map[string]any) []sseChannel {
	var out []sseChannel
	if choices, ok := obj["choices"].([]any); ok {
		base := make(map[string]any)
		for k, v := range obj {
			if k != "choices" && k != "usage" {
				base[k] = v
			}
		}

		for _, choice := range choices {
			cm, ok := choice.(map[string]any)
			if !ok {
				continue
			}
			ci := 0
			if idxNum, ok := cm["index"].(json.Number); ok {
				n, _ := idxNum.Int64()
				ci = int(n)
			} else if idxInt, ok := cm["index"].(int); ok {
				ci = idxInt
			}

			delta, ok := cm["delta"].(map[string]any)
			if !ok {
				continue
			}

			for _, name := range chatTextFields {
				if _, ok := delta[name].(string); ok {
					tplChoice := map[string]any{
						"index": ci,
						"delta": map[string]any{name: ""},
					}
					tpl := make(map[string]any)
					for k, v := range base {
						tpl[k] = v
					}
					tpl["choices"] = []any{tplChoice}
					path := []any{"choices", 0, "delta", name}
					key := fmt.Sprintf("chat:%d:%s", ci, name)
					out = append(out, sseChannel{
						key:       key,
						container: delta,
						field:     name,
						kind:      "text",
						template:  tpl,
						path:      path,
					})
				}
			}

			if calls, ok := delta["tool_calls"].([]any); ok {
				for _, call := range calls {
					callMap, ok := call.(map[string]any)
					if !ok {
						continue
					}
					fn, ok := callMap["function"].(map[string]any)
					if !ok {
						continue
					}
					if _, ok := fn["arguments"].(string); ok {
						ti := 0
						if idxNum, ok := callMap["index"].(json.Number); ok {
							n, _ := idxNum.Int64()
							ti = int(n)
						} else if idxInt, ok := callMap["index"].(int); ok {
							ti = idxInt
						}

						tplChoice := map[string]any{
							"index": ci,
							"delta": map[string]any{
								"tool_calls": []any{
									map[string]any{
										"index": ti,
										"function": map[string]any{
											"arguments": "",
										},
									},
								},
							},
						}
						tpl := make(map[string]any)
						for k, v := range base {
							tpl[k] = v
						}
						tpl["choices"] = []any{tplChoice}
						path := []any{"choices", 0, "delta", "tool_calls", 0, "function", "arguments"}
						key := fmt.Sprintf("chat:%d:tool:%d", ci, ti)
						out = append(out, sseChannel{
							key:       key,
							container: fn,
							field:     "arguments",
							kind:      "json",
							template:  tpl,
							path:      path,
						})
					}
				}
			}
		}
		return out
	}

	kind, ok := obj["type"].(string)
	if !ok {
		return out
	}
	deltaObj := obj["delta"]

	if kind == "content_block_delta" {
		if deltaMap, ok := deltaObj.(map[string]any); ok {
			bi := 0
			if idxNum, ok := obj["index"].(json.Number); ok {
				n, _ := idxNum.Int64()
				bi = int(n)
			}
			for _, f := range anthropicFields {
				if _, ok := deltaMap[f.name].(string); ok {
					tpl := map[string]any{
						"type":  "content_block_delta",
						"index": bi,
						"delta": map[string]any{
							"type": deltaMap["type"],
							f.name: "",
						},
					}
					key := fmt.Sprintf("anthropic:%d:%s", bi, f.name)
					path := []any{"delta", f.name}
					out = append(out, sseChannel{
						key:       key,
						container: deltaMap,
						field:     f.name,
						kind:      f.kind,
						template:  tpl,
						path:      path,
					})
				}
			}
		}
	} else if strings.HasSuffix(kind, ".delta") {
		if _, ok := deltaObj.(string); ok {
			key := fmt.Sprintf("responses:%s:%v:%v:%v", kind, obj["item_id"], obj["output_index"], obj["content_index"])
			tpl := make(map[string]any)
			for k, v := range obj {
				tpl[k] = v
			}
			tpl["delta"] = ""
			path := []any{"delta"}
			out = append(out, sseChannel{
				key:       key,
				container: obj,
				field:     "delta",
				kind:      "text",
				template:  tpl,
				path:      path,
			})
		}
	}

	return out
}
