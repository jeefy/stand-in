"""Per-channel coverage for SseRestorer: Responses API, OpenAI chat variants,
Anthropic block types, SSE framing edge cases and failure paths."""

from __future__ import annotations

import json
import random
from typing import Any

import pytest
from standin_fixtures import fill

from standin.sse import SseRestorer
from standin.tokenizer import Tokenizer, json_escaped

TOK = Tokenizer(salt="sse-channel-tests")
V1 = "ghp_" + fill(24)
V2 = 'pa"ss\\word'
PH1 = TOK.placeholder(V1)
PH2 = TOK.placeholder(V2)
MAPPING = {PH1: V1, PH2: V2}


def _sse(obj: Any, event: str | None = None, *, lead: str = " ", nl: str = "\n") -> bytes:
    name = f"event: {event}{nl}" if event else ""
    return f"{name}data:{lead}{json.dumps(obj)}{nl}{nl}".encode()


def _events(out: bytes) -> list[tuple[str | None, Any]]:
    """(event name, parsed data) for every single-data-line JSON event."""
    found: list[tuple[str | None, Any]] = []
    for block in out.replace(b"\r\n", b"\n").split(b"\n\n"):
        name = None
        data = [ln for ln in block.split(b"\n") if ln.startswith(b"data:")]
        for ln in block.split(b"\n"):
            if ln.startswith(b"event:"):
                name = ln[6:].strip().decode()
        if len(data) != 1 or data[0][5:].strip() == b"[DONE]":
            continue
        try:
            found.append((name, json.loads(data[0][5:])))
        except ValueError:
            continue
    return found


def _run(stream: bytes, mapping: dict[str, str] = MAPPING) -> tuple[bytes, SseRestorer]:
    r = SseRestorer(mapping)
    return r.feed(stream) + r.flush(), r


def _random_splits(stream: bytes, seed: int, n: int = 100) -> list[bytes]:
    rng = random.Random(seed)
    outs = []
    for _ in range(n):
        cuts = sorted(rng.sample(range(1, len(stream)), min(4, len(stream) - 1)))
        parts = [stream[a:b] for a, b in zip([0, *cuts], [*cuts, len(stream)], strict=True)]
        r = SseRestorer(MAPPING)
        outs.append(b"".join(r.feed(p) for p in parts) + r.flush())
    return outs


# --- Responses API ---------------------------------------------------------


def _resp_delta(kind: str, item: str, oi: int, delta: str) -> bytes:
    obj = {"type": kind, "item_id": item, "output_index": oi, "content_index": 0, "delta": delta}
    return _sse(obj, kind)


def _resp_text(out: bytes, kind: str, item: str) -> str:
    return "".join(
        o["delta"] for _, o in _events(out) if o.get("type") == kind and o.get("item_id") == item
    )


def _responses_text_stream() -> bytes:
    k = "response.output_text.delta"
    text = f"key {PH1} ok"
    parts = [text[:9], text[9:17], text[17:]]
    assert all(PH1 not in p for p in parts)
    events = [_resp_delta(k, "msg_1", 0, p) for p in parts]
    events.append(
        _sse(
            {
                "type": "response.output_text.done",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "text": text,
            },
            "response.output_text.done",
        )
    )
    events.append(
        _sse({"type": "response.completed", "response": {"id": "r"}}, "response.completed")
    )
    return b"".join(events)


def test_responses_output_text_split_three_deltas() -> None:
    stream = _responses_text_stream()
    out, r = _run(stream)
    assert _resp_text(out, "response.output_text.delta", "msg_1") == f"key {V1} ok"
    done = [o for _, o in _events(out) if o["type"] == "response.output_text.done"]
    assert done[0]["text"] == f"key {V1} ok"
    assert [o["type"] for _, o in _events(out)][-1] == "response.completed"
    assert r.restored == 2 and r.errors == 0 and r.held == 0
    for o in _random_splits(stream, 11):
        assert _resp_text(o, "response.output_text.delta", "msg_1") == f"key {V1} ok"


def test_responses_function_call_arguments_json_escaped_and_done() -> None:
    k = "response.function_call_arguments.delta"
    args = json.dumps({"cmd": f"login {PH2}"})
    i = args.index(PH2)
    parts = [args[: i + 4], args[i + 4 : i + 15], args[i + 15 :]]
    events = [_resp_delta(k, "fc_1", 1, p) for p in parts]
    events.append(
        _sse(
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_1",
                "output_index": 1,
                "arguments": args,
            }
        )
    )
    stream = b"".join(events)
    out, r = _run(stream)
    assert json.loads(_resp_text(out, k, "fc_1")) == {"cmd": f"login {V2}"}
    done = [o for _, o in _events(out) if o["type"].endswith(".done")][0]
    assert json.loads(done["arguments"]) == {"cmd": f"login {V2}"}
    assert r.restored == 2
    for o in _random_splits(stream, 12):
        assert json.loads(_resp_text(o, k, "fc_1")) == {"cmd": f"login {V2}"}


def test_responses_parallel_items_interleaved_with_held_prefixes() -> None:
    k = "response.output_text.delta"
    a, b = f"A {PH1} a", f"B {PH2} b"
    events = [
        _resp_delta(k, "i1", 0, a[:5]),
        _resp_delta(k, "i2", 1, b[:8]),
        _resp_delta(k, "i1", 0, a[5:14]),
        _resp_delta(k, "i2", 1, b[8:20]),
        _resp_delta(k, "i1", 0, a[14:]),
        _resp_delta(k, "i2", 1, b[20:]),
        _sse({"type": "response.completed"}),
    ]
    stream = b"".join(events)
    out, r = _run(stream)
    assert _resp_text(out, k, "i1") == f"A {V1} a"
    assert _resp_text(out, k, "i2") == f"B {V2} b"
    assert r.restored == 2
    for o in _random_splits(stream, 13):
        assert (_resp_text(o, k, "i1"), _resp_text(o, k, "i2")) == (f"A {V1} a", f"B {V2} b")


def test_responses_held_prefix_drained_before_done() -> None:
    k = "response.output_text.delta"
    stream = _resp_delta(k, "i1", 0, "end <<secr") + _sse(
        {"type": "response.output_text.done", "item_id": "i1", "text": "end <<secr"}
    )
    out, _ = _run(stream)
    assert _resp_text(out, k, "i1") == "end <<secr"
    assert out.index(b'"delta":"<<secr"') < out.index(b"output_text.done")


# --- OpenAI chat -----------------------------------------------------------


def _chat(choices: list[dict[str, Any]]) -> bytes:
    return _sse({"id": "c", "object": "chat.completion.chunk", "choices": choices})


def _delta(delta: dict[str, Any], index: int = 0, finish: str | None = None) -> bytes:
    return _chat([{"index": index, "delta": delta, "finish_reason": finish}])


def _call(ti: int | None, args: str) -> dict[str, Any]:
    c: dict[str, Any] = {"function": {"arguments": args}}
    if ti is not None:
        c["index"] = ti
    return {"tool_calls": [c]}


def _chat_collect(out: bytes) -> dict[tuple[Any, ...], str]:
    acc: dict[tuple[Any, ...], str] = {}
    for _, o in _events(out):
        if not isinstance(o, dict):
            continue
        for ch in o.get("choices", []):
            d = ch.get("delta", {})
            for f in ("content", "reasoning_content", "reasoning", "refusal"):
                if isinstance(d.get(f), str):
                    acc[(ch["index"], f)] = acc.get((ch["index"], f), "") + d[f]
            for call in d.get("tool_calls") or []:
                key = (ch["index"], "tool", call.get("index", 0))
                acc[key] = acc.get(key, "") + call["function"].get("arguments", "")
    return acc


def test_chat_two_tool_calls_interleaved() -> None:
    a0 = json.dumps({"x": PH1})
    a1 = json.dumps({"y": PH2})
    events = [
        _delta(_call(0, a0[:10])),
        _delta(_call(1, a1[:12])),
        _delta(_call(0, a0[10:20])),
        _delta(_call(1, a1[12:22])),
        _delta(_call(0, a0[20:])),
        _delta(_call(1, a1[22:])),
        _delta({}, finish="tool_calls"),
    ]
    stream = b"".join(events)
    want = {(0, "tool", 0): {"x": V1}, (0, "tool", 1): {"y": V2}}
    out, r = _run(stream)
    got = _chat_collect(out)
    assert {k: json.loads(v) for k, v in got.items()} == want and r.restored == 2
    for o in _random_splits(stream, 21):
        assert {k: json.loads(v) for k, v in _chat_collect(o).items()} == want


def test_chat_tool_call_without_index_defaults_to_zero() -> None:
    args = json.dumps({"v": PH2})
    stream = (
        _delta(_call(None, args[:9]))
        + _delta(_call(0, args[9:18]))
        + _delta(_call(None, args[18:]))
    )
    out, _ = _run(stream)
    assert json.loads(_chat_collect(out)[(0, "tool", 0)]) == {"v": V2}


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "refusal"])
def test_chat_text_side_channels(field: str) -> None:
    text = f"see {PH2}!"
    stream = b"".join(_delta({field: p}) for p in (text[:7], text[7:16], text[16:]))
    stream += _delta({}, finish="stop")
    out, r = _run(stream)
    assert _chat_collect(out)[(0, field)] == f"see {V2}!" and r.restored == 1
    for o in _random_splits(stream, 22):
        assert _chat_collect(o)[(0, field)] == f"see {V2}!"


def test_chat_n2_choices_stream_simultaneously() -> None:
    t0, t1 = f"zero {PH1}", f"one {PH2}"
    events = []
    for a, b in ((0, 8), (8, 17), (17, 40)):
        events.append(_delta({"content": t0[a:b]}, index=0))
        events.append(_delta({"content": t1[a:b]}, index=1))
    events.append(
        _chat(
            [
                {"index": 0, "delta": {}, "finish_reason": "stop"},
                {"index": 1, "delta": {}, "finish_reason": "stop"},
            ]
        )
    )
    stream = b"".join(events)
    out, r = _run(stream)
    got = _chat_collect(out)
    assert got[(0, "content")] == f"zero {V1}" and got[(1, "content")] == f"one {V2}"
    assert r.restored == 2
    for o in _random_splits(stream, 23):
        g = _chat_collect(o)
        assert (g[(0, "content")], g[(1, "content")]) == (f"zero {V1}", f"one {V2}")


def test_cross_kind_adjacent_channels_restore_independently() -> None:
    text = f"run {PH2} now"
    args = json.dumps({"k": PH2})
    i = args.index(PH2)
    events = [
        _delta({"content": text[:8]}),
        _delta(_call(0, args[: i + 5])),
        _delta({"content": text[8:]}),
        _delta(_call(0, args[i + 5 :])),
        _delta({}, finish="tool_calls"),
    ]
    stream = b"".join(events)
    out, r = _run(stream)
    got = _chat_collect(out)
    assert got[(0, "content")] == f"run {V2} now"  # raw value in text
    assert json.loads(got[(0, "tool", 0)]) == {"k": V2}  # escaped in JSON
    assert json_escaped(V2) not in got[(0, "content")]
    assert r.restored == 2
    for o in _random_splits(stream, 31):
        g = _chat_collect(o)
        assert g[(0, "content")] == f"run {V2} now" and json.loads(g[(0, "tool", 0)]) == {"k": V2}


# --- Anthropic -------------------------------------------------------------


def _ant(index: int, dtype: str, field: str, value: str) -> bytes:
    obj = {"type": "content_block_delta", "index": index, "delta": {"type": dtype, field: value}}
    return _sse(obj, "content_block_delta")


def _ant_collect(out: bytes, field: str, index: int = 0) -> str:
    return "".join(
        o["delta"].get(field, "")
        for _, o in _events(out)
        if o.get("type") == "content_block_delta" and o.get("index") == index
    )


def test_anthropic_thinking_split() -> None:
    t = f"hmm {PH1} yes"
    stream = b"".join(_ant(0, "thinking_delta", "thinking", p) for p in (t[:7], t[7:20], t[20:]))
    stream += _sse({"type": "content_block_stop", "index": 0}, "content_block_stop")
    out, r = _run(stream)
    assert _ant_collect(out, "thinking") == f"hmm {V1} yes" and r.restored == 1


@pytest.mark.parametrize(
    "terminal",
    [
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
        {"type": "error", "error": {"type": "overloaded_error"}},
    ],
)
def test_anthropic_terminal_events_drain_with_event_line(terminal: dict[str, Any]) -> None:
    stream = _ant(0, "text_delta", "text", "tail <<secret-0") + _sse(terminal, terminal["type"])
    r = SseRestorer(MAPPING)
    out = r.feed(stream)
    assert r.held == 0
    synthetic = b'event: content_block_delta\ndata: {"type":"content_block_delta"'
    assert synthetic in out
    assert out.index(synthetic) < out.index(f"event: {terminal['type']}".encode())
    assert _ant_collect(out + r.flush(), "text") == "tail <<secret-0"


def test_anthropic_two_blocks_interleaved() -> None:
    text = f"key {PH1}."
    args = json.dumps({"cmd": PH2})
    events = [
        _ant(0, "text_delta", "text", text[:8]),
        _ant(1, "input_json_delta", "partial_json", args[:12]),
        _ant(0, "text_delta", "text", text[8:]),
        _ant(1, "input_json_delta", "partial_json", args[12:]),
        _sse({"type": "message_stop"}, "message_stop"),
    ]
    stream = b"".join(events)
    out, r = _run(stream)
    assert _ant_collect(out, "text", 0) == f"key {V1}."
    assert json.loads(_ant_collect(out, "partial_json", 1)) == {"cmd": V2}
    assert r.restored == 2
    for o in _random_splits(stream, 41):
        assert _ant_collect(o, "text", 0) == f"key {V1}."
        assert json.loads(_ant_collect(o, "partial_json", 1)) == {"cmd": V2}


# --- Framing ---------------------------------------------------------------


def test_crlf_framing_end_to_end() -> None:
    text = f"a {PH1} b"
    stream = b"".join(
        _sse({"choices": [{"index": 0, "delta": {"content": p}}]}, nl="\r\n")
        for p in (text[:6], text[6:15], text[15:])
    )
    stream += b"data: [DONE]\r\n\r\n"
    out, r = _run(stream)
    assert _chat_collect(out)[(0, "content")] == f"a {V1} b" and r.restored == 1
    assert out.endswith(b"data: [DONE]\r\n\r\n")
    assert b"\r\n\r\n" in out
    for o in _random_splits(stream, 51):
        assert _chat_collect(o)[(0, "content")] == f"a {V1} b"


def test_two_data_lines_fall_back_to_bytes_and_keep_held_state() -> None:
    held = _delta({"content": f"x {PH1[:10]}"})
    multi = f"data: one {PH2}\ndata: two\n\n".encode()
    rest = _delta({"content": PH1[10:] + " y"})
    out, r = _run(held + multi + rest)
    assert f"data: one {json_escaped(V2)}\ndata: two\n\n".encode() in out
    assert _chat_collect(out)[(0, "content")] == f"x {V1} y"
    assert r.restored == 2 and r.errors == 0


def test_data_without_space_keeps_lead() -> None:
    text = f"q {PH1}"
    stream = b"".join(
        _sse({"choices": [{"index": 0, "delta": {"content": p}}]}, lead="")
        for p in (text[:5], text[5:])
    ) + _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, lead="")
    out, _ = _run(stream)
    assert b"data: " not in out
    assert _chat_collect(out)[(0, "content")] == f"q {V1}"


def test_non_dict_payloads() -> None:
    held = _delta({"content": "w <<sec"})
    stream = held + f'data: [1,"{PH1}"]\n\n'.encode() + b"data: 5\n\n" + _delta({"content": "x"})
    out, r = _run(stream)
    assert f'data: [1,"{V1}"]\n\n'.encode() in out and b"data: 5\n\n" in out
    assert _chat_collect(out)[(0, "content")] == "w <<secx"
    assert r.errors == 0


def test_unicode_escaped_placeholder_in_content_is_restored() -> None:
    escaped = PH1.replace("<", "\\u003c")
    stream = (
        b'data: {"choices":[{"index":0,"delta":{"content":"k ' + escaped.encode() + b'"}}]}\n\n'
    )
    out, r = _run(stream)
    assert _chat_collect(out)[(0, "content")] == f"k {V1}" and r.restored == 1


def test_comment_lines_between_events_pass_through() -> None:
    text = f"c {PH1}"
    stream = (
        _delta({"content": text[:7]})
        + b": keep-alive\n\n"
        + _delta({"content": text[7:]})
        + b": ping\n\n"
    )
    out, _ = _run(stream)
    assert out.count(b": keep-alive\n\n") == 1 and out.endswith(b": ping\n\n")
    assert _chat_collect(out)[(0, "content")] == f"c {V1}"


def test_lt_without_placeholder_is_byte_identical() -> None:
    stream = b'event: x\ndata: {"choices" : [ {"index":0, "delta": {"content": "a<b <<c"}} ]}\n\n'
    out, r = _run(stream + b"data: [DONE]\n\n")
    # "<<c" is not a placeholder prefix, so nothing is held or rewritten
    assert out == stream + b"data: [DONE]\n\n" and r.restored == 0


# --- Failure paths ---------------------------------------------------------


def test_feed_event_exception_passes_raw_event(monkeypatch: pytest.MonkeyPatch) -> None:
    original = SseRestorer._event
    bad = _delta({"content": f"bad {PH1}"})

    def flaky(self: SseRestorer, event: bytes) -> bytes:
        if event == bad:
            raise RuntimeError("synthetic")
        return original(self, event)

    monkeypatch.setattr(SseRestorer, "_event", flaky)
    stream = bad + _delta({"content": f"good {PH2}"})
    out, r = _run(stream)
    assert r.errors == 1
    assert out.startswith(bad)
    assert _chat_collect(out[len(bad) :])[(0, "content")] == f"good {V2}"


def test_abandon_when_drain_raises() -> None:
    r = SseRestorer(MAPPING)
    assert _chat_collect(r.feed(_delta({"content": "h <<secr"})))[(0, "content")] == "h "
    assert r.feed(b"data: {partial") == b""
    for held in r._held.values():
        held.path = ("nope", "deeper")
    assert r.abandon(b"chunk") == b"data: {partialchunk"
    assert not r._held and r.held == 0


def test_dumps_ascii_fallback_lone_surrogate() -> None:
    value = "\udcff" + V1
    ph = TOK.placeholder(value)
    stream = _delta({"content": f"s {ph[:8]}"}) + _delta({"content": ph[8:]})
    out, r = _run(stream, {ph: value})
    out.decode("utf-8")
    assert r.restored == 1
    assert _chat_collect(out)[(0, "content")] == f"s {value}"
