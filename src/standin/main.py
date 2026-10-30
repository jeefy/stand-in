"""stand-in entrypoint: gRPC ext_proc on :9101, Prometheus on :9102.

Environment:
  STANDIN_MODE                   shadow (default) | enforce
  STANDIN_SALT                   placeholder hash key; must be identical on
                                 every replica. Required in enforce (exit 2)
                                 unless STANDIN_ALLOW_DEFAULT_SALT=true
  STANDIN_ALLOW_DEFAULT_SALT     false; let enforce run on the built-in salt
  STANDIN_GRPC_PORT              9101
  STANDIN_METRICS_PORT           9102
  STANDIN_CHUNK_BYTES            max bytes per StreamedBodyResponse (1 MiB)
  STANDIN_MAX_BODY_BYTES         request bodies above this pass through
                                 unredacted (32 MiB; 0 = unlimited)
  STANDIN_CACHE_ENTRIES          per-literal scan cache size (50000; 0 disables)
  STANDIN_TOKENIZE_WORKERS       tokenize processes for large bodies (1; 0 = inline)
  STANDIN_TOKENIZE_OFFLOAD_BYTES bodies at least this big use a worker (262144)
  STANDIN_MAX_CONCURRENT_STREAMS gRPC concurrent-RPC cap (0 = unlimited)
  STANDIN_DRAIN_SECONDS          after SIGTERM: health NOT_SERVING, keep serving (10)
  STANDIN_SHUTDOWN_GRACE         then seconds in-flight streams get to finish (600)
  STANDIN_DEFER_REQUEST_HEADERS  true; answer request_headers after the body so
                                 dynamic_metadata carries the counts
  STANDIN_PATTERNS_FILE          override patterns.yaml
  STANDIN_COUNT_NEW              false; also count Tier A-D matches in the
                                 newest turn of each request (metrics and
                                 metadata only; the body path is unchanged)
  LOG_LEVEL                      INFO; DEBUG adds tracebacks to error logs
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from collections.abc import Mapping
from concurrent.futures import Executor
from dataclasses import dataclass

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from prometheus_client import start_http_server

from standin._proto.envoy.service.ext_proc.v3 import (
    external_processor_pb2_grpc as pb_grpc,
)
from standin.patterns import load_count_rules
from standin.server import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_OFFLOAD_BYTES,
    MODES,
    ExternalProcessor,
    start_tokenize_pool,
)
from standin.tokenizer import SALT_ENV, Tokenizer

log = logging.getLogger("standin")

SERVICE_NAME = "envoy.service.ext_proc.v3.ExternalProcessor"
_STD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_MAX_MESSAGE = 64 * 1024 * 1024
_FALSE = ("0", "false", "no", "off")
_TRUE = ("1", "true", "yes", "on")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


@dataclass(frozen=True)
class Settings:
    mode: str = "shadow"
    salt: str | None = None
    grpc_port: int = 9101
    metrics_port: int = 9102
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    tokenize_workers: int = 1
    offload_bytes: int = DEFAULT_OFFLOAD_BYTES
    max_concurrent_streams: int | None = None
    drain: float = 10.0
    grace: float = 600.0
    defer_request_headers: bool = True
    count_new: bool = False


def _fail(message: str, **extra: object) -> SystemExit:
    log.error(message, extra=extra)
    return SystemExit(2)


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _fail("invalid integer setting", setting=name) from None
    if value < 0:
        raise _fail("setting must not be negative", setting=name)
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise _fail("invalid number setting", setting=name) from None
    if value < 0:
        raise _fail("setting must not be negative", setting=name)
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise _fail("invalid boolean setting", setting=name)


def _settings_from_env(env: Mapping[str, str]) -> Settings:
    """Parse and validate the environment; SystemExit(2) on a bad setting."""
    mode = env.get("STANDIN_MODE", "shadow").strip().lower()
    if mode not in MODES:
        raise _fail("invalid STANDIN_MODE", allowed=list(MODES))
    salt = env.get(SALT_ENV) or None
    if salt is None:
        if mode == "enforce" and not _bool(env, "STANDIN_ALLOW_DEFAULT_SALT", False):
            raise _fail(
                "STANDIN_SALT unset or empty in enforce mode; set it from a Secret "
                "(or STANDIN_ALLOW_DEFAULT_SALT=true for a lab)"
            )
        log.warning("STANDIN_SALT unset or empty; using the built-in default salt")
    return Settings(
        mode=mode,
        salt=salt,
        grpc_port=_int(env, "STANDIN_GRPC_PORT", 9101),
        metrics_port=_int(env, "STANDIN_METRICS_PORT", 9102),
        chunk_bytes=_int(env, "STANDIN_CHUNK_BYTES", DEFAULT_CHUNK_BYTES) or DEFAULT_CHUNK_BYTES,
        max_body_bytes=_int(env, "STANDIN_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES),
        tokenize_workers=_int(env, "STANDIN_TOKENIZE_WORKERS", 1),
        offload_bytes=_int(env, "STANDIN_TOKENIZE_OFFLOAD_BYTES", DEFAULT_OFFLOAD_BYTES),
        max_concurrent_streams=_int(env, "STANDIN_MAX_CONCURRENT_STREAMS", 0) or None,
        drain=_float(env, "STANDIN_DRAIN_SECONDS", 10.0),
        grace=_float(env, "STANDIN_SHUTDOWN_GRACE", 600.0),
        defer_request_headers=_bool(env, "STANDIN_DEFER_REQUEST_HEADERS", True),
        count_new=_bool(env, "STANDIN_COUNT_NEW", False),
    )


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


async def serve(
    processor: ExternalProcessor,
    grpc_port: int,
    stop: asyncio.Event,
    grace: float,
    *,
    drain: float = 0.0,
    pool: Executor | None = None,
    max_concurrent_streams: int | None = None,
) -> None:
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", _MAX_MESSAGE),
            ("grpc.max_send_message_length", _MAX_MESSAGE),
            # agentgateway's client (tonic/hyper) keeps its h2 connection alive
            # with pings, also while no stream is open. gRPC's default server
            # policy counts those as strikes and answers with GOAWAY
            # (too_many_pings), which drops in-flight exchanges on reconnect.
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.min_ping_interval_without_data_ms", 10000),
            ("grpc.http2.max_ping_strikes", 0),
            # Our own pings find dead peers (half-open connections after a
            # gateway node dies) instead of holding their streams forever.
            ("grpc.keepalive_time_ms", 60000),
            ("grpc.keepalive_timeout_ms", 20000),
        ],
        maximum_concurrent_rpcs=max_concurrent_streams,
    )
    pb_grpc.add_ExternalProcessorServicer_to_server(processor, server)
    health_servicer = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
    for service in (health.OVERALL_HEALTH, SERVICE_NAME):
        await health_servicer.set(service, health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    server.add_insecure_port(f"[::]:{grpc_port}")
    await server.start()
    log.info("serving", extra={"grpc_port": grpc_port, "mode": processor.mode})
    try:
        await stop.wait()
        # Readiness fails and the endpoint is withdrawn while we still accept
        # new streams, so the gateway's reconnect after our GOAWAY lands on
        # another replica instead of this one.
        await health_servicer.enter_graceful_shutdown()
        log.info("draining", extra={"drain_seconds": drain})
        if drain > 0:
            await asyncio.sleep(drain)
        log.info("shutting down", extra={"grace_seconds": grace})
        await server.stop(grace)
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)


def main() -> None:
    _configure_logging()
    settings = _settings_from_env(os.environ)
    # Load (and validate) every rule before any worker starts.
    tokenizer = Tokenizer(
        salt=settings.salt,
        count_rules=load_count_rules() if settings.count_new else None,
    )
    pool = None
    if settings.tokenize_workers > 0:
        pool = start_tokenize_pool(
            settings.tokenize_workers, salt=settings.salt, count_new=settings.count_new
        )
    processor = ExternalProcessor(
        tokenizer,
        mode=settings.mode,
        chunk_bytes=settings.chunk_bytes,
        defer_request_headers=settings.defer_request_headers,
        max_body_bytes=settings.max_body_bytes,
        pool=pool,
        offload_bytes=settings.offload_bytes,
        count_new=settings.count_new,
    )
    start_http_server(settings.metrics_port)

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await serve(
            processor,
            settings.grpc_port,
            stop,
            settings.grace,
            drain=settings.drain,
            pool=pool,
            max_concurrent_streams=settings.max_concurrent_streams,
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
