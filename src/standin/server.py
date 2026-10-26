"""envoy.service.ext_proc.v3.ExternalProcessor for agentgateway.

One bidi stream per HTTP exchange: request_headers, request_body chunks,
response_headers, response_body chunks (trailers are skipped by the route's
processingOptions; both body legs are fullDuplexStreamed).

- Request body: chunks are withheld and accumulated; at end_of_stream the
  whole body is tokenized and sent back as StreamedBodyResponse messages (in
  slices of at most ``chunk_bytes``), the last with end_of_stream=true.
- Response body: every chunk is answered with the restored bytes so far; the
  end_of_stream chunk also carries the restorer's held tail. The final
  end_of_stream=true response is always sent or the client hangs.
- dynamic_metadata goes only on headers responses (agentgateway drops it on
  body responses) under ``standin``. agentgateway v1.6.0 only keeps
  the request_headers one, so that response is deferred until the body has
  been tokenized and carries the real counts (agentgateway keeps streaming the
  body meanwhile; verified with scripts/twin.sh). STANDIN_DEFER_REQUEST_HEADERS=false
  answers immediately instead (counts then read 0). The response_headers
  response repeats the struct for processors that honour it.
- Errors never surface as gRPC errors (a response-side error fails the user's
  request): the exchange degrades to pass-through, and if even that fails the
  handler returns, which agentgateway treats as "skip the remaining phases".

Logs carry counts only, never values, placeholders or hashes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Iterator
from typing import Protocol

import grpc
from google.protobuf import struct_pb2

from standin import metrics
from standin._proto.envoy.service.ext_proc.v3 import (
    external_processor_pb2 as pb,
)
from standin._proto.envoy.service.ext_proc.v3 import (
    external_processor_pb2_grpc as pb_grpc,
)
from standin.sse import SseRestorer
from standin.tokenizer import Restorer, Tokenizer, TokenizeResult

log = logging.getLogger("standin.server")

MODES = ("shadow", "enforce")
DEFAULT_CHUNK_BYTES = 1 << 20
METADATA_KEY = "standin"
# Upstream must answer uncompressed: the restorer works on plain bytes, and
# shadow strips it too so both modes measure the same wire. A compressed
# response that still arrives is counted as errors_total{phase="response_encoded"}.
STRIP_REQUEST_HEADERS = ["accept-encoding"]


class _Stream(Protocol):
    restored: int
    errors: int

    @property
    def held(self) -> int: ...

    def feed(self, chunk: bytes) -> bytes: ...

    def flush(self) -> bytes: ...

    def abandon(self, chunk: bytes) -> bytes: ...


class _Passthrough:
    restored = 0
    errors = 0
    held = 0

    def feed(self, chunk: bytes) -> bytes:
        return chunk

    def flush(self) -> bytes:
        return b""

    def abandon(self, chunk: bytes) -> bytes:
        return chunk


class _Abort(Exception):
    """Stop processing; the handler returns and agentgateway passes the rest through."""


class _Exchange:
    def __init__(
        self, tokenizer: Tokenizer, mode: str, chunk_bytes: int, defer_request_headers: bool
    ) -> None:
        self.tokenizer = tokenizer
        self.mode = mode
        self.chunk_bytes = chunk_bytes
        self.request_chunks: list[bytes] = []
        self.request_answered = False
        self.result: TokenizeResult | None = None
        self.restorer: _Stream = _Passthrough()
        self.outcome = "incomplete"
        self.restored_reported = 0
        self.defer_request_headers = defer_request_headers
        self.headers_pending = False

    def metadata(self) -> struct_pb2.Struct:
        result = self.result
        tiers = result.tier_counts() if result else {"a": 0, "b": 0}
        meta = struct_pb2.Struct()
        meta.update(
            {
                METADATA_KEY: {
                    "mode": self.mode,
                    "tokenized": result.replaced if result else 0,
                    "distinct": result.distinct if result else 0,
                    "tier_a": tiers["a"],
                    "tier_b": tiers["b"],
                    "restored": self.restorer.restored,
                }
            }
        )
        return meta

    def handle(self, req: pb.ProcessingRequest) -> Iterator[pb.ProcessingResponse]:
        phase = req.WhichOneof("request")
        if phase == "request_headers":
            if not req.request_headers.end_of_stream and self.defer_request_headers:
                self.headers_pending = True
                return
            if req.request_headers.end_of_stream:
                self.request_answered = True
                self.outcome = "no_body"
            yield self._request_headers_response()
        elif phase == "request_body":
            yield from self._request_body(req.request_body)
        elif phase == "response_headers":
            if not self.request_answered:
                self.outcome = "no_body"
                self.request_answered = True
            self._choose_restorer(req.response_headers)
            yield pb.ProcessingResponse(
                response_headers=pb.HeadersResponse(response=pb.CommonResponse()),
                dynamic_metadata=self.metadata(),
            )
        elif phase == "response_body":
            yield from self._response_body(req.response_body)
        elif phase == "request_trailers":
            yield pb.ProcessingResponse(request_trailers=pb.TrailersResponse())
        elif phase == "response_trailers":
            yield pb.ProcessingResponse(response_trailers=pb.TrailersResponse())

    def _request_body(self, body: pb.HttpBody) -> Iterator[pb.ProcessingResponse]:
        if self.request_answered:
            raise _Abort("request body after the request leg was answered")
        if body.body:
            self.request_chunks.append(body.body)
        if not body.end_of_stream:
            return
        original = b"".join(self.request_chunks)
        self.request_chunks = []
        outgoing = original
        try:
            outgoing = self._tokenize(original)
        except Exception as exc:
            metrics.ERRORS.labels(phase="request_body").inc()
            log.error(
                "tokenize failed; request passed through",
                extra={"error": type(exc).__name__, "bytes": len(original)},
            )
            self.outcome = "error"
            self.result = None
        self.request_answered = True
        if self.headers_pending:
            self.headers_pending = False
            yield self._request_headers_response()
        yield from _streamed(outgoing, self.chunk_bytes, request=True)

    def _request_headers_response(self) -> pb.ProcessingResponse:
        return pb.ProcessingResponse(
            request_headers=pb.HeadersResponse(
                response=pb.CommonResponse(
                    header_mutation=pb.HeaderMutation(remove_headers=STRIP_REQUEST_HEADERS)
                )
            ),
            dynamic_metadata=self.metadata(),
        )

    def _tokenize(self, body: bytes) -> bytes:
        metrics.BODY_BYTES.observe(len(body))
        started = time.perf_counter()
        result = self.tokenizer.tokenize_json(body)
        metrics.TOKENIZE_SECONDS.observe(time.perf_counter() - started)
        if result is None:
            self.outcome = "skipped_non_json"
            return body
        self.result = result
        for tier, index in result.hits:
            metrics.MATCHES.labels(tier=tier, pattern_index=str(index)).inc()
        if not result.replaced:
            self.outcome = "clean"
            return body
        self.outcome = "tokenized"
        if self.mode != "enforce":
            return body
        return result.text.encode("utf-8")

    def _choose_restorer(self, headers: pb.HttpHeaders) -> None:
        if self.mode != "enforce" or self.result is None or not self.result.mapping:
            return
        values = {
            h.key.lower(): (h.raw_value or h.value.encode()).decode("latin-1").lower()
            for h in headers.headers.headers
        }
        if values.get("content-encoding", "identity") not in ("", "identity"):
            metrics.ERRORS.labels(phase="response_encoded").inc()
            return
        if "text/event-stream" in values.get("content-type", ""):
            self.restorer = SseRestorer(self.result.mapping)
        else:
            self.restorer = Restorer(self.result.mapping)

    def _response_body(self, body: pb.HttpBody) -> Iterator[pb.ProcessingResponse]:
        errors_before = self.restorer.errors
        out = self.restorer.feed(body.body)
        if body.end_of_stream:
            out += self.restorer.flush()
        if self.restorer.errors != errors_before:
            metrics.ERRORS.labels(phase="response_event").inc(self.restorer.errors - errors_before)
        metrics.HELD_BYTES.observe(self.restorer.held)
        self._report_restored()
        yield _streamed_one(out, body.end_of_stream, request=False)

    def fail_open_response(self, body: pb.HttpBody) -> pb.ProcessingResponse:
        """The chunk that raised, preceded by whatever the restorer still holds;
        every later chunk passes through untouched."""
        try:
            data = self.restorer.abandon(body.body)
        except Exception:
            metrics.ERRORS.labels(phase="response_abandon").inc()
            data = body.body
        self.restorer = _Passthrough()
        return _streamed_one(data, body.end_of_stream, request=False)

    def _report_restored(self) -> None:
        delta = self.restorer.restored - self.restored_reported
        if delta:
            metrics.RESTORED.inc(delta)
            self.restored_reported = self.restorer.restored


def _streamed_one(data: bytes, end_of_stream: bool, *, request: bool) -> pb.ProcessingResponse:
    body = pb.BodyResponse(
        response=pb.CommonResponse(
            body_mutation=pb.BodyMutation(
                streamed_response=pb.StreamedBodyResponse(body=data, end_of_stream=end_of_stream)
            )
        )
    )
    if request:
        return pb.ProcessingResponse(request_body=body)
    return pb.ProcessingResponse(response_body=body)


def _streamed(data: bytes, chunk_bytes: int, *, request: bool) -> Iterator[pb.ProcessingResponse]:
    if len(data) <= chunk_bytes:
        yield _streamed_one(data, True, request=request)
        return
    for start in range(0, len(data), chunk_bytes):
        piece = data[start : start + chunk_bytes]
        yield _streamed_one(piece, start + chunk_bytes >= len(data), request=request)


class ExternalProcessor(pb_grpc.ExternalProcessorServicer):
    def __init__(
        self,
        tokenizer: Tokenizer,
        mode: str = "shadow",
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        defer_request_headers: bool = True,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"STANDIN_MODE must be one of {MODES}")
        self.tokenizer = tokenizer
        self.mode = mode
        self.chunk_bytes = chunk_bytes
        self.defer_request_headers = defer_request_headers

    async def Process(
        self,
        request_iterator: AsyncIterator[pb.ProcessingRequest],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb.ProcessingResponse]:
        ex = _Exchange(self.tokenizer, self.mode, self.chunk_bytes, self.defer_request_headers)
        try:
            async for req in request_iterator:
                phase = req.WhichOneof("request") or "unknown"
                try:
                    responses = list(ex.handle(req))
                except _Abort:
                    metrics.ERRORS.labels(phase=phase).inc()
                    ex.outcome = "error"
                    return
                except Exception as exc:
                    metrics.ERRORS.labels(phase=phase).inc()
                    log.error(
                        "ext_proc phase failed; passing through",
                        extra={"phase": phase, "error": type(exc).__name__},
                    )
                    ex.outcome = "error"
                    if phase != "response_body":
                        return
                    try:
                        responses = [ex.fail_open_response(req.response_body)]
                    except Exception:
                        return
                for resp in responses:
                    yield resp
        finally:
            metrics.REQUESTS.labels(mode=self.mode, outcome=ex.outcome).inc()
            result = ex.result
            if result is not None and (result.replaced or ex.restorer.restored):
                tiers = result.tier_counts()
                log.info(
                    "exchange",
                    extra={
                        "mode": self.mode,
                        "outcome": ex.outcome,
                        "tokenized": result.replaced,
                        "distinct": result.distinct,
                        "tier_a": tiers["a"],
                        "tier_b": tiers["b"],
                        "restored": ex.restorer.restored,
                    },
                )
