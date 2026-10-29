"""Placeholder restoration for SSE responses.

Models stream text a few tokens per event, so a placeholder is usually split
across several ``data:`` events (``"<<sec"`` then ``"ret-1a2b"`` then
``"3c4d5e6f>>"``), with JSON framing in between that a byte-level scan cannot
see through. This restorer parses each event, follows every delta channel
(OpenAI chat ``content``/``tool_calls[].function.arguments``, Anthropic
``content_block_delta`` text/partial_json/thinking, Responses API ``*.delta``)
and holds back a trailing placeholder prefix per channel until the next delta
of the same channel arrives. Held text is released in a synthetic delta event
before any terminal event (finish_reason, content_block_stop, ``*.done``,
``[DONE]``) and at end of stream, so nothing is ever dropped.

Values are inserted raw into text channels (the event is re-serialized, which
escapes them once) and JSON-escaped into argument channels, whose content is
itself JSON text. Events that are not single-line JSON fall back to a
byte-level replacement of complete placeholders.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from standin.json_restorer import PlaceholderSubstituter, dumps
from standin.tokenizer import partial_hold_point

_EVENT_END = re.compile(rb"\r?\n\r?\n")
_CHAT_TEXT_FIELDS = ("content", "reasoning_content", "reasoning", "refusal")
_ANTHROPIC_FIELDS = (("text", "text"), ("partial_json", "json"), ("thinking", "text"))
_TERMINAL_TYPES = frozenset(
    {
        "content_block_stop",
        "message_delta",
        "message_stop",
        "error",
        "response.completed",
        "response.incomplete",
        "response.failed",
    }
)
Path = tuple[str | int, ...]


@dataclass
class _Channel:
    key: tuple[Any, ...]
    container: dict[str, Any]
    field: str
    kind: str
    template: dict[str, Any]
    path: Path


@dataclass
class _Held:
    text: str
    kind: str
    event_name: bytes | None
    lead: bytes
    template: dict[str, Any]
    path: Path


class SseRestorer(PlaceholderSubstituter):
    def __init__(self, mapping: dict[str, str]) -> None:
        super().__init__(mapping)
        self._buf = b""
        self._held: dict[tuple[Any, ...], _Held] = {}
        self.max_held = 0
        self.errors = 0

    @property
    def held(self) -> int:
        return len(self._buf) + sum(len(h.text.encode()) for h in self._held.values())

    def feed(self, chunk: bytes) -> bytes:
        data = self._buf + chunk
        out: list[bytes] = []
        pos = 0
        while True:
            m = _EVENT_END.search(data, pos)
            if m is None:
                break
            event = data[pos : m.end()]
            pos = m.end()
            try:
                out.append(self._event(event))
            except Exception:
                self.errors += 1
                out.append(event)
        self._buf = data[pos:]
        self.max_held = max(self.max_held, self.held)
        return b"".join(out)

    def flush(self) -> bytes:
        rest, self._buf = self._buf, b""
        out = self._event(rest) if rest else b""
        return out + self._drain()

    def abandon(self, chunk: bytes) -> bytes:
        """Fail-open exit: held channel text, unparsed bytes and ``chunk``."""
        try:
            held = self._drain()
        except Exception:
            held = b""
            self._held.clear()
        out, self._buf = held + self._buf + chunk, b""
        return out

    def _event(self, event: bytes) -> bytes:
        lines = event.splitlines(keepends=True)
        data_lines = [i for i, line in enumerate(lines) if line.startswith(b"data:")]
        if len(data_lines) != 1:
            return self.replace_bytes(event)
        i = data_lines[0]
        line = lines[i]
        body_end = len(line.rstrip(b"\r\n"))
        payload = line[5:body_end]
        lead = b" " if payload.startswith(b" ") else b""
        payload = payload[len(lead) :]
        if payload.strip() == b"[DONE]":
            return self._drain() + event
        if not self._held and b"<" not in payload and b"\\u003c" not in payload.lower():
            return event
        try:
            obj = json.loads(payload)
        except ValueError:
            return self.replace_bytes(event)
        if not isinstance(obj, dict):
            return self.replace_bytes(event)

        event_name = next(
            (ln[6:].strip() for ln in lines if ln.startswith(b"event:")),
            None,
        )
        terminal = _is_terminal(obj)
        changed = False
        channel_ids: set[tuple[int, str]] = set()
        for ch in _channels(obj):
            channel_ids.add((id(ch.container), ch.field))
            original = ch.container[ch.field]
            held = self._held.pop(ch.key, None)
            text = (held.text if held else "") + original
            cut = len(text) if terminal else partial_hold_point(text)
            head, tail = text[:cut], text[cut:]
            if tail:
                self._held[ch.key] = _Held(tail, ch.kind, event_name, lead, ch.template, ch.path)
            new = self.replace(head, ch.kind)
            if new != original:
                ch.container[ch.field] = new
                changed = True
        changed |= self.replace_tree(obj, channel_ids)

        prefix = self._drain() if terminal else b""
        if changed:
            lines[i] = b"data:" + lead + dumps(obj) + line[body_end:]
            event = b"".join(lines)
        return prefix + event

    def _drain(self) -> bytes:
        out: list[bytes] = []
        for held in self._held.values():
            obj = copy.deepcopy(held.template)
            _set_path(obj, held.path, self.replace(held.text, held.kind))
            name = b"event: " + held.event_name + b"\n" if held.event_name else b""
            out.append(name + b"data:" + held.lead + dumps(obj) + b"\n\n")
        self._held.clear()
        return b"".join(out)


def _set_path(obj: Any, path: Path, value: str) -> None:
    for step in path[:-1]:
        obj = obj[step]
    obj[path[-1]] = value


def _is_terminal(obj: dict[str, Any]) -> bool:
    choices = obj.get("choices")
    if isinstance(choices, list) and any(
        isinstance(c, dict) and c.get("finish_reason") for c in choices
    ):
        return True
    kind = obj.get("type")
    return isinstance(kind, str) and (kind in _TERMINAL_TYPES or kind.endswith(".done"))


def _channels(obj: dict[str, Any]) -> list[_Channel]:
    out: list[_Channel] = []
    choices = obj.get("choices")
    if isinstance(choices, list):
        base = {k: v for k, v in obj.items() if k not in ("choices", "usage")}
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
                continue
            ci = choice.get("index", 0)
            delta = choice["delta"]
            for name in _CHAT_TEXT_FIELDS:
                if isinstance(delta.get(name), str):
                    tpl = {**base, "choices": [{"index": ci, "delta": {name: ""}}]}
                    path: Path = ("choices", 0, "delta", name)
                    out.append(_Channel(("chat", ci, name), delta, name, "text", tpl, path))
            calls = delta.get("tool_calls")
            for call in calls if isinstance(calls, list) else []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    ti = call.get("index", 0)
                    tpl = {
                        **base,
                        "choices": [
                            {
                                "index": ci,
                                "delta": {
                                    "tool_calls": [{"index": ti, "function": {"arguments": ""}}]
                                },
                            }
                        ],
                    }
                    path = ("choices", 0, "delta", "tool_calls", 0, "function", "arguments")
                    out.append(
                        _Channel(("chat", ci, "tool", ti), fn, "arguments", "json", tpl, path)
                    )
        return out
    kind = obj.get("type")
    delta = obj.get("delta")
    if kind == "content_block_delta" and isinstance(delta, dict):
        bi = obj.get("index", 0)
        for name, ckind in _ANTHROPIC_FIELDS:
            if isinstance(delta.get(name), str):
                tpl = {
                    "type": "content_block_delta",
                    "index": bi,
                    "delta": {"type": delta.get("type"), name: ""},
                }
                out.append(
                    _Channel(("anthropic", bi, name), delta, name, ckind, tpl, ("delta", name))
                )
    elif isinstance(kind, str) and kind.endswith(".delta") and isinstance(delta, str):
        key = (
            "responses",
            kind,
            obj.get("item_id"),
            obj.get("output_index"),
            obj.get("content_index"),
        )
        tpl = {**obj, "delta": ""}
        ckind = "json" if "arguments" in kind else "text"
        out.append(_Channel(key, obj, "delta", ckind, tpl, ("delta",)))
    return out
