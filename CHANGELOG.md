# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Tier B rule 5: API-key request headers (`x-api-key`, `x-plex-token`,
  `x-nsone-key`, `x-gitlab-token`, `private-token`, `x-vault-token`), value
  only, 16+ chars. Hex values are not rejected: the header name is the signal.

## [0.2.0] - 2026-10-28

### Added

- Request body cap: bodies over `STANDIN_MAX_BODY_BYTES` (32 MiB default)
  pass through unredacted, counted as `outcome="oversize"`.
- Worker-pool tokenizing: bodies of `STANDIN_TOKENIZE_OFFLOAD_BYTES` or more
  are scanned in a spawned worker process (`STANDIN_TOKENIZE_WORKERS`).
- Drain-before-stop on SIGTERM (`STANDIN_DRAIN_SECONDS`): gRPC health goes
  `NOT_SERVING` via the standard grpc health servicer while new streams are
  still accepted, then GOAWAY and `STANDIN_SHUTDOWN_GRACE`.
- gRPC keepalive options matching agentgateway's ping behaviour, and
  `STANDIN_MAX_CONCURRENT_STREAMS` cap on concurrent gRPC calls.
- `STANDIN_ALLOW_DEFAULT_SALT`; enforce without `STANDIN_SALT` now fails fast
  (exit 2).
- Metrics: `standin_active_streams`, `standin_cache_hits_total`, new
  `outcome="oversize"`, new phases `tokenize_pool` and `response_abandon`.
- Structured JSON restorer for non-streamed `application/json` responses:
  buffered to end of stream, restored per string with correct escaping.
- Ten new Tier A rules (AWS STS, Stripe webhook secrets, Slack app-level,
  HashiCorp Vault, GitLab runner/CI, npm legacy `_authToken`, Databricks,
  DigitalOcean, Supabase, PlanetScale) and `oat01` in the Anthropic rule
  (39 Tier A rules total).
- Derived prefilters: literal prefixes are extracted from each pattern
  automatically; explicit `required`/`lead` only needed when a pattern does
  not start with literal text (`required: []` disables).
- Pattern modifiers: `reject` (regexes fullmatched against the would-be
  value), `key_boundary`, and `(?P<value>...)` support for `value_only`.
- CI: pyright, coverage, Python 3.13 matrix, pinned actions, SBOM/provenance
  on image builds, PyPI trusted publishing, dependabot, multi-stage
  digest-pinned image, e2e twin job.
- `scripts/twin.sh` docker support via `CONTAINER_RUNTIME`.

### Changed

- Empty `STANDIN_SALT` now means the built-in default salt (same as unset);
  enforce refuses it.
- Invalid settings (bad integer, float, boolean, or mode) exit with code 2 at
  startup.
- Kubernetes example uses grpc readiness/liveness probes (no preStop hook),
  768Mi memory request, and `terminationGracePeriodSeconds: 640`
  (10 drain + 600 grace + 30 slack).

### Fixed

- Tool-call `arguments` in non-streamed JSON responses get placeholder values
  escaped the right number of times (JSON inside a JSON string), instead of
  one level short.

## [0.1.0] - 2026-10-25

First standalone release, extracted from the private homelab repository where
it ran in front of GitHub Copilot via agentgateway v1.6.0 (shadow, then
enforce).

### Added

- Envoy ext_proc `ExternalProcessor` (grpc.aio) for agentgateway, with
  `fullDuplexStreamed` request and response legs.
- Request tokenizer: Tier A (29 vendor-prefixed token patterns) and Tier B
  (5 key-material patterns) from `patterns.yaml`, matched per decoded JSON
  string literal; value-only replacement for key=value rules, PEM blocks as
  one span, base64 lines only inside PEM or under `data:` keys.
- Deterministic placeholders `<<secret-` + 12 hex + `>>` (salted BLAKE2b),
  stable across turns so upstream prompt caches keep hitting.
- Response restorers: SSE-aware (OpenAI chat completions, Anthropic messages,
  OpenAI Responses delta channels, including tool-call arguments) and a
  byte-level restorer for plain JSON, holding at most 22 bytes.
- `shadow` and `enforce` modes, per-request counts as ext_proc dynamic
  metadata (`standin`), Prometheus metrics (`standin_*`, counts only), gRPC
  health, graceful SIGTERM drain.
- Container image (non-root, read-only root filesystem), Kubernetes and
  agentgateway examples, `scripts/twin.sh` local end-to-end test.

### Changed (relative to the in-tree version)

- Renamed from the in-tree names: Python package (now `standin`),
  environment variables (now `STANDIN_*`), metric names (now `standin_*`) and
  the ext_proc dynamic-metadata key (now `standin`). The placeholder format is
  unchanged.
- The built-in default salt changed, so placeholders produced without
  `STANDIN_SALT` differ from the in-tree version. Placeholders produced with an
  explicit salt are unchanged.
