package patterns

import (
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const (
	MaxPrefilterLiterals = 16
	MinPrefilterLen      = 3
)

var repeatRegex = regexp.MustCompile(`^\{([0-9]*)(?:,[0-9]*)?\}`)

func DeriveRequired(pattern string) []string {
	ignoreCase := strings.HasPrefix(pattern, "(?i)")
	body := pattern
	if ignoreCase {
		body = pattern[4:]
	}

	prefixes, end, _ := parseAlternation(body, 0)
	if prefixes == nil || end != len(body) {
		return nil
	}

	if ignoreCase {
		lowerSet := make(map[string]bool)
		for p := range prefixes {
			lowerSet[strings.ToLower(p)] = true
		}
		prefixes = lowerSet
	}

	var list []string
	for p := range prefixes {
		list = append(list, p)
	}
	sort.Strings(list)

	var minimal []string
	for _, p := range list {
		isExt := false
		for _, o := range list {
			if o != p && strings.HasPrefix(p, o) {
				isExt = true
				break
			}
		}
		if !isExt {
			minimal = append(minimal, p)
		}
	}

	common := commonPrefix(minimal)
	if len(minimal) > 1 && len(common) >= MinPrefilterLen {
		minimal = []string{common}
	}

	if len(minimal) == 0 {
		return nil
	}
	for _, p := range minimal {
		if len(p) < MinPrefilterLen {
			return nil
		}
	}

	return minimal
}

func commonPrefix(strs []string) string {
	if len(strs) == 0 {
		return ""
	}
	prefix := strs[0]
	for _, s := range strs[1:] {
		for !strings.HasPrefix(s, prefix) {
			prefix = prefix[:len(prefix)-1]
			if prefix == "" {
				return ""
			}
		}
	}
	return prefix
}

func crossProduct(left, right map[string]bool) (map[string]bool, bool) {
	out := make(map[string]bool)
	for a := range left {
		for b := range right {
			out[a+b] = true
			if len(out) > MaxPrefilterLiterals {
				return nil, false
			}
		}
	}
	return out, true
}

func parseAlternation(p string, i int) (map[string]bool, int, bool) {
	out := make(map[string]bool)
	complete := true
	for {
		prefixes, nextI, done := parseSequence(p, i)
		if prefixes == nil {
			return nil, 0, false
		}
		for k := range prefixes {
			out[k] = true
		}
		if len(out) > MaxPrefilterLiterals {
			return nil, 0, false
		}
		i = nextI
		if !done {
			complete = false
			i = skipBranch(p, i)
		}
		if i < len(p) && p[i] == '|' {
			i++
			continue
		}
		return out, i, complete
	}
}

func parseSequence(p string, i int) (map[string]bool, int, bool) {
	prefixes := map[string]bool{"": true}
	for {
		if i >= len(p) || p[i] == '|' || p[i] == ')' {
			return prefixes, i, true
		}
		chars, nextI, complete, ok := parseAtom(p, i)
		if !ok {
			return prefixes, i, false
		}
		quant := ""
		if nextI < len(p) {
			quant = string(p[nextI])
		}
		optional := false
		if quant == "{" {
			m := repeatRegex.FindStringSubmatch(p[nextI:])
			if m == nil {
				return prefixes, i, false
			}
			val := 0
			if m[1] != "" {
				val, _ = strconv.Atoi(m[1])
			}
			optional = (val == 0)
		} else {
			optional = (quant == "?" || quant == "*")
		}

		right := make(map[string]bool)
		for c := range chars {
			right[c] = true
		}
		if optional {
			right[""] = true
		}

		extended, ok := crossProduct(prefixes, right)
		if !ok {
			return prefixes, i, false
		}
		if !complete || quant == "*" || quant == "+" || quant == "{" {
			return extended, nextI, false
		}
		prefixes = extended
		i = nextI
		if quant == "?" {
			if i+1 < len(p) && p[i+1] == '?' {
				i += 2
			} else {
				i += 1
			}
		}
	}
}

func parseAtom(p string, i int) (map[string]bool, int, bool, bool) {
	c := p[i]
	if c == '(' {
		var k int
		if strings.HasPrefix(p[i:], "(?:") {
			k = i + 3
		} else if strings.HasPrefix(p[i:], "(?P<") {
			idx := strings.Index(p[i:], ">")
			if idx == -1 {
				return nil, 0, false, false
			}
			k = i + idx + 1
		} else if strings.HasPrefix(p[i:], "(?") {
			return nil, 0, false, false
		} else {
			k = i + 1
		}
		inner, nextK, complete := parseAlternation(p, k)
		if inner == nil || nextK >= len(p) || p[nextK] != ')' {
			return nil, 0, false, false
		}
		return inner, nextK + 1, complete, true
	}
	if c == '[' {
		chars, nextI, ok := parseCharClass(p, i)
		if !ok {
			return nil, 0, false, false
		}
		return chars, nextI, true, true
	}
	if c == '\\' {
		esc, nextI, ok := parseEscape(p, i)
		if !ok {
			return nil, 0, false, false
		}
		return map[string]bool{esc: true}, nextI, true, true
	}
	if strings.ContainsRune(".^$*+?{}[]|)", rune(c)) {
		return nil, 0, false, false
	}
	return map[string]bool{string(c): true}, i + 1, true, true
}

func parseEscape(p string, i int) (string, int, bool) {
	if i+1 >= len(p) {
		return "", 0, false
	}
	nxt := p[i+1]
	if nxt == 'x' {
		if i+3 >= len(p) {
			return "", 0, false
		}
		hexStr := p[i+2 : i+4]
		val, err := strconv.ParseInt(hexStr, 16, 32)
		if err != nil {
			return "", 0, false
		}
		return string(rune(val)), i + 4, true
	}
	if (nxt >= 'a' && nxt <= 'z') || (nxt >= 'A' && nxt <= 'Z') || (nxt >= '0' && nxt <= '9') || nxt == '_' || nxt == ' ' || nxt == '\t' || nxt == '\n' || nxt == '\r' {
		return "", 0, false
	}
	return string(nxt), i + 2, true
}

func parseCharClass(p string, i int) (map[string]bool, int, bool) {
	j := i + 1
	if j < len(p) && p[j] == '^' {
		return nil, 0, false
	}
	chars := make(map[string]bool)
	first := true
	for j < len(p) {
		c := p[j]
		if c == ']' && !first {
			return chars, j + 1, true
		}
		first = false
		if c == '[' {
			return nil, 0, false
		}
		var lo string
		if c == '\\' {
			esc, nextJ, ok := parseEscape(p, j)
			if !ok {
				return nil, 0, false
			}
			lo = esc
			j = nextJ
		} else {
			lo = string(c)
			j++
		}

		if j+1 < len(p) && p[j] == '-' && p[j+1] != ']' {
			var hi string
			if p[j+1] == '\\' {
				esc, nextJ, ok := parseEscape(p, j+1)
				if !ok {
					return nil, 0, false
				}
				hi = esc
				j = nextJ
			} else if p[j+1] == '[' {
				return nil, 0, false
			} else {
				hi = string(p[j+1])
				j += 2
			}
			loRune := []rune(lo)[0]
			hiRune := []rune(hi)[0]
			diff := int(hiRune - loRune)
			if diff < 0 || diff >= MaxPrefilterLiterals {
				return nil, 0, false
			}
			for r := loRune; r <= hiRune; r++ {
				chars[string(r)] = true
			}
		} else {
			chars[lo] = true
		}

		if len(chars) > MaxPrefilterLiterals {
			return nil, 0, false
		}
	}
	return nil, 0, false
}

func skipBranch(p string, i int) int {
	depth := 0
	for i < len(p) {
		c := p[i]
		if c == '\\' {
			i += 2
			continue
		}
		if c == '[' {
			i = skipClass(p, i)
			continue
		}
		if c == '(' {
			depth++
		} else if c == ')' {
			if depth == 0 {
				return i
			}
			depth--
		} else if c == '|' && depth == 0 {
			return i
		}
		i++
	}
	return i
}

func skipClass(p string, i int) int {
	j := i + 1
	if j < len(p) && p[j] == '^' {
		j++
	}
	if j < len(p) && p[j] == ']' {
		j++
	}
	for j < len(p) && p[j] != ']' {
		if p[j] == '\\' {
			j += 2
		} else {
			j += 1
		}
	}
	return j + 1
}
