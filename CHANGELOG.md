# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

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
