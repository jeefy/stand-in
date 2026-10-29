#!/usr/bin/env bash
# Local end-to-end twin for stand-in: agentgateway v1.6.0 (podman or docker,
# --network host) running the route from examples/agentgateway-route.yaml,
# with its extProc host pointed at a locally running stand-in and its `ai`
# backend pointed at a stub OpenAI-compatible upstream started here.
#
# Every credential is SYNTHETIC (an obviously fake tskey-auth-style string).
# Nothing here talks to a real LLM provider. Output is PASS/FAIL lines and
# counts only; no matched value is ever printed.
#
# Checks (enforce mode): streaming SSE with the placeholder split across delta
# events (text and tool-call arguments), non-streaming JSON, a stream longer
# than the 5 s ext_proc requestTimeout, and a 3 MiB request body (several
# StreamedBodyResponse slices), and that the per-request counts arrive in the
# gateway's access log via the example's standin.* attributes, and that the
# client's Accept-Encoding never reaches upstream. Then shadow mode
# (upstream gets the original body) and processor down (failOpen pass-through).
# Each stand-in run (synthetic salt, default tokenize worker pool, no drain)
# must exit within 10 s of SIGTERM and log no ERROR line.
#
# Usage: scripts/twin.sh        (needs podman or docker, uv, curl, python3)
#
# CONTAINER_RUNTIME picks podman or docker (default: podman if installed, else
# docker). Logs (stand-in, stub, agentgateway and its generated config, metrics
# scrapes; counts only, no bodies) are copied to TWIN_LOG_DIR, default
# ${RUNNER_TEMP:-/tmp}/twin-logs. Exits non-zero if any check fails or the twin
# cannot start; never reads from a TTY.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${AGW_IMAGE:-cr.agentgateway.dev/agentgateway:v1.6.0}"
GW_PORT="${TWIN_GW_PORT:-24013}"
ADMIN_PORT="${TWIN_ADMIN_PORT:-25000}"
STATS_PORT="${TWIN_STATS_PORT:-25020}"
READY_PORT="${TWIN_READY_PORT:-25021}"
GRPC_PORT="${TWIN_GRPC_PORT:-29101}"
METRICS_PORT="${TWIN_METRICS_PORT:-29102}"
STUB_PORT="${TWIN_STUB_PORT:-28080}"
CONTAINER="stand-in-twin-agw"
LOG_DIR="${TWIN_LOG_DIR:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/twin-logs}"
# enforce refuses to start without a salt; this one is as synthetic as the key.
TWIN_SALT="stand-in-twin-synthetic-salt"

RUNTIME="${CONTAINER_RUNTIME:-}"
if [[ -z "${RUNTIME}" ]]; then
    if command -v podman >/dev/null 2>&1; then
        RUNTIME=podman
    elif command -v docker >/dev/null 2>&1; then
        RUNTIME=docker
    else
        echo "FAIL: neither podman nor docker found (or set CONTAINER_RUNTIME)" >&2
        exit 1
    fi
fi
for tool in "${RUNTIME}" uv curl python3; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
        echo "FAIL: ${tool} not found" >&2
        exit 1
    fi
done

mkdir -p "${LOG_DIR}"
rm -f "${LOG_DIR}"/{agentgateway.log,agentgateway-config.yaml,stub.log} \
    "${LOG_DIR}"/standin-{enforce,shadow}.log "${LOG_DIR}"/metrics-{enforce,shadow}.txt
WORK="$(mktemp -d "${TMPDIR:-/tmp}/stand-in-twin.XXXXXX")"
export TWIN_FAKE_KEY="tskey-auth-kTEST000CNTRL-0123456789abcdef0123456789abcdef"

STUB_PID=""
TOK_PID=""
PASSED=0
FAILED=0
FAILURES=()
FINISHED=0

pass() {
    PASSED=$((PASSED + 1))
    echo "PASS: $1"
}

fail() {
    FAILED=$((FAILED + 1))
    FAILURES+=("$1")
    echo "FAIL: $1"
}

# kill_tree <signal> <pid>: the process and its descendants (uv -> python ->
# tokenize workers), children first so none is orphaned before it is found.
kill_tree() {
    local child
    for child in $(pgrep -P "$2" 2>/dev/null || true); do
        kill_tree "$1" "${child}"
    done
    kill "-$1" "$2" 2>/dev/null || true
}

# stop_pid <pid>: SIGTERM (uv run forwards it), up to 10 s to exit, then
# SIGKILL for the whole tree. Returns 1 if it had to be killed.
stop_pid() {
    local pid="$1"
    [[ -n "${pid}" ]] || return 0
    kill "${pid}" 2>/dev/null || true
    for _ in $(seq 1 100); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            wait "${pid}" 2>/dev/null || true
            return 0
        fi
        sleep 0.1
    done
    kill_tree KILL "${pid}"
    wait "${pid}" 2>/dev/null || true
    return 1
}

cleanup() {
    local rc=$? log
    trap - ERR
    set +e
    "${RUNTIME}" container inspect "${CONTAINER}" >/dev/null 2>&1 &&
        "${RUNTIME}" logs "${CONTAINER}" >"${LOG_DIR}/agentgateway.log" 2>&1
    "${RUNTIME}" rm -f "${CONTAINER}" >/dev/null 2>&1
    stop_pid "${TOK_PID}"
    stop_pid "${STUB_PID}"
    cp "${WORK}"/standin-*.log "${WORK}/stub.log" "${WORK}"/metrics-*.txt "${LOG_DIR}/" 2>/dev/null
    cp "${WORK}/agw/config.yaml" "${LOG_DIR}/agentgateway-config.yaml" 2>/dev/null
    if [[ "${FINISHED}" == 0 ]]; then
        echo "RESULT: twin ABORTED (exit ${rc}) after ${PASSED} passed and ${FAILED} failed checks"
        for log in "${LOG_DIR}"/standin-*.log "${LOG_DIR}/stub.log" "${LOG_DIR}/agentgateway.log"; do
            [[ -s "${log}" ]] || continue
            echo "--- tail ${log}"
            tail -n 20 "${log}"
        done
    fi
    echo "logs: ${LOG_DIR}"
    if [[ "${KEEP_TWIN_WORKDIR:-0}" == 1 ]]; then
        echo "workdir kept: ${WORK}"
    else
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# set -E: also inside functions. $- drops "e" in command substitutions, whose
# failures are handled by the caller, so only failures that abort are reported.
trap 'rc=$?; [[ $- == *e* ]] && echo "ERROR: line ${LINENO}: ${BASH_COMMAND} (exit ${rc})" >&2' ERR

# wait_port <port> <what> [pid]: up to ~20 s, or until pid exits.
wait_port() {
    local port="$1" what="$2" pid="${3:-}"
    for _ in $(seq 1 100); do
        if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
            return 0
        fi
        if [[ -n "${pid}" ]] && ! kill -0 "${pid}" 2>/dev/null; then
            echo "FAIL: ${what} exited before opening 127.0.0.1:${port}" >&2
            exit 1
        fi
        sleep 0.2
    done
    echo "FAIL: ${what} did not open 127.0.0.1:${port}" >&2
    exit 1
}

# A leftover twin would answer in place of this one (gRPC binds with
# SO_REUSEPORT, so a second stand-in even shares the port).
"${RUNTIME}" rm -f "${CONTAINER}" >/dev/null 2>&1 || true
for port in "${GW_PORT}" "${ADMIN_PORT}" "${STATS_PORT}" "${READY_PORT}" "${GRPC_PORT}" \
    "${METRICS_PORT}" "${STUB_PORT}"; do
    if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
        echo "FAIL: 127.0.0.1:${port} already in use (leftover twin? set TWIN_*_PORT)" >&2
        exit 1
    fi
done
if ! "${RUNTIME}" image inspect "${IMAGE}" >/dev/null 2>&1; then
    echo "pulling ${IMAGE} with ${RUNTIME}"
    "${RUNTIME}" pull -q "${IMAGE}" >/dev/null
fi

# --- stub upstream ----------------------------------------------------------
cat >"${WORK}/stub.py" <<'PY'
"""OpenAI chat-completions stub: records each request body, answers by echoing
the first <<secret-...>> placeholder it received, split across many SSE delta
events (text and tool-call arguments) or across odd byte boundaries (JSON)."""
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLACEHOLDER = re.compile(rb"<<secret-[0-9a-f]{12}>>")
OUT = sys.argv[2]
seq = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read_body(self):
        if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
            body = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return body
                body += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def _chunk(self, data):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def do_POST(self):
        body = self._read_body()
        seq["n"] += 1
        with open(f"{OUT}/upstream-{seq['n']}.json", "wb") as fh:
            fh.write(body)
        with open(f"{OUT}/headers-{seq['n']}.json", "w") as fh:
            json.dump(sorted(k.lower() for k in self.headers.keys()), fh)
        req = json.loads(body)
        found = PLACEHOLDER.search(body)
        ph = found.group().decode() if found else "NONE"
        delay = int(self.headers.get("x-stub-delay-ms") or 0) / 1000
        reply = f"Use {ph} for the tailscale login."
        args = json.dumps({"command": f"tailscale up --authkey={ph}"})
        self.send_response(200)
        stream = bool(req.get("stream"))
        self.send_header("content-type", "text/event-stream" if stream else "application/json")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        base = {"id": "chatcmpl-twin", "created": 0, "model": req.get("model", "m")}
        if not stream:
            message = {
                "role": "assistant",
                "content": reply,
                "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "bash", "arguments": args}}
                ],
            }
            out = json.dumps({**base, "object": "chat.completion", "choices": [
                {"index": 0, "message": message, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            for i in range(0, len(out), 17):
                self._chunk(out[i:i + 17])
            self._chunk(b"")
            return

        def event(delta, finish=None, usage=None):
            obj = {**base, "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage:
                obj["usage"] = usage
            self._chunk(b"data: " + json.dumps(obj).encode() + b"\n\n")
            if delay:
                time.sleep(delay)

        event({"role": "assistant", "content": ""})
        for i in range(0, len(reply), 4):
            event({"content": reply[i:i + 4]})
        event({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                               "function": {"name": "bash", "arguments": ""}}]})
        for i in range(0, len(args), 6):
            event({"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 6]}}]})
        event({}, finish="tool_calls",
              usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        self._chunk(b"data: [DONE]\n\n")
        self._chunk(b"")


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
PY
python3 "${WORK}/stub.py" "${STUB_PORT}" "${WORK}" 2>"${WORK}/stub.log" &
STUB_PID=$!
wait_port "${STUB_PORT}" "stub upstream" "${STUB_PID}"

# --- agentgateway -------------------------------------------------------------
# The gateway config is examples/agentgateway-route.yaml itself (access-log
# attributes, route, extProc policy, ai backend); only the listener port, the
# extProc host and the backend address are rewritten for localhost.
mkdir -p "${WORK}/agw"
uv run --project "${ROOT}" --quiet python - "${ROOT}/examples/agentgateway-route.yaml" \
    "${WORK}/agw/config.yaml" "${ADMIN_PORT}" "${STATS_PORT}" "${READY_PORT}" "${GW_PORT}" \
    "${GRPC_PORT}" "${STUB_PORT}" <<'PY'
import sys

import yaml

src, dst, admin, stats, ready, gw, grpc, stub = sys.argv[1:]
cfg = yaml.safe_load(open(src))
add = cfg["frontendPolicies"]["accessLog"]["add"]
assert any(k.startswith("standin.") for k in add), "no standin.* access-log attributes"
(route,) = [r for r in cfg["routes"] if r["policies"].get("extProc")]
route["gateways"] = ["twin"]
route["policies"]["extProc"]["host"] = f"127.0.0.1:{grpc}"
ai = route["backends"][0]["ai"]
ai["hostOverride"] = f"127.0.0.1:{stub}"
ai["policies"].pop("backendTLS", None)
out = {
    "config": {
        "adminAddr": f"127.0.0.1:{admin}",
        "statsAddr": f"127.0.0.1:{stats}",
        "readinessAddr": f"127.0.0.1:{ready}",
    },
    "frontendPolicies": cfg["frontendPolicies"],
    "gateways": {"twin": {"port": int(gw)}},
    "routes": [route],
}
with open(dst, "w") as fh:
    yaml.safe_dump(out, fh, sort_keys=False)
PY
# The image runs as uid 65532, which rootful docker does not map to the
# caller: keep the bind-mounted config readable whatever the umask.
chmod 755 "${WORK}/agw"
chmod 644 "${WORK}/agw/config.yaml"

# The tokenize worker pool stays at its default (on), so the 3 MiB body is
# tokenized in a worker process. No drain on SIGTERM: a restart takes ~1 s.
start_standin() {
    local mode="$1"
    STANDIN_MODE="${mode}" STANDIN_SALT="${TWIN_SALT}" STANDIN_GRPC_PORT="${GRPC_PORT}" \
        STANDIN_METRICS_PORT="${METRICS_PORT}" STANDIN_DRAIN_SECONDS=0 STANDIN_SHUTDOWN_GRACE=1 \
        uv run --project "${ROOT}" --quiet python -m standin \
        >"${WORK}/standin-${mode}.log" 2>&1 &
    TOK_PID=$!
    wait_port "${GRPC_PORT}" "stand-in (${mode})" "${TOK_PID}"
}

stop_standin() {
    local mode="$1" started="${SECONDS}"
    if stop_pid "${TOK_PID}"; then
        pass "stand-in (${mode}) exited on SIGTERM in $((SECONDS - started))s"
    else
        fail "stand-in (${mode}) still running 10s after SIGTERM; killed"
    fi
    TOK_PID=""
    check "stand-in (${mode}) logged no errors (worker pool started, nothing failed)" \
        "standin-${mode}.log" "standin-${mode}.log" 'b"\"level\": \"ERROR\"" not in out'
}

start_standin enforce
"${RUNTIME}" run -d --name "${CONTAINER}" --network host \
    -v "${WORK}/agw:/config:Z" "${IMAGE}" -f /config/config.yaml >/dev/null
wait_port "${GW_PORT}" "agentgateway"

# --- requests -------------------------------------------------------------------
python3 - "${WORK}" <<'PY'
import json, os, sys
work = sys.argv[1]
key = os.environ["TWIN_FAKE_KEY"]
def req(stream, pad=0):
    messages = [
        {"role": "user", "content": "log in to tailscale with the key from the tool result"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function",
            "function": {"name": "bash", "arguments": "{\"command\":\"cat ts.env\"}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": f"TS_AUTHKEY={key}\n"},
    ]
    if pad:
        messages.insert(0, {"role": "system", "content": "lorem ipsum " * (pad // 12)})
    return {"model": "gpt-4o", "stream": stream, "messages": messages}
for name, body in {"stream": req(True), "plain": req(False), "big": req(True, 3 << 20)}.items():
    with open(f"{work}/req-{name}.json", "w") as fh:
        json.dump(body, fh)
PY

post() {
    # post <request> <client output file> [curl args]: sets CODE (000 if curl
    # failed) and UP, the stub's record of this request (absent if the request
    # never reached upstream, so no check reads an earlier request's record).
    local name="$1" out="$2" seen
    shift 2
    seen="$(find "${WORK}" -maxdepth 1 -name 'upstream-*.json' | wc -l)"
    UP="upstream-$((seen + 1)).json"
    CODE="$(curl -sS -N --max-time 60 -o "${WORK}/${out}" -w '%{http_code}' \
        -H 'content-type: application/json' -H 'authorization: Bearer twin-dummy' \
        -H 'Accept-Encoding: gzip' "$@" \
        --data-binary "@${WORK}/req-${name}.json" "http://127.0.0.1:${GW_PORT}/chat/completions")" ||
        true
}

http_ok() {
    if [[ "${CODE}" == 200 ]]; then
        pass "$1: HTTP 200"
    else
        fail "$1: HTTP ${CODE:-000}, expected 200"
    fi
}

# gateway_log <file> <n>: the container log once it holds n access-log lines
# (agentgateway writes them after the response has ended), or after 5 s.
gateway_log() {
    for _ in $(seq 1 25); do
        "${RUNTIME}" logs "${CONTAINER}" >"${WORK}/$1" 2>&1 || true
        if (($(grep -c 'http\.path=/chat/completions' "${WORK}/$1" || true) >= $2)); then
            return 0
        fi
        sleep 0.2
    done
}

check() {
    # check <description> <python expression over: up (upstream bytes), out (client bytes)>
    local desc="$1" upstream="$2" client="$3" expr="$4"
    if python3 - "${WORK}/${upstream}" "${WORK}/${client}" "${expr}" <<'PY'; then
import json, os, re, sys
key = os.environ["TWIN_FAKE_KEY"].encode()
up = open(sys.argv[1], "rb").read() if os.path.exists(sys.argv[1]) else b""
out = open(sys.argv[2], "rb").read() if os.path.exists(sys.argv[2]) else b""

def sse_text(raw):
    content, args = "", ""
    for line in raw.split(b"\n"):
        if line.startswith(b"data:") and line[5:].strip() not in (b"[DONE]", b""):
            for ch in json.loads(line[5:]).get("choices", []):
                d = ch.get("delta") or {}
                content += d.get("content") or ""
                for tc in d.get("tool_calls") or []:
                    args += (tc.get("function") or {}).get("arguments") or ""
    return content, args

def plain_text(raw):
    msg = json.loads(raw)["choices"][0]["message"]
    return msg["content"], msg["tool_calls"][0]["function"]["arguments"]

ok = eval(sys.argv[3], {"up": up, "out": out, "key": key, "sse_text": sse_text,
                        "plain_text": plain_text, "json": json, "re": re})
sys.exit(0 if ok else 1)
PY
        pass "${desc}"
    else
        fail "${desc}"
    fi
}

echo "== enforce mode"
post stream out-stream.sse
http_ok "stream"
up="${UP}"
check "stream: upstream received a placeholder" "${up}" out-stream.sse 'b"<<secret-" in up'
check "stream: upstream never saw the synthetic key" "${up}" out-stream.sse 'key not in up'
check "stream: client text has the synthetic key restored" "${up}" out-stream.sse \
    'key.decode() in sse_text(out)[0] and "<<secret-" not in sse_text(out)[0]'
check "stream: client tool-call arguments restored and valid JSON" "${up}" out-stream.sse \
    'json.loads(sse_text(out)[1])["command"].endswith(key.decode())'
check "stream: no placeholder left in client bytes" "${up}" out-stream.sse 'b"<<secret-" not in out'
check "stream: terminated with [DONE]" "${up}" out-stream.sse 'out.rstrip().endswith(b"data: [DONE]")'
check "stream: client sent Accept-Encoding gzip, upstream saw no accept-encoding" \
    "${up/upstream-/headers-}" out-stream.sse '"accept-encoding" not in json.loads(up)'

post plain out-plain.json
http_ok "plain"
up="${UP}"
check "plain: upstream received a placeholder, not the key" "${up}" out-plain.json \
    'b"<<secret-" in up and key not in up'
check "plain: client content + arguments restored" "${up}" out-plain.json \
    'key.decode() in plain_text(out)[0] and json.loads(plain_text(out)[1])["command"].endswith(key.decode())'

started="${SECONDS}"
post stream out-slow.sse -H 'x-stub-delay-ms: 300'
elapsed=$((SECONDS - started))
http_ok "slow stream (${elapsed}s)"
up="${UP}"
if ((elapsed > 5)); then
    pass "slow: stream outlasted the 5s ext_proc requestTimeout"
else
    fail "slow: stream took ${elapsed}s, not longer than the 5s ext_proc requestTimeout"
fi
check "slow: stream longer than requestTimeout completes and is restored" "${up}" out-slow.sse \
    'key.decode() in sse_text(out)[0] and out.rstrip().endswith(b"data: [DONE]")'

post big out-big.sse
http_ok "3 MiB body"
up="${UP}"
echo "3 MiB body: upstream bytes $(stat -c %s "${WORK}/${up}" 2>/dev/null || echo none)"
check "big: 3 MiB body tokenized upstream" "${up}" out-big.sse \
    'len(up) > 3_000_000 and b"<<secret-" in up and key not in up'
check "big: response restored" "${up}" out-big.sse 'key.decode() in sse_text(out)[0]'

curl -sS "http://127.0.0.1:${METRICS_PORT}/metrics" >"${WORK}/metrics-enforce.txt" || true
grep -E '^standin_(requests_total|restored_total|errors_total)' "${WORK}/metrics-enforce.txt" || true
check "metrics: no handled errors, restorations counted" metrics-enforce.txt metrics-enforce.txt \
    'not re.search(rb"^standin_errors_total", out, re.M) and float(re.search(rb"^standin_restored_total (\S+)", out, re.M).group(1)) >= 8'

gateway_log agw-enforce.log 4
check "access log: standin.* attributes carry the counts (4 enforce requests)" \
    agw-enforce.log agw-enforce.log \
    'all(out.count(f) >= 4 for f in (b"standin.mode=\"enforce\"", b"standin.tokenized=1", b"standin.distinct=1", b"standin.tier_a=1", b"standin.tier_b=0"))'

echo "== shadow mode"
stop_standin enforce
start_standin shadow
sleep 1
post stream out-shadow.sse
http_ok "shadow stream"
up="${UP}"
check "shadow: upstream received the original body" "${up}" out-shadow.sse \
    'key in up and b"<<secret-" not in up'
check "shadow: client stream intact" "${up}" out-shadow.sse \
    'sse_text(out)[0] == "Use NONE for the tailscale login."'
curl -sS "http://127.0.0.1:${METRICS_PORT}/metrics" >"${WORK}/metrics-shadow.txt" || true
check "shadow: request counted as tokenized" metrics-shadow.txt metrics-shadow.txt \
    're.search(rb"standin_requests_total\{mode=\"shadow\",outcome=\"tokenized\"\} 1", out) is not None'

echo "== stand-in down (failOpen)"
stop_standin shadow
post stream out-down.sse
http_ok "stand-in down"
up="${UP}"
gateway_log agw-down.log 6
grep 'http\.path=/chat/completions' "${WORK}/agw-down.log" | tail -1 >"${WORK}/agw-down-line.log" || true
check "failOpen: no standin.* attributes when the processor never answered" \
    agw-down-line.log agw-down-line.log \
    'not re.search(rb"standin\.[a-z_]+=", out) and b"/chat/completions" in out'
check "failOpen: request passes through unmodified when the processor is unreachable" \
    "${up}" out-down.sse 'key in up and out.rstrip().endswith(b"data: [DONE]")'

"${RUNTIME}" logs "${CONTAINER}" >"${WORK}/agw-final.log" 2>&1 || true
echo "agentgateway log lines mentioning error: $(grep -ciE 'error|panic' "${WORK}/agw-final.log" || true)" \
    "(expected: the failOpen case's refused ext_proc connection)"

FINISHED=1
if [[ "${FAILED}" == 0 ]]; then
    echo "RESULT: all ${PASSED} twin checks passed"
else
    echo "RESULT: ${FAILED} of $((PASSED + FAILED)) twin checks FAILED:"
    printf '  - %s\n' "${FAILURES[@]}"
    exit 1
fi
