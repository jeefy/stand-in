from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any

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
from standin.patterns import load_count_rules
from standin.server import ExternalProcessor, make_tokenize_pool
from standin.sse import SseRestorer
from standin.tokenizer import Tokenizer
from standin.turns import SCOPES

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
            stub: Any = health_pb2_grpc.HealthStub(channel)
            resp = None
            for _ in range(50):
                try:
                    resp = await stub.Check(
                        health_pb2.HealthCheckRequest(service=main_mod.SERVICE_NAME)
                    )
                    break
                except grpc.aio.AioRpcError:
                    await asyncio.sleep(0.05)
            assert resp is not None and resp.status == health_pb2.HealthCheckResponse.SERVING
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


def test_json_formatter_renders_counts_only() -> None:
    import logging

    record = logging.makeLogRecord({"msg": "exchange", "levelname": "INFO", "tokenized": 2})
    entry = json.loads(main_mod.JsonFormatter().format(record))
    assert entry["msg"] == "exchange" and entry["tokenized"] == 2


def _proc(tok: Tokenizer = TOK, **kwargs: object) -> ExternalProcessor:
    kwargs.setdefault("mode", "enforce")
    return ExternalProcessor(tok, **kwargs)  # type: ignore[arg-type]


def _request_leg(responses: list[pb.ProcessingResponse]) -> tuple[bytes, list[bool]]:
    bodies = _bodies(responses, "request_body")
    return b"".join(b.body for b in bodies), [b.end_of_stream for b in bodies]


async def test_oversize_body_switches_to_pass_through() -> None:
    body = _request_body()
    reqs = _exchange(body, [f"echo {PH}".encode()], "application/json")
    before = _sample("standin_requests_total", mode="enforce", outcome="oversize")
    responses, code = await _drive(_proc(max_body_bytes=50), reqs)
    assert code == grpc.StatusCode.OK
    kinds = [r.WhichOneof("response") for r in responses]
    assert kinds == ["request_headers"] + ["request_body"] * 4 + [
        "response_headers",
        "response_body",
    ]
    assert _meta(responses[0])["tokenized"] == 0
    sent, eos = _request_leg(responses)
    assert sent == body and eos == [False, False, False, True]
    restored = _bodies(responses, "response_body")[0].body
    assert restored == f"echo {PH}".encode()
    after = _sample("standin_requests_total", mode="enforce", outcome="oversize")
    assert after - before == 1


async def test_oversize_final_chunk_is_sliced_with_one_end_of_stream() -> None:
    body = _request_body()
    reqs = [
        pb.ProcessingRequest(request_headers=pb.HttpHeaders()),
        pb.ProcessingRequest(request_body=pb.HttpBody(body=body, end_of_stream=True)),
    ]
    responses, _ = await _drive(
        _proc(max_body_bytes=10, chunk_bytes=64, defer_request_headers=False), reqs
    )
    assert responses[0].WhichOneof("response") == "request_headers"
    sent, eos = _request_leg(responses)
    assert sent == body and len(eos) > 1 and eos == [False] * (len(eos) - 1) + [True]


async def test_body_at_cap_is_still_tokenized() -> None:
    body = _request_body()
    responses, _ = await _drive(
        _proc(max_body_bytes=len(body)), _exchange(body, [b"{}"], "application/json")
    )
    sent, _ = _request_leg(responses)
    assert TSKEY.encode() not in sent and PH.encode() in sent


async def test_active_streams_gauge_returns_to_zero() -> None:
    await _drive(_proc(), _exchange(_request_body(), [b"{}"], "application/json"))
    aborted = [
        pb.ProcessingRequest(request_headers=pb.HttpHeaders(end_of_stream=True)),
        pb.ProcessingRequest(request_body=pb.HttpBody(body=b"x", end_of_stream=True)),
    ]
    await _drive(_proc(), aborted)
    for _ in range(50):
        if _sample("standin_active_streams") == 0:
            break
        await asyncio.sleep(0.01)
    assert _sample("standin_active_streams") == 0


def _cacheable_body() -> bytes:
    doc = json.loads(_request_body())
    doc["messages"].append({"role": "user", "content": "long context " * 60})
    return json.dumps(doc).encode()


async def test_cache_hits_counted() -> None:
    tok = Tokenizer(salt="server-tests", cache_entries=100)
    body = _cacheable_body()
    before = _sample("standin_cache_hits_total")
    for _ in range(2):
        await _drive(_proc(tok), _exchange(body, [b"{}"], "application/json"))
    assert _sample("standin_cache_hits_total") - before >= 1


async def test_compressed_response_is_not_restored() -> None:
    reqs = _exchange(_request_body(), [f"key {PH}".encode()], "application/json")
    reqs[5].response_headers.headers.headers.add(key="content-encoding", raw_value=b"gzip")
    before = _sample("standin_errors_total", phase="response_encoded")
    responses, code = await _drive(_proc(), reqs)
    assert code == grpc.StatusCode.OK
    assert _bodies(responses, "response_body")[0].body == f"key {PH}".encode()
    assert _sample("standin_errors_total", phase="response_encoded") - before == 1


async def test_header_value_field_is_read_when_raw_value_is_empty() -> None:
    reqs = _exchange(_request_body(), [f"key {PH}".encode()], "application/json")
    reqs[5].response_headers.headers.headers.add(key="Content-Encoding", value="GZIP")
    before = _sample("standin_errors_total", phase="response_encoded")
    responses, _ = await _drive(_proc(), reqs)
    assert _bodies(responses, "response_body")[0].body == f"key {PH}".encode()
    assert _sample("standin_errors_total", phase="response_encoded") - before == 1

    reqs = _exchange(_request_body(), [f"key {PH}".encode()], "application/json")
    del reqs[5].response_headers.headers.headers[:]
    reqs[5].response_headers.headers.headers.add(key="content-type", value="application/json")
    responses, _ = await _drive(_proc(), reqs)
    assert _bodies(responses, "response_body")[0].body == f"key {TSKEY}".encode()


async def test_request_body_after_answered_request_aborts() -> None:
    reqs = [
        pb.ProcessingRequest(request_headers=pb.HttpHeaders(end_of_stream=True)),
        pb.ProcessingRequest(request_body=pb.HttpBody(body=b"late", end_of_stream=True)),
        pb.ProcessingRequest(response_headers=pb.HttpHeaders()),
    ]
    errors = _sample("standin_errors_total", phase="request_body")
    outcome = _sample("standin_requests_total", mode="enforce", outcome="error")
    responses, code = await _drive(_proc(), reqs)
    assert code == grpc.StatusCode.OK
    assert [r.WhichOneof("response") for r in responses] == ["request_headers"]
    assert _sample("standin_errors_total", phase="request_body") - errors == 1
    assert _sample("standin_requests_total", mode="enforce", outcome="error") - outcome == 1


async def test_fail_open_when_abandon_also_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: SseRestorer, chunk: bytes) -> bytes:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(SseRestorer, "feed", boom)
    monkeypatch.setattr(SseRestorer, "abandon", boom)
    chunks = [b"data: {}\n\n", b"data: [DONE]\n\n"]
    before = _sample("standin_errors_total", phase="response_abandon")
    responses, code = await _drive(_proc(), _exchange(_request_body(), chunks, "text/event-stream"))
    assert code == grpc.StatusCode.OK
    resp = _bodies(responses, "response_body")
    assert b"".join(b.body for b in resp) == b"".join(chunks) and resp[-1].end_of_stream
    assert _sample("standin_errors_total", phase="response_abandon") - before == 1


async def test_enforce_clean_body_is_forwarded_without_restorer() -> None:
    body = json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode()
    before = _sample("standin_requests_total", mode="enforce", outcome="clean")
    responses, _ = await _drive(_proc(), _exchange(body, [f"x {PH}".encode()], "application/json"))
    sent, eos = _request_leg(responses)
    assert sent == body and eos == [True]
    assert _bodies(responses, "response_body")[0].body == f"x {PH}".encode()
    assert _sample("standin_requests_total", mode="enforce", outcome="clean") - before == 1


async def test_trailers_are_answered() -> None:
    reqs = [
        pb.ProcessingRequest(request_trailers=pb.HttpTrailers()),
        pb.ProcessingRequest(response_trailers=pb.HttpTrailers()),
    ]
    responses, code = await _drive(_proc(), reqs)
    assert code == grpc.StatusCode.OK
    assert [r.WhichOneof("response") for r in responses] == [
        "request_trailers",
        "response_trailers",
    ]


@pytest.fixture(scope="module")
def pool() -> Iterator[ProcessPoolExecutor]:
    executor = make_tokenize_pool(1, salt="server-tests", cache_entries=100)
    yield executor
    executor.shutdown(wait=True, cancel_futures=True)


async def test_pool_tokenizes_large_body(pool: ProcessPoolExecutor) -> None:
    body = _cacheable_body()
    reply = f"Running tailscale up --authkey {PH} now."
    tok = Tokenizer(salt="server-tests", cache_entries=0)
    before = _sample("standin_cache_hits_total")
    for _ in range(2):
        responses, code = await _drive(
            _proc(tok, pool=pool, offload_bytes=10),
            _exchange(body, _sse_response(reply), "text/event-stream"),
        )
        assert code == grpc.StatusCode.OK
        sent, _ = _request_leg(responses)
        assert TSKEY.encode() not in sent and PH.encode() in sent
        assert _meta(responses[0])["tokenized"] == 1
    assert tok.cache_hits == 0
    assert _sample("standin_cache_hits_total") - before >= 1


async def test_pool_worker_exception_passes_through(pool: ProcessPoolExecutor) -> None:
    body = b"[" * 100_000 + b"]" * 100_000
    before = _sample("standin_errors_total", phase="request_body")
    responses, code = await _drive(
        _proc(pool=pool, offload_bytes=10), _exchange(body, [b"ok"], "application/json")
    )
    assert code == grpc.StatusCode.OK
    sent, eos = _request_leg(responses)
    assert sent == body and eos[-1]
    assert _sample("standin_errors_total", phase="request_body") - before == 1


async def test_broken_pool_falls_back_inline() -> None:
    import multiprocessing
    import os

    broken = ProcessPoolExecutor(
        1, mp_context=multiprocessing.get_context("spawn"), initializer=os._exit, initargs=(3,)
    )
    processor = _proc(pool=broken, offload_bytes=10)
    before = _sample("standin_errors_total", phase="tokenize_pool")
    try:
        for _ in range(2):
            responses, _ = await _drive(
                processor, _exchange(_request_body(), [b"{}"], "application/json")
            )
            sent, _ = _request_leg(responses)
            assert TSKEY.encode() not in sent and PH.encode() in sent
    finally:
        broken.shutdown(wait=True)
    assert processor.pool is None
    assert _sample("standin_errors_total", phase="tokenize_pool") - before == 1


class _FakePool(ThreadPoolExecutor):
    def __init__(self) -> None:
        super().__init__(1)
        self.shut = False

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shut = True
        super().shutdown(wait, cancel_futures=cancel_futures)


async def test_serve_reports_not_serving_while_draining() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stop = asyncio.Event()
    fake = _FakePool()
    task = asyncio.create_task(
        main_mod.serve(ExternalProcessor(TOK), port, stop, 1.0, drain=0.5, pool=fake)
    )
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub: Any = health_pb2_grpc.HealthStub(channel)

            async def status(service: str) -> int:
                return (await stub.Check(health_pb2.HealthCheckRequest(service=service))).status

            for _ in range(50):
                try:
                    assert await status("") == health_pb2.HealthCheckResponse.SERVING
                    break
                except grpc.aio.AioRpcError:
                    await asyncio.sleep(0.05)
            assert await status(main_mod.SERVICE_NAME) == health_pb2.HealthCheckResponse.SERVING
            with pytest.raises(grpc.aio.AioRpcError) as unknown:
                await status("nope")
            assert unknown.value.code() == grpc.StatusCode.NOT_FOUND

            stop.set()
            for _ in range(20):
                if await status("") == health_pb2.HealthCheckResponse.NOT_SERVING:
                    break
                await asyncio.sleep(0.02)
            assert await status("") == health_pb2.HealthCheckResponse.NOT_SERVING
            assert await status(main_mod.SERVICE_NAME) == health_pb2.HealthCheckResponse.NOT_SERVING
            assert not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    assert fake.shut


def test_settings_defaults() -> None:
    s = main_mod._settings_from_env({})
    assert s == main_mod.Settings()
    assert s.max_body_bytes == 32 << 20 and s.tokenize_workers == 1
    assert s.offload_bytes == 262144 and s.drain == 10.0 and s.max_concurrent_streams is None


def test_settings_overrides() -> None:
    s = main_mod._settings_from_env(
        {
            "STANDIN_MODE": " Enforce ",
            "STANDIN_SALT": "s3",
            "STANDIN_MAX_BODY_BYTES": "0",
            "STANDIN_TOKENIZE_WORKERS": "0",
            "STANDIN_TOKENIZE_OFFLOAD_BYTES": "1024",
            "STANDIN_MAX_CONCURRENT_STREAMS": "64",
            "STANDIN_DRAIN_SECONDS": "2.5",
            "STANDIN_SHUTDOWN_GRACE": "30",
            "STANDIN_DEFER_REQUEST_HEADERS": "false",
            "STANDIN_CHUNK_BYTES": "4096",
        }
    )
    assert s.mode == "enforce" and s.salt == "s3"
    assert s.max_body_bytes == 0 and s.tokenize_workers == 0 and s.offload_bytes == 1024
    assert s.max_concurrent_streams == 64 and s.drain == 2.5 and s.grace == 30.0
    assert not s.defer_request_headers and s.chunk_bytes == 4096


@pytest.mark.parametrize("salt", [None, ""])
def test_enforce_without_salt_exits(salt: str | None) -> None:
    env = {"STANDIN_MODE": "enforce"}
    if salt is not None:
        env["STANDIN_SALT"] = salt
    with pytest.raises(SystemExit) as exc:
        main_mod._settings_from_env(env)
    assert exc.value.code == 2


def test_default_salt_allowed_in_shadow_or_with_override() -> None:
    assert main_mod._settings_from_env({}).salt is None
    s = main_mod._settings_from_env(
        {"STANDIN_MODE": "enforce", "STANDIN_ALLOW_DEFAULT_SALT": "true"}
    )
    assert s.mode == "enforce" and s.salt is None


@pytest.mark.parametrize(
    "env",
    [
        {"STANDIN_MODE": "mask"},
        {"STANDIN_MAX_BODY_BYTES": "lots"},
        {"STANDIN_DRAIN_SECONDS": "-1"},
        {"STANDIN_DEFER_REQUEST_HEADERS": "maybe"},
    ],
)
def test_invalid_settings_exit(env: dict[str, str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main_mod._settings_from_env(env)
    assert exc.value.code == 2


def test_json_formatter_adds_traceback() -> None:
    import logging
    import sys

    try:
        raise RuntimeError("synthetic")
    except RuntimeError:
        record = logging.makeLogRecord(
            {"msg": "failed", "levelname": "ERROR", "exc_info": sys.exc_info()}
        )
    entry = json.loads(main_mod.JsonFormatter().format(record))
    assert entry["exc"].startswith("Traceback") and "RuntimeError: synthetic" in entry["exc"]
    assert "exc_info" not in entry


@pytest.mark.parametrize(("size", "slices"), [(1, 1), (64, 1), (65, 2), (128, 2), (129, 3)])
def test_streamed_slice_boundaries(size: int, slices: int) -> None:
    from standin.server import _streamed

    data = bytes(range(256)) * 2
    data = data[:size]
    out = [
        r.request_body.response.body_mutation.streamed_response
        for r in _streamed(data, 64, request=True)
    ]
    assert len(out) == slices and b"".join(s.body for s in out) == data
    assert all(len(s.body) <= 64 for s in out)
    assert [s.end_of_stream for s in out] == [False] * (slices - 1) + [True]
    held = list(_streamed(data, 64, request=False, end_of_stream=False))
    assert not any(
        r.response_body.response.body_mutation.streamed_response.end_of_stream for r in held
    )


async def test_exchange_log_carries_counts_only(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="standin.server")
    reply = f"key {PH} ok"
    await _drive(_proc(), _exchange(_request_body(), _sse_response(reply), "text/event-stream"))
    records = [r for r in caplog.records if r.getMessage() == "exchange"]
    assert len(records) == 1
    rec = records[0]
    assert (rec.levelno, rec.tokenized, rec.distinct, rec.restored) == (logging.INFO, 1, 1, 1)  # type: ignore[attr-defined]
    for v in vars(rec).values():
        text = str(v)
        assert TSKEY not in text and PH not in text and "<<secret-" not in text

    caplog.clear()
    clean = json.dumps({"messages": [{"role": "user", "content": "hello"}]}).encode()
    await _drive(_proc(), _exchange(clean, [b"{}"], "application/json"))
    assert not [r for r in caplog.records if r.getMessage() == "exchange"]


def _count_tok() -> Tokenizer:
    return Tokenizer(salt="server-tests", cache_entries=0, count_rules=load_count_rules())


def _continuation_body() -> bytes:
    doc = json.loads(_request_body())
    doc["messages"][1:1] = [{"role": "assistant", "content": None, "tool_calls": []}]
    doc["messages"].append({"role": "user", "content": "password=" + "FAKE0TEST0FA"})
    return json.dumps(doc).encode()


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
async def test_count_new_leaves_body_identical(mode: str) -> None:
    body = _continuation_body()
    reqs = _exchange(body, _sse_response(f"echo {PH}"), "text/event-stream")
    off, _ = await _drive(_proc(TOK, mode=mode), reqs)
    on, _ = await _drive(_proc(_count_tok(), mode=mode, count_new=True), reqs)
    assert _request_leg(off) == _request_leg(on)
    assert _bodies(off, "response_body") == _bodies(on, "response_body")
    meta_off, meta_on = _meta(off[0]), _meta(on[0])
    assert not any(k.startswith("new_") for k in meta_off)
    assert {k: v for k, v in meta_on.items() if not k.startswith("new_")} == meta_off
    assert (meta_on["new_a"], meta_on["new_b"], meta_on["new_c"], meta_on["new_d"]) == (
        1,
        0,
        0,
        1,
    )


async def test_count_new_metrics() -> None:
    scope = _sample("standin_turn_scope_total", scope="tail")
    hits_d = _sample("standin_new_matches_total", tier="d", pattern_index="0")
    await _drive(
        _proc(_count_tok(), count_new=True),
        _exchange(_continuation_body(), [b"{}"], "application/json"),
    )
    assert _sample("standin_turn_scope_total", scope="tail") - scope == 1
    assert _sample("standin_new_matches_total", tier="d", pattern_index="0") - hits_d == 1


async def test_count_new_error_leaves_body(monkeypatch: pytest.MonkeyPatch) -> None:
    tok = _count_tok()

    def boom(body: bytes) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(tok, "count_new", boom)
    body = _continuation_body()
    before = _sample("standin_errors_total", phase="count_new")
    responses, code = await _drive(
        _proc(tok, count_new=True), _exchange(body, [b"{}"], "application/json")
    )
    assert code == grpc.StatusCode.OK
    sent, _ = _request_leg(responses)
    off, _ = await _drive(_proc(TOK), _exchange(body, [b"{}"], "application/json"))
    assert sent == _request_leg(off)[0] and TSKEY.encode() not in sent
    assert not any(k.startswith("new_") for k in _meta(responses[0]))
    assert _sample("standin_errors_total", phase="count_new") - before == 1


async def test_count_new_in_pool() -> None:
    executor = make_tokenize_pool(1, salt="server-tests", cache_entries=0, count_new=True)
    try:
        responses, _ = await _drive(
            _proc(_count_tok(), pool=executor, offload_bytes=10, count_new=True),
            _exchange(_continuation_body(), [b"{}"], "application/json"),
        )
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    meta = _meta(responses[0])
    assert (meta["tokenized"], meta["new_a"], meta["new_d"]) == (1, 1, 1)


def _scopes() -> float:
    return sum(_sample("standin_turn_scope_total", scope=s) for s in SCOPES)


@pytest.fixture(scope="module")
def count_pool() -> Iterator[ProcessPoolExecutor]:
    executor = make_tokenize_pool(1, salt="server-tests", cache_entries=0, count_new=True)
    yield executor
    executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("mode", ["enforce", "shadow"])
@pytest.mark.parametrize("offload", [False, True])
@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        (_continuation_body(), "tokenized"),
        (json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(), "clean"),
        (b"not json", "skipped_non_json"),
    ],
    ids=["tokenized", "clean", "non_json"],
)
async def test_count_new_once_per_body(
    count_pool: ProcessPoolExecutor, mode: str, offload: bool, body: bytes, outcome: str
) -> None:
    pool = {"pool": count_pool, "offload_bytes": 1} if offload else {}
    before = _scopes(), _sample("standin_requests_total", mode=mode, outcome=outcome)
    await _drive(
        _proc(_count_tok(), mode=mode, count_new=True, **pool),
        _exchange(body, [b"{}"], "application/json"),
    )
    after = _scopes(), _sample("standin_requests_total", mode=mode, outcome=outcome)
    assert (after[0] - before[0], after[1] - before[1]) == (1, 1)


async def test_count_new_runs_when_tokenize_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    tok = _count_tok()

    def boom(body: bytes) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(tok, "tokenize_json", boom)
    body = _continuation_body()
    before = _scopes(), _sample("standin_errors_total", phase="request_body")
    responses, code = await _drive(
        _proc(tok, count_new=True), _exchange(body, [b"{}"], "application/json")
    )
    assert code == grpc.StatusCode.OK
    sent, _ = _request_leg(responses)
    assert sent == body
    assert _scopes() - before[0] == 1
    assert _sample("standin_errors_total", phase="request_body") - before[1] == 1
    meta = _meta(responses[0])
    assert (meta["tokenized"], meta["new_a"], meta["new_d"]) == (0, 1, 1)


async def test_count_new_runs_when_pool_tokenize_fails(count_pool: ProcessPoolExecutor) -> None:
    # json.loads raises RecursionError (not ValueError) in the worker, so
    # tokenize_json fails; the count still gets its pass and fails on its own.
    body = b"[" * 100_000 + b"]" * 100_000
    before = _scopes(), _sample("standin_errors_total", phase="count_new")
    responses, _ = await _drive(
        _proc(_count_tok(), pool=count_pool, offload_bytes=1, count_new=True),
        _exchange(body, [b"{}"], "application/json"),
    )
    assert _request_leg(responses)[0] == body
    assert _scopes() - before[0] == 0
    assert _sample("standin_errors_total", phase="count_new") - before[1] == 1


def test_settings_count_new() -> None:
    assert not main_mod._settings_from_env({}).count_new
    assert main_mod._settings_from_env({"STANDIN_COUNT_NEW": "true"}).count_new
