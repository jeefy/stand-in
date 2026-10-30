"""The newest turn of an LLM request body, for counting matches once.

Agents re-send the whole conversation every turn, so counting every match in
every body counts an old secret again on each later turn. Only the strings
after the last assistant item that has something after it are new; with no
such item the request opens a conversation and the whole body counts, once.

- ``messages`` (Anthropic messages, OpenAI chat completions): the boundary is
  ``role == "assistant"``. The tail picks up ``tool_result`` blocks in user
  messages and ``role: "tool"`` messages; a trailing assistant prefill has
  nothing after it, so it is part of the tail, not the boundary.
- Responses ``input`` list: the boundary is an assistant ``message``, a
  ``reasoning`` item or any ``*_call`` item (not ``*_call_output``). With
  ``previous_response_id`` the server holds the history and ``input`` is all
  new, while ``instructions`` is re-sent every turn: count ``input`` only.
- A string ``input`` or any other shape counts as a whole body.

Base64 payloads (``source.data`` of a base64 source, ``image_url``,
``file_data``) are skipped: they are not text the model reads as such.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

Scope = Literal["tail", "first", "input_only", "none"]
SCOPES: tuple[Scope, ...] = ("tail", "first", "input_only", "none")

_SKIP_KEYS = frozenset({"image_url", "file_data"})


def new_turn_strings(doc: Any) -> tuple[Scope, list[str]]:
    """``(scope, strings)``: the decoded string values to count for parsed
    request body ``doc``. ``tail``: the items after the boundary; ``first``:
    the whole body (no boundary, or an unknown shape); ``input_only``: the
    Responses ``input`` of a request with ``previous_response_id``."""
    if isinstance(doc, dict):
        messages = doc.get("messages")
        if isinstance(messages, list):
            return _tail_or_first(doc, messages, _is_assistant_message)
        items = doc.get("input")
        if items is not None:
            if doc.get("previous_response_id"):
                return "input_only", strings(items)
            if isinstance(items, list):
                return _tail_or_first(doc, items, _is_responses_boundary)
    return "first", strings(doc)


def strings(value: Any) -> list[str]:
    """Every string value in ``value``, in document order (object keys are not
    included), minus base64 payloads."""
    out: list[str] = []
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            base64_source = node.get("type") == "base64"
            children = [
                v
                for k, v in node.items()
                if k not in _SKIP_KEYS and not (base64_source and k == "data")
            ]
            stack.extend(reversed(children))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return out


def _tail_or_first(
    doc: Any, items: Sequence[Any], is_boundary: Callable[[Any], bool]
) -> tuple[Scope, list[str]]:
    boundary = _last_boundary(items, is_boundary)
    if boundary is None:
        return "first", strings(doc)
    return "tail", strings(list(items[boundary + 1 :]))


def _last_boundary(items: Sequence[Any], is_boundary: Callable[[Any], bool]) -> int | None:
    """Index of the last boundary item with at least one item after it."""
    for i in range(len(items) - 2, -1, -1):
        if is_boundary(items[i]):
            return i
    return None


def _is_assistant_message(item: Any) -> bool:
    return isinstance(item, dict) and item.get("role") == "assistant"


def _is_responses_boundary(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    kind = item.get("type")
    if kind is None or kind == "message":
        return item.get("role") == "assistant"
    return isinstance(kind, str) and (kind == "reasoning" or kind.endswith("_call"))
