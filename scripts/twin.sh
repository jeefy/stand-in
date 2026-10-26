#!/usr/bin/env bash
# Local end-to-end twin for stand-in: agentgateway v1.6.0 (podman,
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
#
# Usage: scripts/twin.sh        (needs podman, uv, curl, python3)
set -euo pipefail

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
WORK="$(mktemp -d "${TMPDIR:-/tmp}/stand-in-twin.XXXXXX")"
export TWIN_FAKE_KEY="tskey-auth-kTEST000CNTRL-0123456789abcdef0123456789abcdef"

STUB_PID=""
TOK_PID=""
FAILED=0

cleanup() {
    podman rm -f "${CONTAINER}" >/dev/null 2>&1 || true
    [[ -n "${TOK_PID}" ]] && kill "${TOK_PID}" 2>/dev/null || true
    [[ -n "${STUB_PID}" ]] && kill "${STUB_PID}" 2>/dev/null || true
    wait 2>/dev/null || true
    if [[ "${KEEP_TWIN_WORKDIR:-0}" == 1 ]]; then
        echo "workdir kept: ${WORK}"
    else
        rm -rf "${WORK}"
    fi
}
trap cleanup EXIT

wait_port() {
    local port="$1" what="$2"
    for _ in $(seq 1 100); do
        if (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null; then
            return 0
        fi
        sleep 0.2
    done
    echo "FAIL: ${what} did not open 127.0.0.1:${port}" >&2
    exit 1
}

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
python3 "${WORK}/stub.py" "${STUB_PORT}" "${WORK}" &
STUB_PID=$!
wait_port "${STUB_PORT}" "stub upstream"

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

start_standin() {
    local mode="$1"
    if [[ -n "${TOK_PID}" ]]; then
        kill "${TOK_PID}" 2>/dev/null || true
        wait "${TOK_PID}" 2>/dev/null || true
    fi
    STANDIN_MODE="${mode}" STANDIN_GRPC_PORT="${GRPC_PORT}" \
        STANDIN_METRICS_PORT="${METRICS_PORT}" STANDIN_SHUTDOWN_GRACE=1 \
        uv run --project "${ROOT}" --quiet python -m standin \
        >"${WORK}/standin-${mode}.log" 2>&1 &
    TOK_PID=$!
    wait_port "${GRPC_PORT}" "stand-in (${mode})"
}

start_standin enforce
podman rm -f "${CONTAINER}" >/dev/null 2>&1 || true
podman run -d --name "${CONTAINER}" --network host \
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
    local name="$1" out="$2"
    shift 2
    curl -sS -N --max-time 60 -o "${WORK}/${out}" -w '%{http_code}' \
        -H 'content-type: application/json' -H 'authorization: Bearer twin-dummy' \
        -H 'Accept-Encoding: gzip' "$@" \
        --data-binary "@${WORK}/req-${name}.json" "http://127.0.0.1:${GW_PORT}/chat/completions"
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
        echo "PASS: ${desc}"
    else
        echo "FAIL: ${desc}"
        FAILED=1
    fi
}

latest_upstream() { ls -1t "${WORK}"/upstream-*.json 2>/dev/null | head -1 | xargs -r basename; }

echo "== enforce mode"
code="$(post stream out-stream.sse)"
echo "streaming: HTTP ${code}"
up="$(latest_upstream)"
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

code="$(post plain out-plain.json)"
echo "non-streaming: HTTP ${code}"
up="$(latest_upstream)"
check "plain: upstream received a placeholder, not the key" "${up}" out-plain.json \
    'b"<<secret-" in up and key not in up'
check "plain: client content + arguments restored" "${up}" out-plain.json \
    'key.decode() in plain_text(out)[0] and json.loads(plain_text(out)[1])["command"].endswith(key.decode())'

start_ts=$(date +%s)
code="$(post stream out-slow.sse -H 'x-stub-delay-ms: 300')"
elapsed=$(($(date +%s) - start_ts))
echo "slow stream (${elapsed}s, ext_proc requestTimeout 5s): HTTP ${code}"
up="$(latest_upstream)"
check "slow: stream longer than requestTimeout completes and is restored" "${up}" out-slow.sse \
    'key.decode() in sse_text(out)[0] and out.rstrip().endswith(b"data: [DONE]")'

code="$(post big out-big.sse)"
echo "3 MiB body: HTTP ${code}, upstream bytes $(wc -c <"${WORK}/$(latest_upstream)")"
up="$(latest_upstream)"
check "big: 3 MiB body tokenized upstream" "${up}" out-big.sse \
    'len(up) > 3_000_000 and b"<<secret-" in up and key not in up'
check "big: response restored" "${up}" out-big.sse 'key.decode() in sse_text(out)[0]'

curl -sS "http://127.0.0.1:${METRICS_PORT}/metrics" >"${WORK}/metrics-enforce.txt"
grep -E '^standin_(requests_total|restored_total|errors_total)' "${WORK}/metrics-enforce.txt" || true
check "metrics: no handled errors, restorations counted" metrics-enforce.txt metrics-enforce.txt \
    'not re.search(rb"^standin_errors_total", out, re.M) and float(re.search(rb"^standin_restored_total (\S+)", out, re.M).group(1)) >= 8'

podman logs "${CONTAINER}" >"${WORK}/agw-enforce.log" 2>&1
check "access log: standin.* attributes carry the counts (4 enforce requests)" \
    agw-enforce.log agw-enforce.log \
    'all(out.count(f) >= 4 for f in (b"standin.mode=\"enforce\"", b"standin.tokenized=1", b"standin.distinct=1", b"standin.tier_a=1", b"standin.tier_b=0"))'

echo "== shadow mode"
start_standin shadow
sleep 1
code="$(post stream out-shadow.sse)"
echo "shadow streaming: HTTP ${code}"
up="$(latest_upstream)"
check "shadow: upstream received the original body" "${up}" out-shadow.sse \
    'key in up and b"<<secret-" not in up'
check "shadow: client stream intact" "${up}" out-shadow.sse \
    'sse_text(out)[0] == "Use NONE for the tailscale login."'
curl -sS "http://127.0.0.1:${METRICS_PORT}/metrics" >"${WORK}/metrics-shadow.txt"
check "shadow: request counted as tokenized" metrics-shadow.txt metrics-shadow.txt \
    're.search(rb"standin_requests_total\{mode=\"shadow\",outcome=\"tokenized\"\} 1", out) is not None'

echo "== stand-in down (failOpen)"
kill "${TOK_PID}" 2>/dev/null || true
wait "${TOK_PID}" 2>/dev/null || true
TOK_PID=""
code="$(post stream out-down.sse)"
echo "stand-in down: HTTP ${code}"
up="$(latest_upstream)"
podman logs "${CONTAINER}" 2>&1 | tail -1 >"${WORK}/agw-down-line.log"
check "failOpen: no standin.* attributes when the processor never answered" \
    agw-down-line.log agw-down-line.log \
    'not re.search(rb"standin\.[a-z_]+=", out) and b"/chat/completions" in out'
check "failOpen: request passes through unmodified when the processor is unreachable" \
    "${up}" out-down.sse 'key in up and out.rstrip().endswith(b"data: [DONE]")'

podman logs "${CONTAINER}" >"${WORK}/agw-final.log" 2>&1 || true
echo "agentgateway log lines mentioning error: $(grep -ciE 'error|panic' "${WORK}/agw-final.log" || true)" \
    "(expected: the failOpen case's refused ext_proc connection)"

if [[ "${FAILED}" == 0 ]]; then
    echo "RESULT: all twin checks passed"
else
    echo "RESULT: twin checks FAILED"
    exit 1
fi
