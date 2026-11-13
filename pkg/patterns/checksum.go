package patterns

import (
	"regexp"
	"strings"
)

// Checksums are the validators a rule's `checksum` key names; same as the
// Python side (src/standin/patterns.py).
var Checksums = map[string]func(string) bool{"luhn": Luhn, "iban": IBAN}

var ibanShape = regexp.MustCompile(`^[A-Z]{2}[0-9]{2}[A-Z0-9]+$`)

// Luhn checks the digits of s (spaces and dashes ignored).
func Luhn(s string) bool {
	s = strings.NewReplacer(" ", "", "-", "").Replace(s)
	if s == "" {
		return false
	}
	total := 0
	for i := 0; i < len(s); i++ {
		c := s[len(s)-1-i]
		if c < '0' || c > '9' {
			return false
		}
		n := int(c - '0')
		if i%2 == 1 {
			n *= 2
			if n > 9 {
				n -= 9
			}
		}
		total += n
	}
	return total%10 == 0
}

// IBAN checks ISO 13616 mod-97 (spaces ignored; per-country lengths are not checked).
func IBAN(s string) bool {
	s = strings.ReplaceAll(s, " ", "")
	if len(s) < 15 || len(s) > 34 || !ibanShape.MatchString(s) {
		return false
	}
	rem := 0
	for _, c := range s[4:] + s[:4] {
		if c >= 'A' {
			rem = (rem*100 + int(c-'A') + 10) % 97
		} else {
			rem = (rem*10 + int(c-'0')) % 97
		}
	}
	return rem == 1
}
