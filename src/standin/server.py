"""envoy.service.ext_proc.v3.ExternalProcessor for agentgateway.

One bidi stream per HTTP exchange: request_headers, request_body chunks,
response_headers, response_body chunks (trailers are skipped by the route's
processingOptions; both body legs are fullDuplexStreamed).

- Request body: chunks are withheld and accumulated; at end_of_stream the
  whole body is tokenized and sent back as StreamedBodyResponse messages (in
  slices of at most ``chunk_bytes``), the last with end_of_stream=true.
  Bodies of at least ``offload_bytes`` are tokenized in a spawn-based process
  pool so a multi-MB scan does not stall every other stream on the event loop.
- Request body over ``max_body_bytes``: the leg switches to pass-through (the
  buffered bytes, then every later chunk as it arrives) so memory stays
  bounded; outcome ``oversize``. Like a non-JSON body, it goes upstream
  unredacted.
- Response body: every chunk is answered with the restored bytes so far; the
  end_of_stream chunk also carries the restorer's held tail. JSON responses
  are held whole until end_of_stream (see standin.json_restorer). The final
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

Logs carry counts only, never values, placeholders or hashes. At DEBUG, error
logs add the traceback (frames and exception messages, never local values).
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import signal
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
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
from standin.json_restorer import JsonRestorer
from standin.sse import SseRestorer
from standin.tokenizer import Restorer, Tokenizer, TokenizeResult

log = logging.getLogger("standin.server")

MODES = ("shadow", "enforce")
DEFAULT_CHUNK_BYTES = 1 << 20
DEFAULT_MAX_BODY_BYTES = 32 << 20
DEFAULT_OFFLOAD_BYTES = 256 << 10
METADATA_KEY = "standin"
# Upstream must answer uncompressed: the restorer works on plain bytes, and
# shadow strips it too so both modes measure the same wire. A compressed
# response that still arrives is counted as errors_total{phase="response_encoded"}.
STRIP_REQUEST_HEADERS = ["accept-encoding"]


def _debug() -> bool:
    return log.isEnabledFor(logging.DEBUG)


# Workers are spawned, not forked: gRPC's core does not survive a fork once
# initialised, and spawn re-imports the package instead of copying the parent.

_WORKER_TOKENIZER: Tokenizer | None = None


def _init_worker(salt: str | None, cache_entries: int | None) -> None:
    global _WORKER_TOKENIZER
    # Ctrl-C reaches the whole process group; the parent shuts the pool down.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _WORKER_TOKENIZER = Tokenizer(salt=salt, cache_entries=cache_entries)


def _tokenize_worker(body: bytes) -> tuple[TokenizeResult | None, int]:
    tok = _WORKER_TOKENIZER
    if tok is None:
        raise RuntimeError("tokenize worker not initialised")
    before = tok.cache_hits
    result = tok.tokenize_json(body)
    return result, tok.cache_hits - before


def make_tokenize_pool(
    workers: int, *, salt: str | None = None, cache_entries: int | None = None
) -> ProcessPoolExecutor:
    """A spawn-context pool whose workers each build their own Tokenizer
    (``salt``/``cache_entries`` None = read from the environment, as in the parent)."""
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_init_worker,
        initargs=(salt, cache_entries),
    )


def start_tokenize_pool(
    workers: int,
    *,
    salt: str | None = None,
    cache_entries: int | None = None,
    timeout: float = 60.0,
) -> ProcessPoolExecutor | None:
    """Create the pool and start every worker now (not on the first large
    body). Returns None, after logging, if the workers cannot start; the
    processor then tokenizes inline."""
    pool = make_tokenize_pool(workers, salt=salt, cache_entries=cache_entries)
    try:
        for future in [pool.submit(_tokenize_worker, b"{}") for _ in range(workers)]:
            future.result(timeout=timeout)
    except Exception as exc:
        log.error(
            "tokenize workers failed to start; tokenizing inline",
            extra={"error": type(exc).__name__, "workers": workers},
            exc_info=_debug(),
        )
        pool.shutdown(wait=False, cancel_futures=True)
        return None
    return pool


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
    def __init__(self, processor: ExternalProcessor) -> None:
        self.processor = processor
        self.mode = processor.mode
        self.chunk_bytes = processor.chunk_bytes
        self.max_body_bytes = processor.max_body_bytes
        self.request_chunks: list[bytes] = []
        self.request_bytes = 0
        self.request_answered = False
        self.oversize = False
        self.result: TokenizeResult | None = None
        self.restorer: _Stream = _Passthrough()
        self.outcome = "incomplete"
        self.restored_reported = 0
        self.defer_request_headers = processor.defer_request_headers
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

    async def handle(self, req: pb.ProcessingRequest) -> list[pb.ProcessingResponse]:
        phase = req.WhichOneof("request")
        if phase == "request_body":
            return await self._request_body(req.request_body)
        return list(self._handle_sync(req, phase))

    def _handle_sync(
        self, req: pb.ProcessingRequest, phase: str | None
    ) -> Iterator[pb.ProcessingResponse]:
        if phase == "request_headers":
            if not req.request_headers.end_of_stream and self.defer_request_headers:
                self.headers_pending = True
                return
            if req.request_headers.end_of_stream:
                self.request_answered = True
                self.outcome = "no_body"
            yield self._request_headers_response()
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

    async def _request_body(self, body: pb.HttpBody) -> list[pb.ProcessingResponse]:
        if self.oversize:
            return [_streamed_one(body.body, body.end_of_stream, request=True)]
        if self.request_answered:
            raise _Abort("request body after the request leg was answered")
        if body.body:
            self.request_chunks.append(body.body)
            self.request_bytes += len(body.body)
        if self.max_body_bytes and self.request_bytes > self.max_body_bytes:
            return self._overflow(body.end_of_stream)
        if not body.end_of_stream:
            return []
        original = b"".join(self.request_chunks)
        self.request_chunks = []
        outgoing = original
        try:
            outgoing = await self._tokenize(original)
        except Exception as exc:
            metrics.ERRORS.labels(phase="request_body").inc()
            log.error(
                "tokenize failed; request passed through",
                extra={"error": type(exc).__name__, "bytes": len(original)},
                exc_info=_debug(),
            )
            self.outcome = "error"
            self.result = None
        self.request_answered = True
        out: list[pb.ProcessingResponse] = []
        if self.headers_pending:
            self.headers_pending = False
            out.append(self._request_headers_response())
        out.extend(_streamed(outgoing, self.chunk_bytes, request=True))
        return out

    def _overflow(self, end_of_stream: bool) -> list[pb.ProcessingResponse]:
        """Over the cap: release what is buffered and pass the rest of the leg
        through as it arrives (unredacted, like a non-JSON body)."""
        buffered = b"".join(self.request_chunks)
        self.request_chunks = []
        self.oversize = True
        self.request_answered = True
        self.outcome = "oversize"
        self.result = None
        log.warning(
            "request body over STANDIN_MAX_BODY_BYTES; passed through",
            extra={"bytes": self.request_bytes, "limit": self.max_body_bytes},
        )
        out: list[pb.ProcessingResponse] = []
        if self.headers_pending:
            self.headers_pending = False
            out.append(self._request_headers_response())
        out.extend(_streamed(buffered, self.chunk_bytes, request=True, end_of_stream=end_of_stream))
        return out

    def _request_headers_response(self) -> pb.ProcessingResponse:
        return pb.ProcessingResponse(
            request_headers=pb.HeadersResponse(
                response=pb.CommonResponse(
                    header_mutation=pb.HeaderMutation(remove_headers=STRIP_REQUEST_HEADERS)
                )
            ),
            dynamic_metadata=self.metadata(),
        )

    async def _tokenize(self, body: bytes) -> bytes:
        metrics.BODY_BYTES.observe(len(body))
        started = time.perf_counter()
        result, cache_hits = await self.processor.tokenize(body)
        metrics.TOKENIZE_SECONDS.observe(time.perf_counter() - started)
        if cache_hits:
            metrics.CACHE_HITS.inc(cache_hits)
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
        content_type = values.get("content-type", "")
        if "text/event-stream" in content_type:
            self.restorer = SseRestorer(self.result.mapping)
        elif _is_json(content_type):
            self.restorer = JsonRestorer(self.result.mapping)
        else:
            self.restorer = Restorer(self.result.mapping)

    def _response_body(self, body: pb.HttpBody) -> Iterator[pb.ProcessingResponse]:
        errors_before = self.restorer.errors
        out = self.restorer.feed(body.body)
        if body.end_of_stream:
            out += self.restorer.flush()
        if self.restorer.errors != errors_before:
            metrics.ERRORS.labels(phase="response_event").inc(self.restorer.errors - errors_before)
        # JsonRestorer holds the whole body until end_of_stream, so for JSON
        # responses this observes the body size so far, not a placeholder tail.
        metrics.HELD_BYTES.observe(self.restorer.held)
        self._report_restored()
        # A buffered JSON body is released at once; slice it like the request leg.
        yield from _streamed(out, self.chunk_bytes, request=False, end_of_stream=body.end_of_stream)

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


def _is_json(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip()
    return media == "application/json" or media.endswith("+json")


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


def _streamed(
    data: bytes, chunk_bytes: int, *, request: bool, end_of_stream: bool = True
) -> Iterator[pb.ProcessingResponse]:
    if len(data) <= chunk_bytes:
        yield _streamed_one(data, end_of_stream, request=request)
        return
    for start in range(0, len(data), chunk_bytes):
        piece = data[start : start + chunk_bytes]
        last = start + chunk_bytes >= len(data)
        yield _streamed_one(piece, end_of_stream and last, request=request)


class ExternalProcessor(pb_grpc.ExternalProcessorServicer):
    def __init__(
        self,
        tokenizer: Tokenizer,
        mode: str = "shadow",
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        defer_request_headers: bool = True,
        *,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        pool: Executor | None = None,
        offload_bytes: int = DEFAULT_OFFLOAD_BYTES,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"STANDIN_MODE must be one of {MODES}")
        self.tokenizer = tokenizer
        self.mode = mode
        self.chunk_bytes = chunk_bytes
        self.defer_request_headers = defer_request_headers
        self.max_body_bytes = max_body_bytes
        # Bodies of at least offload_bytes go to the pool. Its workers build
        # their own Tokenizer, which must use the same salt and rules as
        # ``tokenizer`` (make_tokenize_pool / start_tokenize_pool).
        self.pool = pool
        self.offload_bytes = offload_bytes

    async def tokenize(self, body: bytes) -> tuple[TokenizeResult | None, int]:
        """``tokenize_json(body)`` and the scan-cache hit delta; off the event
        loop when a pool is set and the body is large enough."""
        pool = self.pool
        if pool is not None and len(body) >= self.offload_bytes:
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(pool, _tokenize_worker, body)
            except BrokenProcessPool as exc:
                if self.pool is pool:
                    self.pool = None
                    metrics.ERRORS.labels(phase="tokenize_pool").inc()
                    log.error(
                        "tokenize worker pool broken; tokenizing inline from now on",
                        extra={"error": type(exc).__name__, "bytes": len(body)},
                        exc_info=_debug(),
                    )
                    pool.shutdown(wait=False, cancel_futures=True)
        before = self.tokenizer.cache_hits
        result = self.tokenizer.tokenize_json(body)
        return result, self.tokenizer.cache_hits - before

    async def Process(
        self,
        request_iterator: AsyncIterator[pb.ProcessingRequest],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb.ProcessingResponse]:
        metrics.ACTIVE.inc()
        ex = _Exchange(self)
        try:
            async for req in request_iterator:
                phase = req.WhichOneof("request") or "unknown"
                try:
                    responses = await ex.handle(req)
                except _Abort:
                    metrics.ERRORS.labels(phase=phase).inc()
                    ex.outcome = "error"
                    return
                except Exception as exc:
                    metrics.ERRORS.labels(phase=phase).inc()
                    log.error(
                        "ext_proc phase failed; passing through",
                        extra={"phase": phase, "error": type(exc).__name__},
                        exc_info=_debug(),
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
            metrics.ACTIVE.dec()
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
