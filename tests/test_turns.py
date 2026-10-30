from __future__ import annotations

import json

import pytest
from standin_fixtures import IMAGE_B64, NEW_A, NEW_D, OLD, SYS_C, TSKEY, TURN_CASES

from standin import tokenizer as tokenizer_mod
from standin.patterns import load_count_rules
from standin.tokenizer import CountBudgetExceeded, Tokenizer
from standin.turns import SCOPES, new_turn_strings, strings

TOK = Tokenizer(salt="turn-tests", cache_entries=0, count_rules=load_count_rules())


@pytest.mark.parametrize("name", list(TURN_CASES))
def test_new_turn_counts(name: str) -> None:
    doc, scope, expected = TURN_CASES[name]
    body = json.dumps(doc).encode()
    new = TOK.count_new(body)
    assert new.error is None
    assert (new.scope, new.tier_counts()) == (scope, expected)
    assert scope in SCOPES


@pytest.mark.parametrize("name", list(TURN_CASES))
def test_new_turn_strings_follow_scope(name: str) -> None:
    doc, scope, _ = TURN_CASES[name]
    got_scope, texts = new_turn_strings(doc)
    joined = "\n".join(texts)
    assert got_scope == scope
    assert IMAGE_B64 not in joined
    if scope == "first":
        assert texts == strings(doc)
    else:
        assert OLD not in joined and SYS_C not in joined
        assert NEW_D in joined


def test_strings_document_order_and_skips() -> None:
    doc = {
        "a": "one",
        "b": [{"type": "image_url", "image_url": {"url": "u"}}, "two", 3, None, True],
        "c": {"type": "base64", "data": "skip", "media_type": "image/png"},
        "file_data": "skip",
        "d": {"data": "kept"},
    }
    assert strings(doc) == ["one", "image_url", "two", "base64", "image/png", "kept"]


def test_tail_starts_after_last_boundary() -> None:
    msgs = [
        {"role": "user", "content": OLD},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": NEW_A},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": NEW_D},
    ]
    scope, texts = new_turn_strings({"messages": msgs})
    assert scope == "tail" and texts == ["user", NEW_D]


@pytest.mark.parametrize("doc", [[1, 2], "x", {"messages": "not a list"}, {}])
def test_unrecognized_shapes_scan_everything(doc: object) -> None:
    assert new_turn_strings(doc)[0] == "first"


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe", b""])
def test_non_json_scope_none(body: bytes) -> None:
    new = TOK.count_new(body)
    assert new.scope == "none" and new.hits == [] and new.error is None


def test_count_new_never_changes_tokenization() -> None:
    doc, _, _ = TURN_CASES["anthropic_tool_continuation"]
    body = json.dumps(doc).encode()
    plain = Tokenizer(salt="turn-tests", cache_entries=0)
    TOK.count_new(body)
    assert TOK.tokenize_json(body) == plain.tokenize_json(body)


def test_count_new_reports_pattern_indices() -> None:
    body = json.dumps({"messages": [{"role": "user", "content": TSKEY + " " + NEW_D}]}).encode()
    new = TOK.count_new(body)
    tiers = {tier for tier, _ in new.hits}
    assert tiers == {"a", "d"}
    assert sum(new.tier_counts().values()) == 2


def test_count_new_budget() -> None:
    body = json.dumps({"messages": [{"role": "user", "content": "apikey" * 2000}]}).encode()
    with pytest.raises(CountBudgetExceeded):
        TOK.count_new(body, budget=-1.0)
    assert tokenizer_mod.COUNT_BUDGET_SECONDS == 2.0
