"""stand-in entrypoint: gRPC ext_proc on :9101, Prometheus on :9102.

Environment:
  STANDIN_MODE                   shadow (default) | enforce
  STANDIN_SALT                   placeholder hash key; must be identical on
                                 every replica (set it from a Secret before
                                 enforce)
  STANDIN_GRPC_PORT              9101
  STANDIN_METRICS_PORT           9102
  STANDIN_CHUNK_BYTES            max bytes per StreamedBodyResponse (1 MiB)
  STANDIN_CACHE_ENTRIES          per-literal scan cache size (50000; 0 disables)
  STANDIN_SHUTDOWN_GRACE         seconds in-flight streams get on SIGTERM (600)
  STANDIN_DEFER_REQUEST_HEADERS  true; answer request_headers after the body so
                                 dynamic_metadata carries the counts
  STANDIN_PATTERNS_FILE          override patterns.yaml
  LOG_LEVEL                      INFO
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from prometheus_client import start_http_server

from standin._proto.envoy.service.ext_proc.v3 import (
    external_processor_pb2_grpc as pb_grpc,
)
from standin.server import DEFAULT_CHUNK_BYTES, ExternalProcessor
from standin.tokenizer import SALT_ENV, Tokenizer

log = logging.getLogger("standin")

SERVICE_NAME = "envoy.service.ext_proc.v3.ExternalProcessor"
_STD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_MAX_MESSAGE = 64 * 1024 * 1024


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS})
        return json.dumps(entry, default=str)


class HealthServicer(health_pb2_grpc.HealthServicer):
    def __init__(self) -> None:
        self.status = health_pb2.HealthCheckResponse.SERVING

    async def Check(
        self, request: health_pb2.HealthCheckRequest, context: grpc.aio.ServicerContext
    ) -> health_pb2.HealthCheckResponse:
        if request.service not in ("", SERVICE_NAME):
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown service")
        return health_pb2.HealthCheckResponse(status=self.status)


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
) -> None:
    server = grpc.aio.server(
        options=[
            ("grpc.max_receive_message_length", _MAX_MESSAGE),
            ("grpc.max_send_message_length", _MAX_MESSAGE),
        ]
    )
    pb_grpc.add_ExternalProcessorServicer_to_server(processor, server)
    health_servicer = HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    server.add_insecure_port(f"[::]:{grpc_port}")
    await server.start()
    log.info("serving", extra={"grpc_port": grpc_port, "mode": processor.mode})
    await stop.wait()
    log.info("shutting down", extra={"grace_seconds": grace})
    health_servicer.status = health_pb2.HealthCheckResponse.NOT_SERVING
    await server.stop(grace)


def main() -> None:
    _configure_logging()
    mode = os.environ.get("STANDIN_MODE", "shadow").strip().lower()
    if SALT_ENV not in os.environ:
        log.warning("STANDIN_SALT unset; using the built-in default salt")
    processor = ExternalProcessor(
        Tokenizer(),
        mode=mode,
        chunk_bytes=int(os.environ.get("STANDIN_CHUNK_BYTES", DEFAULT_CHUNK_BYTES)),
        defer_request_headers=os.environ.get("STANDIN_DEFER_REQUEST_HEADERS", "true").lower()
        not in ("0", "false", "no"),
    )
    start_http_server(int(os.environ.get("STANDIN_METRICS_PORT", "9102")))

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await serve(
            processor,
            int(os.environ.get("STANDIN_GRPC_PORT", "9101")),
            stop,
            float(os.environ.get("STANDIN_SHUTDOWN_GRACE", "600")),
        )

    asyncio.run(run())


if __name__ == "__main__":
    main()
