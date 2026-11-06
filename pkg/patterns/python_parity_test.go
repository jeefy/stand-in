package patterns

import (
	"encoding/json"
	"os/exec"
	"reflect"
	"testing"
)

type pyRule struct {
	Tier     string   `json:"tier"`
	Index    int      `json:"index"`
	Required []string `json:"required"`
	Lead     int      `json:"lead"`
	Handling string   `json:"handling"`
}

func TestMatchPythonPatternsExact(t *testing.T) {
	cmd := exec.Command("/usr/bin/python3", "-c", `
import json, sys
from pathlib import Path
sys.path.insert(0, "../../src")
from standin.patterns import load_count_rules
rules = load_count_rules(Path("../../patterns.yaml"))
out = []
for r in rules:
    out.append({
        "tier": r.tier,
        "index": r.index,
        "required": list(r.required),
        "lead": r.lead,
        "handling": r.handling,
    })
print(json.dumps(out))
`)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("run python failed: %v, output: %s", err, string(out))
	}

	var pyRules []pyRule
	if err := json.Unmarshal(out, &pyRules); err != nil {
		t.Fatalf("unmarshal python rules: %v", err)
	}

	goRules, err := LoadCountRules("patterns.yaml")
	if err != nil {
		t.Fatalf("LoadCountRules failed: %v", err)
	}

	if len(pyRules) != len(goRules) {
		t.Fatalf("rule count mismatch: py %d != go %d", len(pyRules), len(goRules))
	}

	for i := range goRules {
		g := goRules[i]
		p := pyRules[i]

		if g.Tier != p.Tier || g.Index != p.Index {
			t.Errorf("[%d] tier/index mismatch: go=(%s, %d) vs py=(%s, %d)", i, g.Tier, g.Index, p.Tier, p.Index)
		}
		if string(g.Handling) != p.Handling {
			t.Errorf("[%d] handling mismatch: go=%s vs py=%s", i, g.Handling, p.Handling)
		}
		if g.Lead != p.Lead {
			t.Errorf("[%d] lead mismatch: go=%d vs py=%d", i, g.Lead, p.Lead)
		}
		// Compare required
		if len(g.Required) == 0 && len(p.Required) == 0 {
			continue
		}
		if !reflect.DeepEqual(g.Required, p.Required) {
			t.Errorf("[%d] required mismatch for rule (%s, %d):\ngo=%v\npy=%v", i, g.Tier, g.Index, g.Required, p.Required)
		}
	}
}
