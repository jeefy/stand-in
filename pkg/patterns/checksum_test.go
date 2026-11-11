package patterns

import "testing"

func TestChecksums(t *testing.T) {
	cases := []struct {
		fn   func(string) bool
		in   string
		want bool
	}{
		{Luhn, "4111 1111 1111 1111", true},
		{Luhn, "5500-0000-0000-0004", true},
		{Luhn, "4111 1111 1111 1112", false},
		{Luhn, "", false},
		{IBAN, "DE89 3704 0044 0532 0130 00", true},
		{IBAN, "GB82WEST12345698765432", true},
		{IBAN, "DE88 3704 0044 0532 0130 00", false},
		{IBAN, "de89370400440532013000", false},
	}
	for _, c := range cases {
		if got := c.fn(c.in); got != c.want {
			t.Errorf("%q = %v; want %v", c.in, got, c.want)
		}
	}
}
