from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from standin_fixtures import (
    B64_LINE,
    EXTRA_NEGATIVE,
    EXTRA_POSITIVE,
    NPM_UUID,
    PEM_BODY,
    PEM_KEY,
    POSITIVE,
    TSKEY,
    fill,
)

from standin.patterns import load_rules
from standin.tokenizer import PLACEHOLDER_RE, Restorer, Tokenizer, _is_data_key, _value_span

RULES = load_rules()


@pytest.fixture
def tok() -> Tokenizer:
    return Tokenizer(salt="test-salt", cache_entries=0)


def test_every_rule_has_a_fixture() -> None:
    assert set(POSITIVE) == {(r.tier, r.index) for r in RULES}


@pytest.mark.parametrize("key", sorted(POSITIVE), ids=lambda k: f"{k[0]}{k[1]}")
def test_rule_positive(tok: Tokenizer, key: tuple[str, int]) -> None:
    text, secret, _ = POSITIVE[key]
    result = tok.tokenize_text(text)
    assert key in result.hits
    assert secret not in result.text
    assert PLACEHOLDER_RE.search(result.text)


@pytest.mark.parametrize("key", sorted(POSITIVE), ids=lambda k: f"{k[0]}{k[1]}")
def test_rule_negative(tok: Tokenizer, key: tuple[str, int]) -> None:
    _, _, negative = POSITIVE[key]
    result = tok.tokenize_text(f"before {negative} after")
    assert key not in result.hits
    assert result.text == f"before {negative} after"


@pytest.mark.parametrize("key", sorted(POSITIVE), ids=lambda k: f"{k[0]}{k[1]}")
def test_rule_positive_inside_json(tok: Tokenizer, key: tuple[str, int]) -> None:
    text, secret, _ = POSITIVE[key]
    body = json.dumps({"messages": [{"role": "tool", "content": text}]}).encode()
    result = tok.tokenize_json(body)
    assert result is not None and key in result.hits
    decoded = json.loads(result.text)["messages"][0]["content"]
    assert secret not in decoded


def test_placeholder_shape_and_determinism() -> None:
    a, b = Tokenizer(salt="s1"), Tokenizer(salt="s1")
    assert re.fullmatch(r"<<secret-[0-9a-f]{12}>>", a.placeholder(TSKEY))
    assert a.placeholder(TSKEY) == b.placeholder(TSKEY)
    assert a.placeholder(TSKEY) != Tokenizer(salt="s2").placeholder(TSKEY)
    assert a.placeholder(TSKEY) != a.placeholder(TSKEY + "0")


def test_same_value_same_placeholder_within_request(tok: Tokenizer) -> None:
    result = tok.tokenize_text(f"{TSKEY} and again {TSKEY}")
    placeholders = PLACEHOLDER_RE.findall(result.text)
    assert len(placeholders) == 2 and placeholders[0] == placeholders[1]
    assert result.replaced == 2 and result.distinct == 1
    assert result.mapping == {placeholders[0]: TSKEY}


def test_long_salt_is_accepted() -> None:
    assert Tokenizer(salt="x" * 200).placeholder("v").startswith("<<secret-")


def test_pem_whole_key_is_one_placeholder(tok: Tokenizer) -> None:
    result = tok.tokenize_text("cat id_ed25519\n" + PEM_KEY + "\n$ ")
    assert result.text == "cat id_ed25519\n" + tok.placeholder(PEM_KEY) + "\n$ "
    assert result.replaced == 1
    assert ("b", 0) in result.hits and ("b", 1) in result.hits


def test_pem_without_footer_takes_following_base64_lines(tok: Tokenizer) -> None:
    truncated = PEM_KEY.split("\n-----END")[0]
    result = tok.tokenize_text("head -4 key\n" + truncated + "\nnext prompt")
    assert result.text == "head -4 key\n" + tok.placeholder(truncated) + "\nnext prompt"


def test_pem_inside_double_encoded_json(tok: Tokenizer) -> None:
    sa_file = json.dumps({"type": "service_account", "private_key": PEM_KEY + "\n"}, indent=1)
    body = json.dumps({"messages": [{"role": "tool", "content": sa_file}]}).encode()
    result = tok.tokenize_json(body)
    assert result is not None
    content = json.loads(result.text)["messages"][0]["content"]
    assert "BEGIN OPENSSH" not in content and PEM_BODY.split("\n")[0] not in content
    parsed = json.loads(content)
    assert PLACEHOLDER_RE.fullmatch(parsed["private_key"].rstrip("\n"))
    assert PLACEHOLDER_RE.fullmatch(parsed["type"])
    pem_ph = parsed["private_key"].rstrip("\n")
    assert result.mapping[pem_ph] == PEM_KEY.replace("\n", "\\n")


def test_base64_line_rule_needs_data_context(tok: Tokenizer) -> None:
    standalone = tok.tokenize_text("firmware blob:\n" + B64_LINE + "\n")
    assert standalone.replaced == 0
    block = "kind: Secret\ndata:\n  " + B64_LINE + "\n  " + B64_LINE[::-1] + "\nother:\n  x\n"
    scoped = tok.tokenize_text(block)
    assert scoped.replaced == 2
    assert B64_LINE not in scoped.text and B64_LINE[::-1] not in scoped.text
    assert scoped.text.startswith("kind: Secret\ndata:\n  <<secret-")
    for key in ("client-key-data:", "binaryData:", '"tls_data": |', "Data:"):
        assert tok.tokenize_text(f"{key}\n    {B64_LINE}\n").replaced == 1, key
    for key in ("metadata:", "notdata:", "data: inline"):
        assert tok.tokenize_text(f"{key}\n    {B64_LINE}\n").replaced == 0, key


def test_value_only_keeps_the_key(tok: Tokenizer) -> None:
    value = fill(48, "Zm9v")
    result = tok.tokenize_text(f"client-key-data: {value}")
    assert result.text == f"client-key-data: {tok.placeholder(value)}"


def test_overlapping_matches_are_unioned(tok: Tokenizer) -> None:
    text = '{"private_key": "' + PEM_KEY.replace("\n", "\\n") + '"}'
    result = tok.tokenize_text(text)
    assert result.replaced == 1
    assert result.text.startswith('{"private_key": "<<secret-') and result.text.endswith('>>"}')


def test_tokenize_json_rejects_non_json(tok: Tokenizer) -> None:
    assert tok.tokenize_json(b"not json " + TSKEY.encode()) is None
    assert tok.tokenize_json(b"\xff\xfe{}") is None


def test_tokenize_json_untouched_bytes_are_identical(tok: Tokenizer) -> None:
    body = (
        b'{ "model" : "m",\n  "x": "caf\\u00e9 \\/ \\"q\\"",'
        b' "messages": [ {"role":"tool","content":"k=' + TSKEY.encode() + b' ok"} ] }'
    )
    result = tok.tokenize_json(body)
    assert result is not None
    ph = tok.placeholder(TSKEY)
    assert result.text.encode() == body.replace(TSKEY.encode(), ph.encode())


def test_tokenize_json_without_matches_returns_input(tok: Tokenizer) -> None:
    body = b'{"messages": [{"role": "user", "content": "hello\\nworld \\u00e9"}]}'
    result = tok.tokenize_json(body)
    assert result is not None and result.replaced == 0 and result.text.encode() == body


def test_escaped_json_cannot_hide_a_match(tok: Tokenizer) -> None:
    body = json.dumps({"content": 'BW_CLIENTSECRET="' + fill(30) + '"'}).encode()
    assert b'\\"' in body
    result = tok.tokenize_json(body)
    assert result is not None and ("a", 28) in result.hits


def test_cache_gives_identical_results() -> None:
    body = json.dumps(
        {"messages": [{"role": "tool", "content": fill(600, "lorem ipsum ") + " " + TSKEY}] * 3}
    ).encode()
    cold = Tokenizer(salt="c", cache_entries=0).tokenize_json(body)
    cached_tok = Tokenizer(salt="c", cache_entries=10)
    first = cached_tok.tokenize_json(body)
    second = cached_tok.tokenize_json(body)
    assert cold is not None and first is not None and second is not None
    assert cold.text == first.text == second.text
    assert cold.hits == second.hits and cold.mapping == second.mapping
    assert cached_tok.cache_hits >= 3


def test_round_trip_value_with_quote_and_backslash() -> None:
    value = 'p"a\\ss'
    ph = Tokenizer(salt="t").placeholder(value)
    upstream = json.dumps({"content": f"use {ph} now"}).encode()
    restorer = Restorer({ph: value})
    restored = restorer.feed(upstream) + restorer.flush()
    assert json.loads(restored)["content"] == f"use {value} now"
    assert restorer.restored == 1


def test_round_trip_pem_through_json(tok: Tokenizer) -> None:
    body = json.dumps({"messages": [{"role": "tool", "content": PEM_KEY}]}).encode()
    result = tok.tokenize_json(body)
    assert result is not None
    (ph,) = result.mapping
    echoed = json.dumps({"choices": [{"message": {"content": f"key:\n{ph}"}}]}).encode()
    restorer = Restorer(result.mapping)
    out = restorer.feed(echoed) + restorer.flush()
    assert json.loads(out)["choices"][0]["message"]["content"] == "key:\n" + PEM_KEY


@pytest.mark.parametrize("case", EXTRA_POSITIVE, ids=lambda c: f"{c[0][0]}{c[0][1]}")
def test_extra_positive(tok: Tokenizer, case: tuple[tuple[str, int], str, str]) -> None:
    key, text, secret = case
    result = tok.tokenize_text(text)
    assert key in result.hits
    assert secret not in result.text


@pytest.mark.parametrize("case", EXTRA_NEGATIVE, ids=lambda c: f"{c[0][0]}{c[0][1]}")
def test_extra_negative(tok: Tokenizer, case: tuple[tuple[str, int], str]) -> None:
    key, text = case
    result = tok.tokenize_text(f"before {text} after")
    assert key not in result.hits
    assert result.text == f"before {text} after"


def test_value_group_keeps_a_key_with_a_colon(tok: Tokenizer) -> None:
    key = "//registry.npmjs.org/:_authToken="
    result = tok.tokenize_text(f"{key}{NPM_UUID}\n")
    assert result.text == f"{key}{tok.placeholder(NPM_UUID)}\n"


def test_reject_and_key_boundary(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text(
        "tier_a:\n"
        "  - pattern: 'secret=[a-z0-9]{8,}'\n"
        "    handling: value_only\n"
        "    reject: ['0+', 'deadbeef[0-9]*']\n"
        "    key_boundary: true\n"
    )
    tok = Tokenizer(rules=load_rules(path), salt="s", cache_entries=0)
    for text in ("secret=abcdefgh", "my-secret=abcdefgh", '{"x":"secret=abcdefgh'):
        result = tok.tokenize_text(text)
        assert result.text == text.replace("abcdefgh", tok.placeholder("abcdefgh")), text
    for text in ("secret=00000000", "secret=deadbeef42", "mysecret=abcdefgh", "My0secret=abcdefgh"):
        assert tok.tokenize_text(text).replaced == 0, text


def test_cache_evicts_oldest_entries() -> None:
    tok = Tokenizer(salt="c", cache_entries=2)
    literals = [fill(600, f"lorem {i} ") for i in range(3)]
    tok.tokenize_json(json.dumps({"m": literals}).encode())
    assert len(tok._cache) == 2
    tok.tokenize_json(json.dumps({"m": literals[2]}).encode())
    assert tok.cache_hits == 1
    tok.tokenize_json(json.dumps({"m": literals[0]}).encode())
    assert tok.cache_hits == 1 and len(tok._cache) == 2


def test_cached_clean_literal_is_returned_unchanged() -> None:
    tok = Tokenizer(salt="c", cache_entries=10)
    body = json.dumps({"content": fill(700, "lorem ipsum ")}).encode()
    first = tok.tokenize_json(body)
    second = tok.tokenize_json(body)
    assert tok.cache_hits == 1
    for result in (first, second):
        assert result is not None and result.replaced == 0 and result.text.encode() == body


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("abcdef", (0, 6)),
        ("key=", (0, 4)),
        ('key: ""', (0, 7)),
        ("key: 'v' ", (6, 7)),
        ("a:b=c", (2, 5)),
    ],
)
def test_value_span_edges(text: str, expected: tuple[int, int]) -> None:
    assert _value_span(text, 0, len(text)) == expected


def test_lone_surrogate_literal_round_trips(tok: Tokenizer) -> None:
    secret = "ghp_" + fill(24)
    body = b'{"c": "\\udcff ' + secret.encode() + b'"}'
    result = tok.tokenize_json(body)
    assert result is not None and result.replaced == 1
    out = result.text.encode("utf-8")
    assert json.loads(out)["c"] == "\udcff " + tok.placeholder(secret)
    assert result.mapping == {tok.placeholder(secret): secret}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("data", True),
        ("{data", True),
        (",data", True),
        ("x.data", True),
        ("a.b.data", True),
        ("\ttls-data", True),
        ('"tls_data', True),
        ("binaryData", True),
        ("metadata", False),
        ("nodata", False),
        ("a+data", False),
        ("x/data", False),
    ],
)
def test_is_data_key_boundaries(text: str, expected: bool) -> None:
    assert _is_data_key(text, text.lower(), len(text) - 4) is expected


def test_data_keys_end_to_end(tok: Tokenizer) -> None:
    for prefix in ("secret.data:", "a: 1,data:", '{"data":'):
        result = tok.tokenize_text(f"{prefix}\n    {B64_LINE}\n")
        assert result.replaced == 1 and B64_LINE not in result.text, prefix


def test_data_key_at_end_of_input(tok: Tokenizer) -> None:
    assert tok.tokenize_text("data:").replaced == 0
    assert tok.tokenize_text("kind: Secret\ndata:").replaced == 0
    result = tok.tokenize_text("data:\n  " + B64_LINE)
    assert result.text == "data:\n  " + tok.placeholder(B64_LINE)


def test_byte_restorer_json_escapes_even_in_plain_text_bodies() -> None:
    value = 'p"a\\ss\nx'
    ph = Tokenizer(salt="t").placeholder(value)
    restorer = Restorer({ph: value})
    out = restorer.feed(f"plain {ph} text".encode()) + restorer.flush()
    assert out == b'plain p\\"a\\\\ss\\nx text'


def test_partial_hold_point_property() -> None:
    import random

    from standin.tokenizer import partial_hold_point

    ph = Tokenizer(salt="t").placeholder("v")
    rng = random.Random(99)
    alphabet = "ab<>-secrt0123456789abcdef \n"
    for _ in range(500):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))).rstrip("<")
        assert partial_hold_point(s + ph) == len(s) + len(ph)
        for k in range(1, len(ph)):
            assert partial_hold_point(s + ph[:k]) == len(s), (s, k)
