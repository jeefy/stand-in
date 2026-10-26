from __future__ import annotations

import itertools
import json
import random

from standin_fixtures import TSKEY, fill

from standin.sse import SseRestorer
from standin.tokenizer import MAX_HOLD, PLACEHOLDER_LEN, Restorer, Tokenizer

TOK = Tokenizer(salt="restorer-tests")
PH = TOK.placeholder(TSKEY)
OTHER_VALUE = "ghp_" + fill(24)
PH2 = TOK.placeholder(OTHER_VALUE)
MAPPING = {PH: TSKEY, PH2: OTHER_VALUE}
UNKNOWN = "<<secret-000000000000>>"


def _run(chunks: list[bytes], mapping: dict[str, str] = MAPPING) -> tuple[bytes, Restorer]:
    r = Restorer(mapping)
    out = b"".join(r.feed(c) for c in chunks) + r.flush()
    return out, r


def test_constants() -> None:
    assert PLACEHOLDER_LEN == 23 and MAX_HOLD == 22


def test_split_at_every_offset_two_chunks() -> None:
    stream = f'data: {{"c":"run {PH} now"}}\n\n'.encode()
    expected = stream.replace(PH.encode(), TSKEY.encode())
    for i in range(len(stream) + 1):
        out, r = _run([stream[:i], stream[i:]])
        assert out == expected, i
        assert r.restored == 1 and r.max_held <= MAX_HOLD


def test_split_at_every_offset_three_chunks() -> None:
    stream = f"x{PH}y".encode()
    expected = f"x{TSKEY}y".encode()
    for i, j in itertools.combinations_with_replacement(range(len(stream) + 1), 2):
        out, _ = _run([stream[:i], stream[i:j], stream[j:]])
        assert out == expected, (i, j)


def test_placeholder_at_very_end() -> None:
    out, r = _run([b"tail ", PH.encode()])
    assert out == f"tail {TSKEY}".encode() and r.restored == 1
    r = Restorer(MAPPING)
    assert r.feed(f"tail {PH}".encode()) == f"tail {TSKEY}".encode()
    assert r.flush() == b""


def test_partial_placeholder_at_end_is_flushed_unchanged() -> None:
    r = Restorer(MAPPING)
    partial = PH[:-1].encode()
    assert r.feed(b"abc " + partial) == b"abc "
    assert r.held == len(partial)
    assert r.flush() == partial


def test_unknown_placeholder_passes_through() -> None:
    stream = f"a {UNKNOWN} b {PH}".encode()
    for i in range(len(stream) + 1):
        out, r = _run([stream[:i], stream[i:]])
        assert out == f"a {UNKNOWN} b {TSKEY}".encode()
        assert r.restored == 1


def test_interleaved_placeholders() -> None:
    stream = f"{PH}{PH2}<{PH}<<{UNKNOWN}{PH2}>".encode()
    expected = f"{TSKEY}{OTHER_VALUE}<{TSKEY}<<{UNKNOWN}{OTHER_VALUE}>".encode()
    for i, j in itertools.combinations(range(0, len(stream) + 1, 3), 2):
        out, r = _run([stream[:i], stream[i:j], stream[j:]])
        assert out == expected and r.restored == 4


def test_random_megabyte_is_byte_identical_and_hold_is_bounded() -> None:
    rng = random.Random(1234)
    data = rng.randbytes(1 << 20)
    alphabet = b"<<<secret-0123456789abcdef>>x"
    adversarial = bytes(rng.choice(alphabet) for _ in range(1 << 16))
    for stream in (data, adversarial + data):
        assert b"<<secret-" not in stream or all(ph.encode() not in stream for ph in MAPPING)
        r = Restorer(MAPPING)
        out = []
        pos = 0
        while pos < len(stream):
            step = rng.randint(1, 4096)
            out.append(r.feed(stream[pos : pos + step]))
            assert r.held <= MAX_HOLD
            pos += step
        out.append(r.flush())
        assert b"".join(out) == stream
        assert r.max_held <= 23 and r.restored == 0


def test_abandon_returns_held_and_chunk() -> None:
    r = Restorer(MAPPING)
    assert r.feed(b"ab<<sec") == b"ab"
    assert r.abandon(b"ret-x") == b"<<secret-x"


def _chat_event(delta: dict, finish: str | None = None) -> bytes:
    obj = {"id": "c1", "object": "chat.completion.chunk", "model": "m"}
    obj["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _split(text: str, sizes: list[int]) -> list[str]:
    out, pos = [], 0
    for n in sizes:
        out.append(text[pos : pos + n])
        pos += n
    out.append(text[pos:])
    return [p for p in out if p]


def _collect_chat(stream: bytes) -> tuple[str, str]:
    content, args = "", ""
    for line in stream.split(b"\n"):
        if not line.startswith(b"data: ") or line == b"data: [DONE]":
            continue
        delta = json.loads(line[6:])["choices"][0]["delta"]
        content += delta.get("content") or ""
        for call in delta.get("tool_calls") or []:
            args += call["function"].get("arguments", "")
    return content, args


def _chat_stream() -> tuple[bytes, str, str]:
    text = f"Run this: {PH} then {PH2}."
    command = json.dumps({"command": f"tailscale up --authkey {PH}"})
    events = [_chat_event({"role": "assistant", "content": ""})]
    events += [_chat_event({"content": p}) for p in _split(text, [7, 5, 4, 3, 9, 2, 6, 8])]
    first = {"index": 0, "id": "call_1", "type": "function"}
    events.append(
        _chat_event({"tool_calls": [{**first, "function": {"name": "bash", "arguments": ""}}]})
    )
    for part in _split(command, [10, 20, 5, 6, 7, 3]):
        events.append(_chat_event({"tool_calls": [{"index": 0, "function": {"arguments": part}}]}))
    events.append(b": keep-alive\n\n")
    events.append(_chat_event({}, finish="tool_calls"))
    events.append(b"data: [DONE]\n\n")
    want_args = json.dumps({"command": f"tailscale up --authkey {TSKEY}"})
    return b"".join(events), text.replace(PH, TSKEY).replace(PH2, OTHER_VALUE), want_args


def test_sse_placeholder_split_across_delta_events() -> None:
    stream, want_text, want_args = _chat_stream()
    r = SseRestorer(MAPPING)
    out = r.feed(stream) + r.flush()
    content, args = _collect_chat(out)
    assert content == want_text
    assert args == want_args and json.loads(args)["command"].endswith(TSKEY)
    assert r.restored == 3 and r.errors == 0
    assert out.endswith(b"data: [DONE]\n\n")


def test_sse_any_byte_split_of_the_stream() -> None:
    stream, want_text, want_args = _chat_stream()
    rng = random.Random(7)
    for _ in range(200):
        cuts = sorted(rng.sample(range(1, len(stream)), 3))
        parts = [stream[a:b] for a, b in zip([0, *cuts], [*cuts, len(stream)], strict=True)]
        r = SseRestorer(MAPPING)
        out = b"".join(r.feed(p) for p in parts) + r.flush()
        assert _collect_chat(out) == (want_text, want_args)


def test_sse_held_prefix_is_released_before_finish() -> None:
    stream = _chat_event({"content": "end <<secr"}) + _chat_event({}, finish="stop")
    r = SseRestorer(MAPPING)
    out = r.feed(stream) + r.flush()
    assert _collect_chat(out)[0] == "end <<secr"
    assert out.index(b"<<secr") < out.index(b'"finish_reason": "stop"')


def test_sse_held_prefix_is_released_at_end_of_stream() -> None:
    r = SseRestorer(MAPPING)
    out = r.feed(_chat_event({"content": "x <<secret-0a"})) + r.flush()
    assert _collect_chat(out)[0] == "x <<secret-0a"


def test_sse_anthropic_text_and_input_json() -> None:
    def ev(name: str, obj: dict) -> bytes:
        return f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode()

    text = f"key is {PH}"
    args = json.dumps({"cmd": f"echo {PH2}"})
    events = [ev("message_start", {"type": "message_start", "message": {"id": "m"}})]
    for p in _split(text, [5, 6, 4, 4]):
        delta = {"type": "text_delta", "text": p}
        events.append(
            ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
        )
        events.append(ev("ping", {"type": "ping"}))
    events.append(ev("content_block_stop", {"type": "content_block_stop", "index": 0}))
    for p in _split(args, [9, 9, 4]):
        delta = {"type": "input_json_delta", "partial_json": p}
        events.append(
            ev("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": delta})
        )
    events.append(ev("content_block_stop", {"type": "content_block_stop", "index": 1}))
    events.append(ev("message_stop", {"type": "message_stop"}))
    r = SseRestorer(MAPPING)
    out = r.feed(b"".join(events)) + r.flush()
    got_text, got_args = "", ""
    for block in out.split(b"\n\n"):
        for line in block.split(b"\n"):
            if line.startswith(b"data:"):
                obj = json.loads(line[5:])
                if obj.get("type") == "content_block_delta":
                    got_text += obj["delta"].get("text", "")
                    got_args += obj["delta"].get("partial_json", "")
    assert got_text == f"key is {TSKEY}"
    assert json.loads(got_args) == {"cmd": f"echo {OTHER_VALUE}"}
    assert r.restored == 2


def test_sse_json_escaping_in_arguments() -> None:
    value = 'q"uo\\te'
    ph = TOK.placeholder(value)
    args = json.dumps({"v": ph})
    stream = b"".join(
        _chat_event({"tool_calls": [{"index": 0, "function": {"arguments": p}}]})
        for p in _split(args, [8, 7, 6])
    )
    r = SseRestorer({ph: value})
    out = r.feed(stream) + r.flush()
    assert json.loads(_collect_chat(out)[1]) == {"v": value}


def test_sse_unknown_and_non_json_events() -> None:
    stream = _chat_event({"content": f"{UNKNOWN}"}) + f"data: not json {PH}\n\n".encode()
    r = SseRestorer(MAPPING)
    out = r.feed(stream) + r.flush()
    assert UNKNOWN.encode() in out
    assert f"data: not json {TSKEY}\n\n".encode() in out


def test_sse_events_without_placeholders_are_untouched() -> None:
    stream = _chat_event({"content": "plain <b>html</b>"}) + b"event: x\ndata: {}\n\n"
    r = SseRestorer(MAPPING)
    assert r.feed(stream) + r.flush() == stream
