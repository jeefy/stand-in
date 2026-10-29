from __future__ import annotations

import json
from collections.abc import AsyncIterator

import grpc
import pytest
from standin_fixtures import PEM_KEY, TSKEY, fill

from standin._proto.envoy.config.core.v3 import base_pb2 as core
from standin._proto.envoy.service.ext_proc.v3 import external_processor_pb2 as pb
from standin._proto.envoy.service.ext_proc.v3 import (
    external_processor_pb2_grpc as pb_grpc,
)
from standin.json_restorer import JsonRestorer
from standin.server import ExternalProcessor
from standin.tokenizer import MAX_HOLD, Restorer, Tokenizer, json_escaped

TOK = Tokenizer(salt="json-restorer-tests", cache_entries=0)
NASTY = '-----BEGIN TEST KEY-----\nline "one" \\ two\t\n' + fill(40) + "\n-----END TEST KEY-----"
PH_NASTY = TOK.placeholder(NASTY)
PH_TS = TOK.placeholder(TSKEY)
MAPPING = {PH_NASTY: NASTY, PH_TS: TSKEY}
UNKNOWN = "<<secret-000000000000>>"


def _run(body: bytes, chunks: int = 1, **kwargs: int) -> tuple[bytes, JsonRestorer]:
    r = JsonRestorer(MAPPING, **kwargs)
    step = max(1, -(-len(body) // chunks))
    out = b"".join(r.feed(body[i : i + step]) for i in range(0, len(body), step))
    return out + r.flush(), r


def _chat_completion(arguments: str, content: str | None = None) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": arguments},
                        }
                    ],
                },
            }
        ],
    }


def _arguments_of(out: bytes) -> dict:
    msg = json.loads(out)["choices"][0]["message"]
    return json.loads(msg["tool_calls"][0]["function"]["arguments"])


def test_tool_call_arguments_are_escaped_twice() -> None:
    args = json.dumps({"path": "/tmp/k", "content": PH_NASTY, "note": f"key={PH_TS}"})
    body = json.dumps(_chat_completion(args, f"Writing {PH_TS} now.")).encode()
    out, r = _run(body)
    assert _arguments_of(out) == {"path": "/tmp/k", "content": NASTY, "note": f"key={TSKEY}"}
    assert json.loads(out)["choices"][0]["message"]["content"] == f"Writing {TSKEY} now."
    assert r.restored == 3 and r.errors == 0 and r.held == 0


def test_byte_level_restorer_gets_tool_call_arguments_wrong() -> None:
    body = json.dumps(_chat_completion(json.dumps({"content": PH_NASTY}))).encode()
    byte_level = Restorer(MAPPING)
    out = byte_level.feed(body) + byte_level.flush()
    with pytest.raises(ValueError):
        _arguments_of(out)
    fixed, _ = _run(body)
    assert _arguments_of(fixed) == {"content": NASTY}


@pytest.mark.parametrize("chunks", [2, 3, 7, 50, 10_000])
def test_chunked_feed_across_placeholder_boundaries(chunks: int) -> None:
    args = json.dumps({"content": PH_NASTY})
    body = json.dumps(_chat_completion(args, f"{PH_TS}{PH_TS}")).encode()
    r = JsonRestorer(MAPPING)
    step = max(1, len(body) // chunks)
    pieces = [body[i : i + step] for i in range(0, len(body), step)]
    out = b""
    for n, piece in enumerate(pieces, 1):
        out += r.feed(piece)
        assert out == b"" and r.held == sum(map(len, pieces[:n]))
    out += r.flush()
    assert _arguments_of(out) == {"content": NASTY}
    assert json.loads(out)["choices"][0]["message"]["content"] == TSKEY * 2
    assert r.restored == 3 and r.max_held == len(body) and r.held == 0


def test_anthropic_tool_use_input_is_plain_text() -> None:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "text", "text": f"Using {PH_TS}"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "run",
                "input": {
                    "command": f"tailscale up --authkey={PH_TS}",
                    "files": [{"body": PH_NASTY}],
                    "arguments": PH_NASTY,
                },
            },
        ],
        "stop_reason": "tool_use",
    }
    out, r = _run(json.dumps(message).encode(), chunks=5)
    text, tool = json.loads(out)["content"]
    assert text["text"] == f"Using {TSKEY}"
    assert tool["input"] == {
        "command": f"tailscale up --authkey={TSKEY}",
        "files": [{"body": NASTY}],
        "arguments": NASTY,
    }
    assert r.restored == 4 and r.errors == 0


def test_responses_api_function_call_arguments() -> None:
    response = {
        "object": "response",
        "output": [
            {"type": "function_call", "name": "f", "arguments": json.dumps({"k": PH_NASTY})},
            {"type": "custom_tool_call", "name": "g", "input": f"raw {PH_NASTY}"},
        ],
    }
    out, _ = _run(json.dumps(response).encode())
    call, custom = json.loads(out)["output"]
    assert json.loads(call["arguments"]) == {"k": NASTY}
    assert custom["input"] == f"raw {NASTY}"


def test_unicode_escaped_placeholder_and_non_ascii_text() -> None:
    body = json.dumps({"choices": [{"message": {"content": f"héllo {PH_TS} ✓"}}]}).encode()
    assert b"\\u00e9" in body and PH_TS.encode() in body
    escaped = body.replace(b"<", b"\\u003c")
    out, r = _run(escaped)
    assert json.loads(out)["choices"][0]["message"]["content"] == f"héllo {TSKEY} ✓"
    assert "héllo".encode() in out and r.restored == 1


def test_unknown_placeholder_passes_through_byte_identical() -> None:
    body = b'{ "choices" : [ {"message": {"content": "x ' + UNKNOWN.encode() + b'"}} ] }\n'
    out, r = _run(body, chunks=4)
    assert out == body and r.restored == 0 and r.errors == 0


@pytest.mark.parametrize(
    "body",
    [
        b'{\n  "z": 1,\n  "a": "<b>not a placeholder</b>",\n  "f": 1.50\n}\n',
        b'  [1, 2.0e3, "\\u00e9", {"b": null, "a": true}]  ',
        b'{"id": "x", "choices": []}',
        b"[]",
    ],
)
def test_nothing_to_restore_is_byte_identical(body: bytes) -> None:
    out, r = _run(body, chunks=3)
    assert out == body and r.restored == 0 and r.errors == 0


def test_top_level_string() -> None:
    out, r = _run(json.dumps(f"k {PH_NASTY}").encode())
    assert json.loads(out) == f"k {NASTY}" and r.restored == 1


def test_non_json_body_falls_back_to_byte_restorer() -> None:
    body = f'not json: "{PH_NASTY}" and {UNKNOWN} and <<secret-'.encode()
    out, r = _run(body, chunks=6)
    expected = f'not json: "{json_escaped(NASTY)}" and {UNKNOWN} and <<secret-'.encode()
    assert out == expected
    assert r.errors == 1 and r.restored == 1


def test_empty_body() -> None:
    r = JsonRestorer(MAPPING)
    assert r.feed(b"") == b"" and r.flush() == b""
    assert r.errors == 0 and r.restored == 0


def test_abandon_returns_buffered_bytes_and_chunk_unmodified() -> None:
    r = JsonRestorer(MAPPING)
    assert r.feed(f'{{"a": "{PH_TS}'.encode()) == b""
    assert r.abandon(b'"}') == f'{{"a": "{PH_TS}"}}'.encode()
    assert r.held == 0 and r.restored == 0


def test_over_buffer_cap_degrades_to_byte_level() -> None:
    body = json.dumps({"pad": "x" * 200, "content": f"{PH_TS} {PH_NASTY}"}).encode()
    r = JsonRestorer(MAPPING, max_buffer=64)
    pieces = [body[i : i + 50] for i in range(0, len(body), 50)]
    first = r.feed(pieces[0])
    assert first == b"" and r.held == 50
    out = first + b"".join(r.feed(p) for p in pieces[1:])
    assert r.held <= MAX_HOLD
    out += r.flush()
    assert json.loads(out)["content"] == f"{TSKEY} {NASTY}"
    assert r.restored == 2 and r.errors == 0


def test_over_cap_abandon_releases_held_tail() -> None:
    r = JsonRestorer(MAPPING, max_buffer=4)
    out = r.feed(b'"abc <<sec')
    assert out == b'"abc ' and r.abandon(b"ret") == b"<<secret"


# End to end through ExternalProcessor.Process (helpers mirror tests/test_server.py).


def _headers(**kv: str) -> core.HeaderMap:
    return core.HeaderMap(
        headers=[
            core.HeaderValue(key=k.replace("_", "-"), raw_value=v.encode()) for k, v in kv.items()
        ]
    )


def _exchange(
    body: bytes, response_chunks: list[bytes], content_type: str
) -> list[pb.ProcessingRequest]:
    reqs = [
        pb.ProcessingRequest(
            request_headers=pb.HttpHeaders(headers=_headers(content_type="application/json"))
        ),
        pb.ProcessingRequest(request_body=pb.HttpBody(body=body, end_of_stream=True)),
        pb.ProcessingRequest(
            response_headers=pb.HttpHeaders(headers=_headers(content_type=content_type))
        ),
    ]
    for i, chunk in enumerate(response_chunks):
        last = i == len(response_chunks) - 1
        reqs.append(pb.ProcessingRequest(response_body=pb.HttpBody(body=chunk, end_of_stream=last)))
    return reqs


async def _drive(
    processor: ExternalProcessor, reqs: list[pb.ProcessingRequest]
) -> tuple[list[pb.ProcessingResponse], grpc.StatusCode]:
    server = grpc.aio.server()
    pb_grpc.add_ExternalProcessorServicer_to_server(processor, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb_grpc.ExternalProcessorStub(channel)

            async def gen() -> AsyncIterator[pb.ProcessingRequest]:
                for r in reqs:
                    yield r

            call = stub.Process(gen())
            responses = [r async for r in call]
            return responses, await call.code()
    finally:
        await server.stop(None)


def _response_leg(responses: list[pb.ProcessingResponse]) -> list[pb.StreamedBodyResponse]:
    return [
        r.response_body.response.body_mutation.streamed_response
        for r in responses
        if r.WhichOneof("response") == "response_body"
    ]


def _request_with_pem() -> tuple[bytes, str, str]:
    body = json.dumps(
        {
            "model": "m",
            "messages": [
                {"role": "user", "content": "store the key"},
                {"role": "tool", "tool_call_id": "t1", "content": f"{PEM_KEY}\n"},
            ],
        }
    ).encode()
    result = TOK.tokenize_json(body)
    assert result is not None
    ((ph, value),) = [(p, v) for p, v in result.mapping.items() if "\n" in v]
    return body, ph, value


@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8"])
async def test_end_to_end_json_tool_call_arguments(content_type: str) -> None:
    body, ph, value = _request_with_pem()
    reply = json.dumps(
        _chat_completion(json.dumps({"path": "id_test", "content": ph}), f"saved {ph}")
    ).encode()
    chunks = [reply[i : i + 17] for i in range(0, len(reply), 17)]
    responses, code = await _drive(
        ExternalProcessor(TOK, mode="enforce", chunk_bytes=64),
        _exchange(body, chunks, content_type),
    )
    assert code == grpc.StatusCode.OK
    leg = _response_leg(responses)
    assert [b.end_of_stream for b in leg] == [False] * (len(leg) - 1) + [True]
    assert all(b.body == b"" for b in leg[: len(chunks) - 1])
    assert all(len(b.body) <= 64 for b in leg)
    out = b"".join(b.body for b in leg)
    assert _arguments_of(out) == {"path": "id_test", "content": value}
    assert json.loads(out)["choices"][0]["message"]["content"] == f"saved {value}"


async def test_end_to_end_text_plain_keeps_byte_restorer() -> None:
    body, ph, value = _request_with_pem()
    chunks = [f"a {ph}".encode(), b" b"]
    responses, _ = await _drive(
        ExternalProcessor(TOK, mode="enforce"), _exchange(body, chunks, "text/plain")
    )
    leg = _response_leg(responses)
    assert leg[0].body == f"a {json_escaped(value)}".encode()
    assert b"".join(b.body for b in leg) == f"a {json_escaped(value)} b".encode()
