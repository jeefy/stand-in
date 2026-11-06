package restorer

import (
	"bytes"
	"regexp"

	"github.com/jeefy/stand-in/pkg/tokenizer"
)

var (
	PlaceholderRegexBytes = regexp.MustCompile(`<<secret-[0-9a-f]{12}>>`)
	secretMarkerBytes     = []byte("<<secret-")
)

type ByteRestorer struct {
	replacements map[string][]byte
	held         []byte
	Restored     int
	MaxHeld      int
	Errors       int
}

func NewByteRestorer(mapping map[string]string) *ByteRestorer {
	repl := make(map[string][]byte)
	for ph, v := range mapping {
		repl[ph] = []byte(tokenizer.JSONEscaped(v))
	}
	return &ByteRestorer{
		replacements: repl,
	}
}

func (br *ByteRestorer) Held() int {
	return len(br.held)
}

func (br *ByteRestorer) Feed(chunk []byte) []byte {
	var data []byte
	if len(br.held) > 0 {
		data = make([]byte, len(br.held)+len(chunk))
		copy(data, br.held)
		copy(data[len(br.held):], chunk)
	} else {
		data = chunk
	}

	cut := tokenizer.PartialHoldPointBytes(data)
	head := data[:cut]

	if bytes.Contains(head, secretMarkerBytes) {
		head = PlaceholderRegexBytes.ReplaceAllFunc(head, func(m []byte) []byte {
			val, ok := br.replacements[string(m)]
			if !ok {
				return m
			}
			br.Restored++
			return val
		})
	}

	br.held = data[cut:]
	if len(br.held) > br.MaxHeld {
		br.MaxHeld = len(br.held)
	}
	return head
}

func (br *ByteRestorer) Flush() []byte {
	tail := br.held
	br.held = nil
	return tail
}

func (br *ByteRestorer) Abandon(chunk []byte) []byte {
	out := append(br.held, chunk...)
	br.held = nil
	return out
}

func (br *ByteRestorer) RestoredCount() int {
	return br.Restored
}
