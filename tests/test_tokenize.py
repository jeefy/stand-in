from __future__ import annotations

import json
import re

import pytest
from standin_fixtures import B64_LINE, PEM_BODY, PEM_KEY, POSITIVE, TSKEY, fill

from standin.patterns import load_rules
from standin.tokenizer import PLACEHOLDER_RE, Restorer, Tokenizer

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
