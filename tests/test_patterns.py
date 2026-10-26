"""patterns.yaml sanity: every rule compiles, is exercised by a positive fixture,
and the tier sizes match the gateway audit lists the file was taken from."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from standin_fixtures import POSITIVE

from standin.patterns import PATTERNS_ENV, Rule, default_patterns_path, load_raw, load_rules

RULES = load_rules()


def test_every_pattern_compiles() -> None:
    for tier, entries in load_raw().items():
        for index, entry in enumerate(entries):
            re.compile(entry["pattern"])
            assert (tier, index) in {(r.tier, r.index) for r in RULES}


@pytest.mark.parametrize("rule", RULES, ids=lambda r: f"{r.tier}{r.index}")
def test_every_pattern_has_a_positive_fixture(rule: Rule) -> None:
    assert (rule.tier, rule.index) in POSITIVE
    text, _, _ = POSITIVE[(rule.tier, rule.index)]
    assert rule.regex.search(text)


def test_tier_sizes() -> None:
    assert sum(r.tier == "a" for r in RULES) == 29
    assert sum(r.tier == "b" for r in RULES) == 5


def test_required_literals_are_lowercase_for_case_insensitive_rules() -> None:
    for rule in RULES:
        if rule.ignore_case:
            assert all(lit == lit.lower() for lit in rule.required), rule.index


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "custom.yaml"
    custom.write_text("tier_a:\n  - pattern: 'FAKE_[0-9]{4}'\n")
    monkeypatch.setenv(PATTERNS_ENV, str(custom))
    assert default_patterns_path() == custom
    assert [r.pattern for r in load_rules()] == ["FAKE_[0-9]{4}"]
