"""Request-side tokenization and byte-level response restoration.

Placeholders are ``<<secret-`` + 12 lowercase hex + ``>>``: the first 6 bytes
of ``blake2b(value, key=salt)``. The same value always maps to the same
placeholder for a given salt, so placeholders are stable across turns and the
upstream prompt cache keeps hitting.

Nothing in this module logs; callers only ever see counts.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from standin.patterns import COUNT_TIERS, Rule, load_count_rules, load_rules
from standin.turns import Scope, new_turn_strings

PLACEHOLDER_PREFIX = "<<secret-"
PLACEHOLDER_HEX_LEN = 12
PLACEHOLDER_LEN = len(PLACEHOLDER_PREFIX) + PLACEHOLDER_HEX_LEN + 2
MAX_HOLD = PLACEHOLDER_LEN - 1

SALT_ENV = "STANDIN_SALT"
DEFAULT_SALT = "stand-in-default-salt"
CACHE_ENV = "STANDIN_CACHE_ENTRIES"
DEFAULT_CACHE_ENTRIES = 50_000
_CACHE_MIN_CHARS = 512

PLACEHOLDER_RE = re.compile(r"<<secret-[0-9a-f]{12}>>")
PLACEHOLDER_RE_B = re.compile(rb"<<secret-[0-9a-f]{12}>>")
# A proper prefix of a placeholder (never the complete 23-byte form).
_PARTIAL = r"<(?:<(?:s(?:e(?:c(?:r(?:e(?:t(?:-(?:[0-9a-f]{12}>|[0-9a-f]{0,12}))?)?)?)?)?)?)?)?"
PARTIAL_RE = re.compile(_PARTIAL)
PARTIAL_RE_B = re.compile(_PARTIAL.encode())

# PEM: from the end of a BEGIN header, lazily through the matching END footer.
# The body class covers base64, PEM headers (Proc-Type/DEK-Info), real line
# breaks and JSON-escaped ones (\n inside a tool result that is itself JSON).
_PEM_TAIL = re.compile(
    r"[A-Za-z0-9+/=\\ \t\r\n:,._-]{0,32768}?-----END[ A-Z0-9_-]{0,40}PRIVATE KEY(?: BLOCK)?-----"
)
# Truncated PEM (no footer in view, e.g. `head -5 key.pem`): header plus the
# base64 lines that follow it.
_PEM_LINES = re.compile(r"(?:(?:\r?\n|(?:\\r)?\\n)[ \t]*[A-Za-z0-9+/=]{16,})+")
# What may follow a `data` key for the next line(s) to be its base64 block:
# `data:`, `client-key-data: |`, `"binaryData":` + end of line.
_AFTER_DATA_KEY = re.compile(r"""["']?[ \t]*:[ \t]*(?:[|>][-+0-9]*)?[ \t]*\r?$""")
_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
_KEY_BOUNDARY = frozenset(" \t\n\r\"'{,")
_ASCII_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_VALUE_SEP = re.compile(r"""[:=][\s"']*""")
# One JSON string literal (unrolled loop: fast on multi-MB bodies).
_JSON_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"')
# Joins decoded string literals for one scan. No rule matches this run (Tier
# A/B never match NUL; the Tier D and C5 classes that do stop at the newlines),
# so no span crosses a literal boundary; the newlines keep `(?m)^`/`$` anchored
# at literal edges like they are for the gateway's per-message scan.
_LITERAL_SEP = "\n\x00\n"
# Wall-clock cap on one count_new scan. Python `re` is backtracking: a body
# built to defeat the Tier C prefilters (a long run of `apikey`, say) can take
# seconds where the gateway's Rust regex is linear, and the count shares the
# request's critical path. Past the budget the count is dropped, not the body.
COUNT_BUDGET_SECONDS = 2.0


class CountBudgetExceeded(Exception):
    pass


@dataclass
class _Span:
    start: int
    end: int
    hits: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class TokenizeResult:
    text: str
    mapping: dict[str, str]
    hits: list[tuple[str, int]]
    replaced: int

    @property
    def distinct(self) -> int:
        return len(self.mapping)

    def tier_counts(self) -> dict[str, int]:
        counts = Counter(tier for tier, _ in self.hits)
        return {"a": counts.get("a", 0), "b": counts.get("b", 0)}


@dataclass
class NewCounts:
    """Rule matches in the newest turn of a request (see standin.turns).
    ``error`` is the exception type name when counting failed."""

    scope: Scope
    hits: list[tuple[str, int]] = field(default_factory=list)
    error: str | None = None

    def tier_counts(self) -> dict[str, int]:
        counts = Counter(tier for tier, _ in self.hits)
        return {tier: counts.get(tier, 0) for tier in COUNT_TIERS}


class Tokenizer:
    def __init__(
        self,
        rules: list[Rule] | None = None,
        salt: str | None = None,
        cache_entries: int | None = None,
        count_rules: list[Rule] | None = None,
    ) -> None:
        self.rules = rules if rules is not None else load_rules()
        self._count_rules = count_rules
        key = (salt if salt is not None else os.environ.get(SALT_ENV, DEFAULT_SALT)).encode()
        if len(key) > hashlib.blake2b.MAX_KEY_SIZE:
            key = hashlib.blake2b(key).digest()
        self._key = key
        self._cache: OrderedDict[bytes, tuple[tuple[int, int, tuple[tuple[str, int], ...]], ...]]
        self._cache = OrderedDict()
        self._cache_size = (
            int(os.environ.get(CACHE_ENV, DEFAULT_CACHE_ENTRIES))
            if cache_entries is None
            else cache_entries
        )
        self.cache_hits = 0

    def placeholder(self, value: str) -> str:
        digest = hashlib.blake2b(
            value.encode("utf-8", "surrogatepass"), key=self._key, digest_size=6
        ).hexdigest()
        return f"{PLACEHOLDER_PREFIX}{digest}>>"

    @property
    def count_rules(self) -> list[Rule]:
        """Tier A-D rules for count_new, loaded on first use."""
        if self._count_rules is None:
            self._count_rules = load_count_rules()
        return self._count_rules

    def find_spans(
        self, text: str, rules: list[Rule] | None = None, deadline: float | None = None
    ) -> list[_Span]:
        """Non-overlapping replacement spans (of ``self.rules`` unless ``rules``
        is given); overlapping matches are unioned so a secret is never
        partially exposed, and every rule match stays in ``hits``. Past
        ``deadline`` (``time.monotonic()``) raises CountBudgetExceeded."""
        found: list[_Span] = []
        pem: list[tuple[int, int]] = []
        line_rules: list[Rule] = []
        folded = _Folded(text)
        for rule in self.rules if rules is None else rules:
            if rule.handling == "data_block_line":
                line_rules.append(rule)
                continue
            for m in _rule_matches(rule, text, folded, deadline):
                start, end = m.span()
                if rule.key_boundary and start > 0 and text[start - 1] in _ASCII_ALNUM:
                    continue
                if rule.handling == "value_only":
                    start, end = _value_span(text, start, end, m)
                elif rule.handling == "pem_span":
                    end = _pem_end(text, end)
                if rule.reject and any(r.fullmatch(text, start, end) for r in rule.reject):
                    continue
                if rule.handling == "pem_span":
                    pem.append((start, end))
                found.append(_Span(start, end, [(rule.tier, rule.index)]))
        for rule in line_rules:
            found.extend(_data_block_lines(rule, text, folded, pem))
        return _merge(found)

    def tokenize_text(self, text: str) -> TokenizeResult:
        spans = self.find_spans(text)
        mapping: dict[str, str] = {}
        hits: list[tuple[str, int]] = []
        out: list[str] = []
        pos = 0
        for span in spans:
            value = text[span.start : span.end]
            ph = self.placeholder(value)
            mapping.setdefault(ph, value)
            hits.extend(span.hits)
            out.append(text[pos : span.start])
            out.append(ph)
            pos = span.end
        out.append(text[pos:])
        return TokenizeResult("".join(out), mapping, hits, len(spans))

    def tokenize_json(self, body: bytes) -> TokenizeResult | None:
        """Tokenize inside the string literals of a JSON document, leaving every
        other byte untouched. Returns None when ``body`` is not UTF-8 JSON.

        Rules run on the decoded string content (as the gateway's guard does),
        so escapes like ``\\n`` or ``\\"`` cannot hide a match. Results for long
        literals are cached by keyed hash: an agent turn re-sends the whole
        conversation, so only the new messages are scanned again."""
        try:
            raw = body.decode("utf-8")
            json.loads(raw)
        except ValueError:
            return None
        literals = list(_JSON_STRING.finditer(raw))
        decoded: dict[int, str] = {}
        per_literal: dict[int, list[_Span]] = {}
        scan_idx: list[int] = []
        scan_starts: list[int] = []
        scan_parts: list[str] = []
        to_cache: dict[int, bytes] = {}
        pos = 0
        for idx, m in enumerate(literals):
            inner = raw[m.start() + 1 : m.end() - 1]
            if len(inner) >= _CACHE_MIN_CHARS and self._cache_size:
                key = hashlib.blake2b(
                    inner.encode("utf-8", "surrogatepass"), key=self._key, digest_size=16
                ).digest()
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    self.cache_hits += 1
                    if cached:
                        decoded[idx] = _decode_literal(m.group(), inner)
                        per_literal[idx] = [_Span(s, e, list(h)) for s, e, h in cached]
                    continue
                to_cache[idx] = key
            value = _decode_literal(m.group(), inner)
            decoded[idx] = value
            scan_idx.append(idx)
            scan_starts.append(pos)
            scan_parts.append(value)
            pos += len(value) + len(_LITERAL_SEP)

        if scan_parts:
            for span in self.find_spans(_LITERAL_SEP.join(scan_parts)):
                n = bisect.bisect_right(scan_starts, span.start) - 1
                idx, base = scan_idx[n], scan_starts[n]
                end = min(span.end - base, len(decoded[idx]))
                per_literal.setdefault(idx, []).append(_Span(span.start - base, end, span.hits))
        for idx, key in to_cache.items():
            spans = per_literal.get(idx, [])
            self._cache[key] = tuple((sp.start, sp.end, tuple(sp.hits)) for sp in spans)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

        if not per_literal:
            return TokenizeResult(raw, {}, [], 0)
        mapping: dict[str, str] = {}
        hits: list[tuple[str, int]] = []
        out: list[str] = []
        cursor = 0
        replaced = 0
        for idx in sorted(per_literal):
            text = decoded[idx]
            pieces: list[str] = []
            p = 0
            for span in per_literal[idx]:
                value = text[span.start : span.end]
                ph = self.placeholder(value)
                mapping.setdefault(ph, value)
                hits.extend(span.hits)
                pieces.append(text[p : span.start])
                pieces.append(ph)
                p = span.end
                replaced += 1
            pieces.append(text[p:])
            m = literals[idx]
            out.append(raw[cursor : m.start()])
            out.append(_encode_literal("".join(pieces)))
            cursor = m.end()
        out.append(raw[cursor:])
        return TokenizeResult("".join(out), mapping, hits, replaced)

    def count_new(self, body: bytes, budget: float | None = COUNT_BUDGET_SECONDS) -> NewCounts:
        """Rule matches (Tier A-D) in the newest turn of ``body``; scope
        ``none`` when it is not UTF-8 JSON. Read-only: never touches the
        scan cache or the body. Raises CountBudgetExceeded after ``budget``
        seconds of scanning (None: no limit)."""
        try:
            doc = json.loads(body.decode("utf-8"))
        except ValueError:
            return NewCounts("none")
        scope, texts = new_turn_strings(doc)
        if not texts:
            return NewCounts(scope)
        hits: list[tuple[str, int]] = []
        deadline = None if budget is None else time.monotonic() + budget
        for span in self.find_spans(_LITERAL_SEP.join(texts), self.count_rules, deadline):
            hits.extend(span.hits)
        return NewCounts(scope, hits)


def _decode_literal(literal: str, inner: str) -> str:
    return json.loads(literal) if "\\" in inner else inner


def _encode_literal(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False)
    try:
        encoded.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(value)
    return encoded


def _value_span(text: str, start: int, end: int, m: re.Match[str] | None = None) -> tuple[int, int]:
    """The value part of a key/value match: the `value` group when the rule
    has one (for keys that themselves contain `:` or `=`), else what follows
    the first `:`/`=` minus quotes and blanks; the whole match if empty."""
    if m is not None and "value" in m.re.groupindex and m.start("value") < m.end("value"):
        return m.span("value")
    sep = _VALUE_SEP.search(text, start, end)
    if sep is None:
        return start, end
    vstart, vend = sep.end(), end
    while vend > vstart and text[vend - 1] in "\"' \t":
        vend -= 1
    return (vstart, vend) if vend > vstart else (start, end)


def _pem_end(text: str, header_end: int) -> int:
    tail = _PEM_TAIL.match(text, header_end)
    if tail is not None:
        return tail.end()
    lines = _PEM_LINES.match(text, header_end)
    return lines.end() if lines is not None else header_end


class _Folded:
    """Lazily case-folded copy of the scan text with identical offsets."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._folded: str | None = None

    @property
    def text(self) -> str:
        if self._folded is None:
            lowered = self._text.lower()
            if len(lowered) != len(self._text):
                lowered = self._text.translate(_ASCII_LOWER)
            self._folded = lowered
        return self._folded


_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def _find_all(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    i = haystack.find(needle)
    while i != -1:
        out.append(i)
        i = haystack.find(needle, i + 1)
    return out


def _rule_matches(
    rule: Rule, text: str, folded: _Folded, deadline: float | None = None
) -> list[re.Match[str]]:
    """``rule.regex.finditer(text)``, but only tried near the rule's required
    literals when it has any (same leftmost, non-overlapping results). With a
    ``deadline``, checked before the rule and at every candidate position."""
    if deadline is not None and time.monotonic() > deadline:
        raise CountBudgetExceeded
    if not rule.required:
        return list(rule.regex.finditer(text))
    haystack = folded.text if rule.ignore_case else text
    positions = sorted({p for lit in rule.required for p in _find_all(haystack, lit)})
    out: list[re.Match[str]] = []
    last_end = 0
    for q in positions:
        if deadline is not None and time.monotonic() > deadline:
            raise CountBudgetExceeded
        for start in range(max(q - rule.lead, last_end), q + 1):
            m = rule.regex.match(text, start)
            if m is not None:
                out.append(m)
                last_end = max(m.end(), start + 1)
                break
    return out


def _data_block_lines(
    rule: Rule, text: str, folded: _Folded, pem: list[tuple[int, int]]
) -> list[_Span]:
    """The bare base64-line rule, never standalone: lines inside a PEM span, and
    runs of lines directly under a `data:`-style key line."""
    tag = (rule.tier, rule.index)
    accepted: list[_Span] = []
    for ps, pe in pem:
        for m in rule.regex.finditer(text, ps, pe):
            accepted.append(_Span(*_strip_blank(text, *m.span()), [tag]))
    for q in _find_all(folded.text, "data"):
        if text[q + 4 : q + 5] not in (":", '"', "'"):
            continue
        line_end = text.find("\n", q)
        if line_end == -1 or _AFTER_DATA_KEY.match(text, q + 4, line_end) is None:
            continue
        if not _is_data_key(text, folded.text, q):
            continue
        pos = line_end + 1
        while True:
            m = rule.regex.match(text, pos)
            if m is None:
                break
            accepted.append(_Span(*_strip_blank(text, *m.span()), [tag]))
            if m.end() >= len(text) or text[m.end()] != "\n":
                break
            pos = m.end() + 1
    return accepted


def _is_data_key(text: str, folded: str, q: int) -> bool:
    """`data` at q is a whole key or the suffix of `*-data` / `*_data` /
    `*.data` / `binaryData`, preceded by a line start, blank, quote, `{` or `,`."""
    k = q
    while k > 0 and text[k - 1] in _KEY_CHARS:
        k -= 1
    if k > 0 and text[k - 1] not in _KEY_BOUNDARY:
        return False
    prefix = folded[k:q]
    return prefix == "" or prefix == "binary" or prefix[-1] in "-_."


def _strip_blank(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start] in " \t":
        start += 1
    while end > start and text[end - 1] in " \t\r":
        end -= 1
    return start, end


def _merge(spans: list[_Span]) -> list[_Span]:
    spans.sort(key=lambda s: (s.start, -s.end))
    merged: list[_Span] = []
    for span in spans:
        if span.end <= span.start:
            continue
        if merged and span.start < merged[-1].end:
            last = merged[-1]
            last.end = max(last.end, span.end)
            last.hits.extend(span.hits)
        else:
            merged.append(_Span(span.start, span.end, list(span.hits)))
    return merged


def json_escaped(value: str) -> str:
    return json.dumps(value)[1:-1]


def partial_hold_point(data: str) -> int:
    """Index where a trailing proper prefix of a placeholder starts (len if none)."""
    n = len(data)
    i = data.find("<", max(0, n - MAX_HOLD))
    while i != -1:
        if PARTIAL_RE.fullmatch(data, i):
            return i
        i = data.find("<", i + 1)
    return n


class Restorer:
    """Streaming, byte-level placeholder restorer.

    ``feed`` returns everything that can no longer be part of a placeholder and
    holds back at most ``MAX_HOLD`` (22) bytes: a trailing proper prefix of
    ``<<secret-XXXXXXXXXXXX>>``. Known placeholders are replaced by the
    JSON-escaped original (they sit inside JSON string literals); unknown ones
    pass through unchanged. ``flush`` releases the held tail at end of stream.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._repl = {ph.encode(): json_escaped(v).encode() for ph, v in mapping.items()}
        self._held = b""
        self.restored = 0
        self.max_held = 0
        self.errors = 0

    @property
    def held(self) -> int:
        return len(self._held)

    def _sub(self, m: re.Match[bytes]) -> bytes:
        repl = self._repl.get(m.group())
        if repl is None:
            return m.group()
        self.restored += 1
        return repl

    def _hold_point(self, data: bytes) -> int:
        n = len(data)
        i = data.find(b"<", max(0, n - MAX_HOLD))
        while i != -1:
            if PARTIAL_RE_B.fullmatch(data, i):
                return i
            i = data.find(b"<", i + 1)
        return n

    def feed(self, chunk: bytes) -> bytes:
        data = self._held + chunk if self._held else chunk
        cut = self._hold_point(data)
        head = data[:cut]
        if b"<<secret-" in head:
            head = PLACEHOLDER_RE_B.sub(self._sub, head)
        self._held = data[cut:]
        self.max_held = max(self.max_held, len(self._held))
        return head

    def flush(self) -> bytes:
        tail, self._held = self._held, b""
        return tail

    def abandon(self, chunk: bytes) -> bytes:
        """Fail-open exit: the held bytes and ``chunk``, unmodified."""
        out, self._held = self._held + chunk, b""
        return out
