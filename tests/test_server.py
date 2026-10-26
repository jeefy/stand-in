from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from prometheus_client import REGISTRY
from standin_fixtures import TSKEY

from standin import main as main_mod
from standin._proto.envoy.config.core.v3 import base_pb2 as core
from standin._proto.envoy.service.ext_proc.v3 import external_processor_pb2 as pb
from standin._proto.envoy.service.ext_proc.v3 import (
    external_processor_pb2_grpc as pb_grpc,
)
from standin.server import ExternalProcessor
from standin.sse import SseRestorer
from standin.tokenizer import Tokenizer

TOK = Tokenizer(salt="server-tests", cache_entries=0)
PH = TOK.placeholder(TSKEY)


def _headers(**kv: str) -> core.HeaderMap:
    return core.HeaderMap(
        headers=[
            core.HeaderValue(key=k.replace("_", "-"), raw_value=v.encode()) for k, v in kv.items()
        ]
    )


def _request_body() -> bytes:
    return json.dumps(
        {
            "model": "m",
            "stream": True,
            "messages": [
                {"role": "user", "content": "use the key from the tool result"},
                {"role": "tool", "tool_call_id": "t1", "content": f"TS_AUTHKEY={TSKEY}\n"},
            ],
        }
    ).encode()


def _sse_response(text: str) -> list[bytes]:
    events = []
    for i in range(0, len(text), 5):
        obj = {"choices": [{"index": 0, "delta": {"content": text[i : i + 5]}}]}
        events.append(b"data: " + json.dumps(obj).encode() + b"\n\n")
    events.append(b"data: [DONE]\n\n")
    blob = b"".join(events)
    return [blob[i : i + 37] for i in range(0, len(blob), 37)]


def _exchange(
    body: bytes, response_chunks: list[bytes], content_type: str
) -> list[pb.ProcessingRequest]:
    reqs = [
        pb.ProcessingRequest(
            request_headers=pb.HttpHeaders(headers=_headers(content_type="application/json"))
        )
    ]
    cuts = [0, len(body) // 3, 2 * len(body) // 3, len(body)]
    for a, b in zip(cuts, cuts[1:], strict=False):
        reqs.append(pb.ProcessingRequest(request_body=pb.HttpBody(body=body[a:b])))
    reqs.append(pb.ProcessingRequest(request_body=pb.HttpBody(body=b"", end_of_stream=True)))
    reqs.append(
        pb.ProcessingRequest(
            response_headers=pb.HttpHeaders(headers=_headers(content_type=content_type))
        )
    )
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


def _bodies(responses: list[pb.ProcessingResponse], leg: str) -> list[pb.StreamedBodyResponse]:
    out = []
    for r in responses:
        if r.WhichOneof("response") == leg:
            out.append(getattr(r, leg).response.body_mutation.streamed_response)
    return out


def _meta(resp: pb.ProcessingResponse) -> dict:
    from google.protobuf.json_format import MessageToDict

    return MessageToDict(resp.dynamic_metadata)["standin"]


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_enforce_full_exchange() -> None:
    body = _request_body()
    reply = f"Running tailscale up --authkey {PH} now."
    before = _sample("standin_restored_total")
    responses, code = await _drive(
        ExternalProcessor(TOK, mode="enforce"),
        _exchange(body, _sse_response(reply), "text/event-stream"),
    )
    assert code == grpc.StatusCode.OK
    kinds = [r.WhichOneof("response") for r in responses]
    assert kinds[0] == "request_headers" and kinds[1] == "request_body"
    assert kinds.count("request_body") == 1

    (req_body,) = _bodies(responses, "request_body")
    assert req_body.end_of_stream
    assert TSKEY.encode() not in req_body.body and PH.encode() in req_body.body
    assert json.loads(req_body.body)["messages"][1]["content"] == f"TS_AUTHKEY={PH}\n"

    resp_bodies = _bodies(responses, "response_body")
    assert resp_bodies[-1].end_of_stream and not any(b.end_of_stream for b in resp_bodies[:-1])
    client_sees = b"".join(b.body for b in resp_bodies)
    text = "".join(
        json.loads(line[6:])["choices"][0]["delta"]["content"]
        for line in client_sees.split(b"\n")
        if line.startswith(b"data: {")
    )
    assert text == reply.replace(PH, TSKEY)
    assert _sample("standin_restored_total") - before == 1

    headers = [r for r in responses if r.WhichOneof("response").endswith("_headers")]
    assert all(r.HasField("dynamic_metadata") for r in headers)
    bodies = [r for r in responses if r.WhichOneof("response").endswith("_body")]
    assert not any(r.HasField("dynamic_metadata") for r in bodies)
    for resp in headers:
        meta = _meta(resp)
        assert meta["mode"] == "enforce" and meta["tokenized"] == 1 and meta["distinct"] == 1
        assert meta["tier_a"] == 1 and meta["tier_b"] == 0


async def test_request_headers_answered_immediately_when_not_deferred() -> None:
    body = _request_body()
    reqs = _exchange(body, [b"{}"], "application/json")
    responses, code = await _drive(
        ExternalProcessor(TOK, mode="enforce", defer_request_headers=False), reqs
    )
    assert code == grpc.StatusCode.OK
    first = responses[0]
    assert first.WhichOneof("response") == "request_headers"
    assert _meta(first)["tokenized"] == 0
    assert [r.WhichOneof("response") for r in responses][:2] == ["request_headers", "request_body"]


async def test_shadow_sends_original_and_does_not_restore() -> None:
    body = _request_body()
    chunks = _sse_response(f"echo {PH}")
    before = _sample("standin_requests_total", mode="shadow", outcome="tokenized")
    responses, code = await _drive(
        ExternalProcessor(TOK, mode="shadow"), _exchange(body, chunks, "text/event-stream")
    )
    assert code == grpc.StatusCode.OK
    (req_body,) = _bodies(responses, "request_body")
    assert req_body.body == body and req_body.end_of_stream
    assert b"".join(b.body for b in _bodies(responses, "response_body")) == b"".join(chunks)
    meta = _meta([r for r in responses if r.WhichOneof("response") == "response_headers"][0])
    assert meta["mode"] == "shadow" and meta["tokenized"] == 1
    after = _sample("standin_requests_total", mode="shadow", outcome="tokenized")
    assert after - before == 1


async def test_non_streaming_json_response_is_restored() -> None:
    reply = json.dumps({"choices": [{"message": {"content": f"key {PH}"}}]}).encode()
    chunks = [reply[:30], reply[30:41], reply[41:]]
    responses, _ = await _drive(
        ExternalProcessor(TOK, mode="enforce"),
        _exchange(_request_body(), chunks, "application/json"),
    )
    out = b"".join(b.body for b in _bodies(responses, "response_body"))
    assert json.loads(out)["choices"][0]["message"]["content"] == f"key {TSKEY}"


async def test_non_json_body_passes_through() -> None:
    body = b"plain text " + TSKEY.encode()
    before = _sample("standin_requests_total", mode="enforce", outcome="skipped_non_json")
    responses, code = await _drive(
        ExternalProcessor(TOK, mode="enforce"), _exchange(body, [b"ok"], "text/plain")
    )
    assert code == grpc.StatusCode.OK
    (req_body,) = _bodies(responses, "request_body")
    assert req_body.body == body
    after = _sample("standin_requests_total", mode="enforce", outcome="skipped_non_json")
    assert after - before == 1


async def test_large_body_is_sent_in_slices() -> None:
    body = _request_body()
    responses, _ = await _drive(
        ExternalProcessor(TOK, mode="enforce", chunk_bytes=50),
        _exchange(body, [b"{}"], "application/json"),
    )
    slices = _bodies(responses, "request_body")
    assert len(slices) > 2
    assert [s.end_of_stream for s in slices] == [False] * (len(slices) - 1) + [True]
    assert TSKEY.encode() not in b"".join(s.body for s in slices)


async def test_tokenize_exception_passes_original_body(monkeypatch: pytest.MonkeyPatch) -> None:
    tok = Tokenizer(salt="boom", cache_entries=0)

    def boom(body: bytes) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(tok, "tokenize_json", boom)
    before = _sample("standin_errors_total", phase="request_body")
    body = _request_body()
    responses, code = await _drive(
        ExternalProcessor(tok, mode="enforce"), _exchange(body, [b"a", b"b"], "text/event-stream")
    )
    assert code == grpc.StatusCode.OK
    (req_body,) = _bodies(responses, "request_body")
    assert req_body.body == body and req_body.end_of_stream
    resp = _bodies(responses, "response_body")
    assert b"".join(b.body for b in resp) == b"ab" and resp[-1].end_of_stream
    assert _sample("standin_errors_total", phase="request_body") - before == 1


async def test_restorer_exception_still_terminates_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    original_feed = SseRestorer.feed

    def flaky(self: SseRestorer, chunk: bytes) -> bytes:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("synthetic failure")
        return original_feed(self, chunk)

    monkeypatch.setattr(SseRestorer, "feed", flaky)
    chunks = [b"data: {}\n\n", b"data: <<sec", b"ret-tail\n\n", b"data: [DONE]\n\n"]
    before = _sample("standin_errors_total", phase="response_body")
    responses, code = await _drive(
        ExternalProcessor(TOK, mode="enforce"),
        _exchange(_request_body(), chunks, "text/event-stream"),
    )
    assert code == grpc.StatusCode.OK
    resp = _bodies(responses, "response_body")
    assert b"".join(b.body for b in resp).startswith(b"data: {}\n\ndata: <<sec")
    assert _sample("standin_errors_total", phase="response_body") - before == 1


async def test_headers_only_request() -> None:
    reqs = [
        pb.ProcessingRequest(request_headers=pb.HttpHeaders(end_of_stream=True)),
        pb.ProcessingRequest(response_headers=pb.HttpHeaders()),
        pb.ProcessingRequest(response_body=pb.HttpBody(body=b"x", end_of_stream=True)),
    ]
    responses, code = await _drive(ExternalProcessor(TOK, mode="enforce"), reqs)
    assert code == grpc.StatusCode.OK
    assert [r.WhichOneof("response") for r in responses] == [
        "request_headers",
        "response_headers",
        "response_body",
    ]


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
@pytest.mark.parametrize("defer", [True, False])
async def test_request_headers_response_strips_accept_encoding(mode: str, defer: bool) -> None:
    reqs = _exchange(_request_body(), [b"{}"], "application/json")
    reqs[0].request_headers.headers.headers.add(key="accept-encoding", raw_value=b"gzip, br")
    responses, code = await _drive(
        ExternalProcessor(TOK, mode=mode, defer_request_headers=defer), reqs
    )
    assert code == grpc.StatusCode.OK
    (headers_resp,) = [r for r in responses if r.WhichOneof("response") == "request_headers"]
    mutation = headers_resp.request_headers.response.header_mutation
    assert list(mutation.remove_headers) == ["accept-encoding"]
    assert not mutation.set_headers
    for r in responses:
        if r.WhichOneof("response") != "request_headers":
            assert not getattr(r, r.WhichOneof("response")).response.HasField("header_mutation")


async def test_headers_only_request_also_strips_accept_encoding() -> None:
    reqs = [pb.ProcessingRequest(request_headers=pb.HttpHeaders(end_of_stream=True))]
    responses, _ = await _drive(ExternalProcessor(TOK, mode="enforce"), reqs)
    assert list(responses[0].request_headers.response.header_mutation.remove_headers) == [
        "accept-encoding"
    ]


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValueError):
        ExternalProcessor(TOK, mode="mask")


async def test_serve_health_and_graceful_stop() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stop = asyncio.Event()
    task = asyncio.create_task(main_mod.serve(ExternalProcessor(TOK), port, stop, 1.0))
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            for _ in range(50):
                try:
                    resp = await stub.Check(
                        health_pb2.HealthCheckRequest(service=main_mod.SERVICE_NAME)
                    )
                    break
                except grpc.aio.AioRpcError:
                    await asyncio.sleep(0.05)
            assert resp.status == health_pb2.HealthCheckResponse.SERVING
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


def test_json_formatter_renders_counts_only() -> None:
    import logging

    record = logging.makeLogRecord({"msg": "exchange", "levelname": "INFO", "tokenized": 2})
    entry = json.loads(main_mod.JsonFormatter().format(record))
    assert entry["msg"] == "exchange" and entry["tokenized"] == 2
