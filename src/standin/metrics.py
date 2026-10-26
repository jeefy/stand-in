"""Prometheus metrics. Labels carry tiers, rule indexes, phases and outcomes
only; never a value, a prefix or a placeholder."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

REQUESTS = Counter(
    "standin_requests",
    "ext_proc exchanges by mode and outcome",
    ["mode", "outcome"],
)
MATCHES = Counter(
    "standin_matches",
    "Rule matches in request bodies (patterns.yaml tier and index)",
    ["tier", "pattern_index"],
)
RESTORED = Counter(
    "standin_restored",
    "Placeholders restored to their original value in responses",
)
ERRORS = Counter(
    "standin_errors",
    "Exceptions handled by passing the exchange through unchanged",
    ["phase"],
)
HELD_BYTES = Histogram(
    "standin_held_bytes",
    "Bytes held back by the response restorer after each response chunk",
    buckets=(0, 1, 2, 4, 8, 16, 22, 23, 64, 256, 1024, 4096, 16384),
)
BODY_BYTES = Histogram(
    "standin_body_bytes",
    "Request body size",
    buckets=tuple(1024 * 4**i for i in range(10)),
)
TOKENIZE_SECONDS = Histogram(
    "standin_tokenize_seconds",
    "Time spent tokenizing one request body",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
