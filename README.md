# stand-in

Reversible secret redaction for LLM traffic, as an Envoy
[ext_proc](https://www.envoyproxy.io/docs/envoy/latest/api-v3/service/ext_proc/v3/external_processor.proto)
processor for [agentgateway](https://github.com/agentgateway/agentgateway).

Coding agents leak credentials: tool output from `cat .env`, `grep`, or a
kubeconfig dump goes to the model provider verbatim. A gateway `mask` is
one-way (the agent can no longer use the value) and a no-op on streaming
responses. stand-in replaces credential-shaped strings with deterministic
placeholders like `<<secret-xxxxxxxxxxxx>>` on the way out and restores them in
the response, in assistant text and tool-call arguments. The model never holds
the secret; the agent still runs `tailscale up --authkey=<real value>`.

Extracted from a homelab setup that fronts GitHub Copilot with agentgateway
standalone, where it ran in shadow and then enforce on every agent request.

## How it works

- **Placeholder**: `<<secret-` + 12 hex + `>>`, the first 6 bytes of
  `blake2b(value, key=STANDIN_SALT)`. Deterministic per salt, so the provider's
  prompt cache keeps hitting across turns. The map lives in memory for one
  ext_proc stream (one HTTP exchange) and is rebuilt every turn.
- **Request leg** (`fullDuplexStreamed`): accumulate to `end_of_stream`, parse
  as JSON (else pass through), scan the decoded content of every string
  literal, re-encode only changed literals, answer in `StreamedBodyResponse`
  chunks of `STANDIN_CHUNK_BYTES`.
- **Response leg** (`fullDuplexStreamed`): for SSE, each delta channel is
  restored independently: OpenAI chat (`content`, `reasoning_content`,
  `refusal`, `tool_calls[].function.arguments`), Anthropic
  (`text`, `partial_json`, `thinking`), Responses API (`*.delta`). A partial
  placeholder is held per channel and flushed before any terminal event.
  `application/json` (and `+json`) bodies are held whole until
  `end_of_stream`, parsed, and restored per string: tool-call `arguments`
  (JSON inside a JSON string) get the value escaped for both levels, other
  strings once (Anthropic `tool_use.input` is always plain text); an untouched
  body is forwarded byte for byte. A body that does not parse, or one over
  64 MiB, falls back to the byte-level restorer, which other content types use
  too and which holds at most 22 bytes. Unknown placeholders pass through.
- **Modes**: `shadow` counts and emits metadata but forwards the original body;
  `enforce` rewrites and restores. Both strip `accept-encoding` upstream.
- **Failure**: never a gRPC error on the stream. Tokenize errors forward the
  original body; response errors release held bytes and pass the rest through;
  as a last resort the stream is closed cleanly, which agentgateway treats as
  pass-through. `failOpen` covers only unreachability before body bytes arrive.

## Why ext_proc and not promptGuard

Regex `mask` is a no-op on streaming responses
([#2956](https://github.com/agentgateway/agentgateway/issues/2956)); webhook
guards have no request-to-response state and `mask` drops `tool_calls`
([#3331](https://github.com/agentgateway/agentgateway/issues/3331)); there is
no plugin system ([#1665](https://github.com/agentgateway/agentgateway/issues/1665));
CEL transformations only see buffered bodies. ext_proc is one bidirectional
gRPC stream per exchange with streaming body modes on both legs, exactly the
lifetime a placeholder map needs. Keep the gateway guardrails as an audit in
shadow and a `mask` backstop in enforce.

## agentgateway gotchas (v1.6.0)

- `$NAME` is substituted across the whole config file, comments included; write
  a literal dollar as `\x24`.
- Only the request-headers response's `dynamic_metadata` is kept, so stand-in
  answers request headers after tokenizing (`STANDIN_DEFER_REQUEST_HEADERS`).
- `requestBodyMode: buffered` is capped at 2 MiB regardless of
  `maxBufferSize`; use `fullDuplexStreamed`.
- `extProc.policies` is an object, not a list.
- `failOpen` stops applying once body bytes reach the processor; the processor
  must degrade to pass-through itself.
- Strip `accept-encoding` or the provider may gzip the stream.

## Patterns

[`patterns.yaml`](patterns.yaml) carries two tiers, kept identical to the
matching gateway guardrail blocks so shadow counts compare rule for rule:

| Tier | Contents |
| --- | --- |
| A | 29 vendor-prefixed tokens (GitHub, OpenAI, Anthropic, AWS, JWT, Slack, GitLab, Tailscale, Google, Groq, Hugging Face, npm, PyPI, Docker, Atlassian, Telegram, webhooks, Cloudflare, age, argon2id, Stripe, SendGrid, Tavily, LangSmith, Bitwarden) |
| B | 5 key-material rules: PEM private keys, base64 key blocks, kubeconfig/Secret `*-data` fields, service-account JSON |

Contextual `key=value` and generic `password=` rules are too noisy to rewrite
and stay audit-only in the gateway. Handling modifiers: `value_only` replaces
the value after the first `:`/`=`; `pem_span` makes a `BEGIN...END PRIVATE KEY`
block one placeholder; `data_block_line` matches a bare base64 line only inside
a PEM span or under a `data:`/`*-data:`/`binaryData:` key. Overlaps merge.

To extend, append under `tier_a`/`tier_b` (`pattern` is Python `re`; stay in
the Rust-`regex` subset if you mirror it in the gateway), optionally `handling`
and `required`/`lead` prefilter hints, and add synthetic positive and negative
fixtures to `tests/standin_fixtures.py`. `STANDIN_PATTERNS_FILE` overrides the
file at runtime.

## Deploy

[`examples/kubernetes.yaml`](examples/kubernetes.yaml): Deployment x2, Service,
PDB, NetworkPolicy. Image `ghcr.io/jeefy/stand-in:<version>`; gRPC on 9101,
metrics on 9102. Keep `terminationGracePeriodSeconds` above
`STANDIN_DRAIN_SECONDS + STANDIN_SHUTDOWN_GRACE`; long agent turns ride one
stream for minutes.

[`examples/agentgateway-route.yaml`](examples/agentgateway-route.yaml): the
route policy and access-log attributes.

```yaml
policies:
  extProc:
    host: "stand-in.<namespace>.svc.cluster.local:9101"
    failureMode: failOpen
    processingOptions:
      requestHeaderMode: send
      responseHeaderMode: send
      requestBodyMode: fullDuplexStreamed
      responseBodyMode: fullDuplexStreamed
      requestTrailerMode: skip
      responseTrailerMode: skip
    policies:
      http: {requestTimeout: 5s}
```

Rollout: deploy in `shadow` and confirm `standin_errors_total` stays 0 and
`standin.*` access-log counts match the gateway's Tier A/B audit; create the
salt Secret and switch to `enforce`; verify with a synthetic credential from a
disposable agent session; then set the gateway's Tier A guardrail to `mask` as
a backstop. Roll back by flipping `STANDIN_MODE`. In `enforce` stand-in exits
(code 2) without `STANDIN_SALT` unless `STANDIN_ALLOW_DEFAULT_SALT=true`; the
built-in salt is public, so its placeholders are predictable.

Rollouts: agentgateway keeps one HTTP/2 connection per processor, which a
ClusterIP Service pins to one pod. On SIGTERM stand-in reports gRPC health
`NOT_SERVING` and keeps serving for `STANDIN_DRAIN_SECONDS` while the endpoint
is withdrawn, then sends GOAWAY and gives in-flight streams
`STANDIN_SHUTDOWN_GRACE`; the gateway reconnects to another replica. Use the
`grpc` readiness/liveness probes from the example (no `preStop` hook needed).
A stream that still lands on a stopping replica, or any other unreachable
moment, falls under `failureMode`: with `failOpen` that exchange goes upstream
unredacted, so switch to `failClosed` once in `enforce`.

Request bodies over `STANDIN_MAX_BODY_BYTES` are not buffered: the leg switches
to pass-through and the body goes upstream unredacted, like a non-JSON body,
counted as `outcome="oversize"`. Bodies of `STANDIN_TOKENIZE_OFFLOAD_BYTES` or
more are tokenized in a spawned worker process so a multi-MB scan does not stall
other streams; each worker keeps its own scan cache, so the cache-hit metric
counts both. Workers need a writable `/dev/shm` (the container default).

| Variable | Default | Meaning |
| --- | --- | --- |
| `STANDIN_MODE` | `shadow` | `shadow` or `enforce` |
| `STANDIN_SALT` | built-in | Placeholder hash key; identical on every replica, keep secret; required in `enforce` |
| `STANDIN_ALLOW_DEFAULT_SALT` | `false` | Let `enforce` start on the built-in salt (labs only) |
| `STANDIN_GRPC_PORT` | `9101` | ext_proc and gRPC health |
| `STANDIN_METRICS_PORT` | `9102` | Prometheus `/metrics` |
| `STANDIN_CHUNK_BYTES` | `1048576` | Max bytes per request-leg `StreamedBodyResponse` |
| `STANDIN_MAX_BODY_BYTES` | `33554432` | Request bodies above this pass through unredacted (`oversize`); `0` = unlimited |
| `STANDIN_CACHE_ENTRIES` | `50000` | Per-literal scan cache (per process); `0` disables |
| `STANDIN_TOKENIZE_WORKERS` | `1` | Worker processes for large bodies; `0` tokenizes on the event loop |
| `STANDIN_TOKENIZE_OFFLOAD_BYTES` | `262144` | Bodies at least this big go to a worker |
| `STANDIN_MAX_CONCURRENT_STREAMS` | `0` | Cap on concurrent gRPC calls, health checks included; over it calls fail before any body (`failureMode` applies); `0` = unlimited |
| `STANDIN_DRAIN_SECONDS` | `10` | After SIGTERM: health `NOT_SERVING` while still serving |
| `STANDIN_SHUTDOWN_GRACE` | `600` | Then seconds in-flight streams get to finish |
| `STANDIN_DEFER_REQUEST_HEADERS` | `true` | Answer request headers after the body |
| `STANDIN_PATTERNS_FILE` | packaged | Alternative pattern file |
| `LOG_LEVEL` | `INFO` | JSON lines on stdout; `DEBUG` adds tracebacks to error lines |

| Metric | Labels | Meaning |
| --- | --- | --- |
| `standin_requests_total` | `mode`, `outcome` | `tokenized`, `clean`, `skipped_non_json`, `oversize`, `no_body`, `error`, `incomplete` |
| `standin_matches_total` | `tier`, `pattern_index` | Rule matches in request bodies |
| `standin_restored_total` | | Placeholders restored in responses |
| `standin_errors_total` | `phase` | Exceptions handled by pass-through; `response_encoded` = compressed response skipped; `tokenize_pool` = worker pool broke, inline from then on |
| `standin_held_bytes` | | Histogram of bytes held by the restorer per chunk; JSON responses are held whole, so their sizes land in the top buckets |
| `standin_body_bytes` | | Histogram of request body size |
| `standin_tokenize_seconds` | | Histogram of tokenize time per body, worker queueing included |
| `standin_cache_hits_total` | | String literals served from the scan cache |
| `standin_active_streams` | | ext_proc streams currently open |

Access-log attributes: `standin.mode`, `standin.tokenized`, `standin.distinct`,
`standin.tier_a`, `standin.tier_b`. Logs and metrics carry counts only, never
values.

## Local twin

`uv sync && scripts/twin.sh` runs agentgateway v1.6.0 with the example route, a
stub OpenAI-compatible upstream, and stand-in from the working tree. It uses
`CONTAINER_RUNTIME` if set, otherwise podman, otherwise docker, and CI runs it
with docker as the `e2e` job. It exits non-zero if any check fails and prints
where it copied the logs (`TWIN_LOG_DIR` overrides). `TWIN_*_PORT`,
`AGW_IMAGE`, and `KEEP_TWIN_WORKDIR=1` are honoured.

## Limitations

- Non-streamed JSON responses are buffered whole before they reach the client
  (fine for non-streaming calls, which arrive at once anyway). Past 64 MiB, or
  if the body is not valid JSON, restoration is byte-level, so tool-call
  `arguments` there get values escaped one level short (only matters for
  values needing escaping, such as PEM keys); invalid JSON counts as
  `standin_errors_total{phase="response_event"}`.
- Compressed responses pass through unrestored (`phase="response_encoded"`).
- A placeholder the model alters is not recognized.
- Per-rule attribution exists only in `standin_matches_total`, not per request.
- Non-UTF-8 or non-JSON request bodies are not inspected.

## License

Apache-2.0. See [LICENSE](LICENSE).
