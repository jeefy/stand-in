"""Placeholder restoration for non-streamed JSON responses.

A byte-level scan cannot tell how deep a placeholder sits: in a chat
completion's ``tool_calls[].function.arguments`` the string content is itself
JSON text, so the value must be escaped twice (once for the inner document,
once for the outer string literal), while in ``message.content`` it is escaped
once. ``JsonRestorer`` therefore buffers the whole body, parses it, restores
each string by key (``JSON_FIELDS`` get the JSON-escaped value, everything
else the raw value; re-serializing escapes once more) and re-serializes only
if something changed, so an untouched body is forwarded byte for byte.

A body that does not parse falls back to the byte-level ``Restorer`` (complete
placeholders, JSON-escaped once). A body over ``MAX_BUFFER_BYTES`` stops being
buffered and switches to the byte-level ``Restorer`` for the rest of the
stream.

The substitution helpers here are shared with ``standin.sse``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from standin.tokenizer import PLACEHOLDER_RE, PLACEHOLDER_RE_B, Restorer, json_escaped

log = logging.getLogger("standin.json_restorer")

# Strings under these keys are JSON documents (tool-call arguments), so a
# placeholder inside them sits in an inner string literal.
JSON_FIELDS = frozenset({"arguments", "partial_json"})
# Objects under these keys hold caller-defined data (Anthropic ``tool_use``
# ``input``): their keys say nothing about the protocol, so every string in
# them is plain text even if a tool parameter happens to be named ``arguments``.
OPAQUE_KEYS = frozenset({"input"})
# Same as the gRPC message cap in standin.main.
MAX_BUFFER_BYTES = 64 << 20

Skip = set[tuple[int, str]]


def dumps(obj: Any) -> bytes:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(obj, separators=(",", ":")).encode("ascii")


class PlaceholderSubstituter:
    """Placeholder -> value tables and the ``restored`` counter.

    ``kind`` ``"text"`` inserts the raw value (the caller re-serializes, which
    escapes it once); ``"json"`` inserts it JSON-escaped, for strings whose
    content is itself JSON text. ``replace_bytes`` works on serialized JSON
    and always inserts the JSON-escaped value.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._text = dict(mapping)
        self._json = {ph: json_escaped(v) for ph, v in mapping.items()}
        self._bytes = {ph.encode(): json_escaped(v).encode() for ph, v in mapping.items()}
        self.restored = 0

    def replace(self, text: str, kind: str) -> str:
        if "<<secret-" not in text:
            return text
        table = self._json if kind == "json" else self._text

        def sub(m: re.Match[str]) -> str:
            value = table.get(m.group())
            if value is None:
                return m.group()
            self.restored += 1
            return value

        return PLACEHOLDER_RE.sub(sub, text)

    def replace_bytes(self, data: bytes) -> bytes:
        if b"<<secret-" not in data:
            return data

        def sub(m: re.Match[bytes]) -> bytes:
            value = self._bytes.get(m.group())
            if value is None:
                return m.group()
            self.restored += 1
            return value

        return PLACEHOLDER_RE_B.sub(sub, data)

    def replace_tree(
        self,
        node: dict[str, Any] | list[Any],
        skip: Skip | None = None,
        *,
        opaque_keys: frozenset[str] = frozenset(),
        json_fields: frozenset[str] = JSON_FIELDS,
    ) -> bool:
        """Restore every string value under ``node`` in place; True if any changed.

        ``skip`` holds ``(id(container), key)`` pairs already handled by the
        caller. Below a key in ``opaque_keys`` no key counts as a JSON field.
        """
        changed = False
        items: list[tuple[Any, Any]] = (
            list(node.items()) if isinstance(node, dict) else list(enumerate(node))
        )
        container: Any = node
        for k, v in items:
            if isinstance(v, str):
                if skip and isinstance(k, str) and (id(node), k) in skip:
                    continue
                new = self.replace(v, "json" if k in json_fields else "text")
                if new != v:
                    container[k] = new
                    changed = True
            elif isinstance(v, dict | list):
                fields = frozenset() if k in opaque_keys else json_fields
                changed |= self.replace_tree(v, skip, opaque_keys=opaque_keys, json_fields=fields)
        return changed


class JsonRestorer(PlaceholderSubstituter):
    """Buffer a whole JSON response body and restore it at ``flush``.

    ``feed`` returns nothing until the body exceeds ``max_buffer``; ``held`` is
    the buffered size, so ``standin_held_bytes`` sees whole bodies here.
    """

    def __init__(self, mapping: dict[str, str], *, max_buffer: int = MAX_BUFFER_BYTES) -> None:
        super().__init__(mapping)
        self._mapping = mapping
        self._max_buffer = max_buffer
        self._chunks: list[bytes] = []
        self._size = 0
        self._fallback: Restorer | None = None
        self.max_held = 0
        self.errors = 0

    @property
    def held(self) -> int:
        return self._fallback.held if self._fallback is not None else self._size

    def feed(self, chunk: bytes) -> bytes:
        fallback = self._fallback
        if fallback is not None:
            return self._counted(fallback, fallback.feed(chunk))
        if chunk:
            self._chunks.append(chunk)
            self._size += len(chunk)
            self.max_held = max(self.max_held, self._size)
        if self._size <= self._max_buffer:
            return b""
        log.warning(
            "JSON response over the buffer cap; restoring byte-level",
            extra={"bytes": self._size, "limit": self._max_buffer},
        )
        fallback = self._fallback = Restorer(self._mapping)
        return self._counted(fallback, fallback.feed(self._take()))

    def flush(self) -> bytes:
        fallback = self._fallback
        if fallback is not None:
            return self._counted(fallback, fallback.flush())
        data = self._take()
        if not data:
            return b""
        before = self.restored
        try:
            return self._restore(data)
        except Exception:
            self.restored = before
            self.errors += 1
            byte_level = Restorer(self._mapping)
            out = byte_level.feed(data) + byte_level.flush()
            self.restored += byte_level.restored
            return out

    def abandon(self, chunk: bytes) -> bytes:
        """Fail-open exit: buffered (or held) bytes and ``chunk``, unmodified."""
        if self._fallback is not None:
            return self._fallback.abandon(chunk)
        return self._take() + chunk

    def _take(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks = []
        self._size = 0
        return data

    def _counted(self, fallback: Restorer, out: bytes) -> bytes:
        self.restored = fallback.restored
        self.max_held = max(self.max_held, fallback.held)
        return out

    def _restore(self, data: bytes) -> bytes:
        if b"<" not in data and b"\\u003c" not in data.lower():
            return data
        obj = json.loads(data.decode("utf-8"))
        if isinstance(obj, str):
            new = self.replace(obj, "text")
            return data if new == obj else dumps(new)
        if isinstance(obj, dict | list) and self.replace_tree(obj, opaque_keys=OPAQUE_KEYS):
            return dumps(obj)
        return data
