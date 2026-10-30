"""patterns.yaml sanity: every rule compiles and has fixtures, the derived
prefilters find exactly what plain ``finditer`` finds, and the loader rejects
malformed files."""

from __future__ import annotations

import random
import re
import string
from pathlib import Path

import pytest
from standin_fixtures import (
    COUNT_ONLY,
    COUNT_ONLY_EXTRA,
    EXTRA_NEGATIVE,
    EXTRA_POSITIVE,
    POSITIVE,
    fill,
)

import standin.patterns as patterns_mod
from standin.patterns import (
    COUNT_TIERS,
    PATTERNS_ENV,
    TIERS,
    Rule,
    default_patterns_path,
    derive_required,
    load_count_rules,
    load_raw,
    load_rules,
)
from standin.tokenizer import Tokenizer, _Folded, _rule_matches

RULES = load_rules()
COUNT_RULES = load_count_rules()
COUNT_ONLY_RULES = [r for r in COUNT_RULES if r.tier not in TIERS]


def test_every_pattern_compiles() -> None:
    for tier, entries in load_raw().items():
        for index, entry in enumerate(entries):
            re.compile(entry["pattern"])
            assert (tier, index) in {(r.tier, r.index) for r in COUNT_RULES}


@pytest.mark.parametrize("rule", RULES, ids=lambda r: f"{r.tier}{r.index}")
def test_every_pattern_has_a_positive_fixture(rule: Rule) -> None:
    assert (rule.tier, rule.index) in POSITIVE
    text, _, _ = POSITIVE[(rule.tier, rule.index)]
    assert rule.regex.search(text)


def test_tier_sizes_follow_the_shipped_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PATTERNS_ENV, raising=False)
    raw = load_raw(default_patterns_path())
    assert tuple(raw) == COUNT_TIERS
    shipped = {(tier, i) for tier, entries in raw.items() for i in range(len(entries))}
    tokenized = {k for k in shipped if k[0] in TIERS}
    assert {(r.tier, r.index) for r in RULES} == tokenized
    assert {(r.tier, r.index) for r in COUNT_RULES} == shipped
    assert set(POSITIVE) == tokenized
    assert set(COUNT_ONLY) == shipped - tokenized
    assert all(len(raw[t]) for t in raw)
    assert (len(raw["c"]), len(raw["d"])) == (7, 1)


def test_count_rules_start_with_the_tokenized_rules() -> None:
    head = COUNT_RULES[: len(RULES)]
    assert [(r.tier, r.index, r.pattern) for r in head] == [
        (r.tier, r.index, r.pattern) for r in RULES
    ]


@pytest.mark.parametrize("rule", COUNT_ONLY_RULES, ids=lambda r: f"{r.tier}{r.index}")
def test_count_only_fixtures(rule: Rule) -> None:
    pos, neg = COUNT_ONLY[(rule.tier, rule.index)]
    for text in (pos, *(t for k, t in COUNT_ONLY_EXTRA if k == (rule.tier, rule.index))):
        assert len(list(_rule_matches(rule, text, _Folded(text)))) == 1, text
    assert not list(_rule_matches(rule, neg, _Folded(neg)))


@pytest.mark.parametrize("key", ["handling", "reject", "key_boundary"])
def test_count_only_rules_reject_tokenizer_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    value = {"handling": "'data_block_line'", "reject": "['x']", "key_boundary": "true"}[key]
    path = _use(
        tmp_path,
        monkeypatch,
        f"tier_a:\n  - pattern: 'a{{3}}'\ntier_c:\n  - pattern: 'b{{3}}'\n    {key}: {value}\n",
    )
    with pytest.raises(ValueError, match="tokenizer-only"):
        load_count_rules(path)
    assert [r.tier for r in load_rules(path)] == ["a"]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("abc", ("abc",)),
        ("ya29\\.[a-z]+", ("ya29.",)),
        ("\\x24argon2id\\x24v=[0-9]+", ("$argon2id$v=",)),
        ("(?:sk|rk)_live_x", ("rk_live_x", "sk_live_x")),
        ("gh[pousr]_x", ("gho_x", "ghp_x", "ghr_x", "ghs_x", "ghu_x")),
        ("cf[ua]t_", ("cfat_", "cfut_")),
        ("abc|def", ("abc", "def")),
        ("BW_?(?:CLIENT)?SECRET", ("BWCLIENTSECRET", "BWSECRET", "BW_CLIENTSECRET", "BW_SECRET")),
        ("(?P<v>abc)d", ("abcd",)),
        ("abc+", ("abc",)),
        ("tskey-(?:auth|api)-x", ("tskey-a",)),
        ("tskey-(?:auth|client)-x", ("tskey-",)),
        ("(?i)ABC[0-9]", ("abc",)),
        ("(?i)(?:Ab|aB)c", ("abc",)),
        ("(?:abc[0-9]+|defg)h", tuple(f"abc{d}" for d in range(10)) + ("defg",)),
        ("(?:abc[0-9A-Z]+|defg)h", ("abc", "defg")),
        ("abc(?:[0-9]|[a-z])x", ("abc",)),
        ("ab", ()),
        ("abc*", ()),
        ("a.c", ()),
        ("[0-9]{8,10}:AA", ()),
        ("(?m)^abc", ()),
        ("\\babc", ()),
        ("(?i:abc)", ()),
        ("x(?=abc)", ()),
        ("[^a]bcd", ()),
        ("(?:a|b|c|d|e|f|g|h|i|j|k|l|m|n|o|p|q)xyz", ()),
        ("[a-q]bcd", ()),
    ],
)
def test_derive_required(pattern: str, expected: tuple[str, ...]) -> None:
    re.compile(pattern)
    assert derive_required(pattern) == expected


def test_shipped_rules_are_prefiltered() -> None:
    unfiltered = [(r.tier, r.index) for r in RULES if not r.required]
    # Only the base64-line rule, which never goes through _rule_matches.
    assert unfiltered == [("b", 1)]
    by_key = {(r.tier, r.index): r for r in RULES}
    assert (by_key[("a", 18)].required, by_key[("a", 18)].lead) == ((":AA",), 10)
    assert by_key[("a", 24)].required == (
        "rk_live_",
        "rk_prod_",
        "rk_test_",
        "sk_live_",
        "sk_prod_",
        "sk_test_",
    )
    assert set(by_key[("b", 2)].required) == {
        "client-key-data",
        "client-certificate-data",
        "certificate-authority-data",
        "token",
    }


def _corpus(seed: int) -> str:
    rng = random.Random(seed)
    samples = [s for text, _, neg in POSITIVE.values() for s in (text, neg)]
    samples += [text for _, text, _ in EXTRA_POSITIVE] + [text for _, text in EXTRA_NEGATIVE]
    samples += [s.swapcase() for s in samples] + [s.upper() for s in samples]
    pieces = sorted({lit for r in RULES for lit in r.required}) + ["0f" * 8, fill(12), "\n", " "]
    alphabet = string.ascii_letters + string.digits + "_-.:=/+\"' \t\n{},$"

    def filler(n: int) -> str:
        return "".join(rng.choice(alphabet) for _ in range(n))

    parts: list[str] = []
    for s in samples:
        parts += [filler(rng.randint(0, 12)), s]
    parts.append("".join(samples))
    parts.append("".join(rng.sample(samples, len(samples))))
    for a, b in zip(samples, samples[1:], strict=False):
        k = len(a) // 2
        parts.append(filler(3) + a[:k] + b + a[k:] + "\n")
    for _ in range(2000):
        parts.append(rng.choice(pieces) if rng.random() < 0.6 else filler(rng.randint(1, 40)))
    return "".join(parts)


CORPUS = _corpus(1) + "\n\x00\n" + _corpus(2)


@pytest.mark.parametrize("rule", RULES, ids=lambda r: f"{r.tier}{r.index}")
def test_prefilter_matches_plain_finditer(rule: Rule) -> None:
    expected = [m.span() for m in rule.regex.finditer(CORPUS)]
    got = [m.span() for m in _rule_matches(rule, CORPUS, _Folded(CORPUS))]
    assert got == expected
    if rule.handling != "data_block_line":
        assert expected, "corpus never exercises this rule"


COUNT_CORPUS = (
    CORPUS
    + "\n"
    + "\n".join(t for pair in COUNT_ONLY.values() for t in pair)
    + "\n".join(t for _, t in COUNT_ONLY_EXTRA)
)


@pytest.mark.parametrize("rule", COUNT_ONLY_RULES, ids=lambda r: f"{r.tier}{r.index}")
def test_count_only_prefilter_matches_plain_finditer(rule: Rule) -> None:
    expected = [m.span() for m in rule.regex.finditer(COUNT_CORPUS)]
    got = [m.span() for m in _rule_matches(rule, COUNT_CORPUS, _Folded(COUNT_CORPUS))]
    assert got == expected and expected


@pytest.mark.parametrize(
    "blob",
    [
        string.ascii_letters * 20000,
        "a" * 1_000_000,
        "w_" * 500_000,
        "apikey" + "a" * 1_000_000,
        "secret=" + "A" * 1_000_000,
    ],
    ids=["alnum", "a", "w_", "apikey", "secret"],
)
def test_count_only_rules_stay_linear_on_large_blobs(blob: str) -> None:
    import time

    start = time.perf_counter()
    for rule in COUNT_ONLY_RULES:
        list(_rule_matches(rule, blob, _Folded(blob)))
    assert time.perf_counter() - start < 1.5


def _use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> Path:
    custom = tmp_path / "custom.yaml"
    custom.write_text(content)
    monkeypatch.setenv(PATTERNS_ENV, str(custom))
    return custom


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = _use(tmp_path, monkeypatch, "tier_a:\n  - pattern: 'FAKE_[0-9]{4}'\n")
    assert default_patterns_path() == custom
    assert [r.pattern for r in load_rules()] == ["FAKE_[0-9]{4}"]


def test_case_insensitive_prefilter_matches_mixed_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(
        tmp_path,
        monkeypatch,
        "tier_a:\n"
        "  - pattern: '(?i)FakeKey_[0-9]{4}'\n"
        "  - pattern: '(?i)[xy][0-9]Zq[0-9]'\n"
        "    required: ['ZQ']\n"
        "    lead: 2\n",
    )
    rules = load_rules()
    assert rules[0].required == ("fakekey_",)
    assert rules[1].required == ("zq",) and rules[1].lead == 2
    tok = Tokenizer(rules=rules, salt="s", cache_entries=0)
    for text, key in (("FAKEKEY_1234", ("a", 0)), ("fAkEkEy_1234", ("a", 0)), ("X1zQ9", ("a", 1))):
        result = tok.tokenize_text(f"before {text} after")
        assert result.hits == [key] and text not in result.text, text


def test_explicit_required_overrides_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(
        tmp_path,
        monkeypatch,
        "tier_a:\n"
        "  - pattern: 'abcdef[0-9]+'\n"
        "    required: ['def']\n"
        "    lead: 3\n"
        "  - pattern: 'abcdef[0-9]+'\n"
        "    required: []\n"
        "  - pattern: 'abcdef[0-9]+'\n",
    )
    rules = load_rules()
    assert (rules[0].required, rules[0].lead) == (("def",), 3)
    assert rules[1].required == ()
    assert (rules[2].required, rules[2].lead) == (("abcdef",), 0)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("- just\n- a list\n", "must be a mapping"),
        ("tier_a: []\ntier_b: []\n", "no rules"),
        ("other: 1\n", "no rules"),
        ("tier_a:\n  - pattern: 'x'\n    handling: sideways\n", "unknown handling"),
        ("tier_a:\n  - handling: value_only\n", "string `pattern`"),
        ("tier_a:\n  - pattern: 'x'\n    required: 'x'\n", "required"),
        ("tier_a:\n  - pattern: 'x'\n    required: ['']\n", "non-empty"),
        ("tier_a:\n  - pattern: 'x'\n    lead: -1\n", "lead"),
        ("tier_a:\n  - pattern: 'x'\n    reject: '[0-9]+'\n", "reject"),
        ("tier_a:\n  - pattern: 'x'\n    key_boundary: 'yes'\n", "key_boundary"),
    ],
)
def test_malformed_files_raise_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, message: str
) -> None:
    _use(tmp_path, monkeypatch, content)
    with pytest.raises(ValueError, match=message):
        load_rules()


def test_load_raw_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("42\n")
    with pytest.raises(ValueError, match="mapping"):
        load_raw(path)


def test_default_patterns_path_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PATTERNS_ENV, raising=False)
    module = tmp_path / "site" / "pkg" / "standin" / "patterns.py"
    module.parent.mkdir(parents=True)
    module.write_text("")
    monkeypatch.setattr(patterns_mod, "__file__", str(module))
    with pytest.raises(FileNotFoundError, match=PATTERNS_ENV):
        default_patterns_path()
    (module.parent / "patterns.yaml").write_text("tier_a: []\n")
    assert default_patterns_path() == module.parent / "patterns.yaml"
