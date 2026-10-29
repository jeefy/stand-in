"""Load the Tier A / Tier B rules from patterns.yaml."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

Handling = Literal["whole", "value_only", "pem_span", "data_block_line"]
HANDLINGS: frozenset[str] = frozenset({"whole", "value_only", "pem_span", "data_block_line"})
TIERS: tuple[str, ...] = ("a", "b")

PATTERNS_ENV = "STANDIN_PATTERNS_FILE"

# Automatic prefilter: a rule without an explicit `required` gets the literal
# prefixes its regex must start with, when there are at most this many and each
# is at least this long. Otherwise the rule runs unfiltered (plain finditer).
MAX_PREFILTER_LITERALS = 16
MIN_PREFILTER_LEN = 3


@dataclass(frozen=True)
class Rule:
    tier: str
    index: int
    pattern: str
    handling: Handling
    regex: re.Pattern[str]
    required: tuple[str, ...] = ()
    lead: int = 0
    # Fullmatched against the value to replace (after value_only extraction);
    # any hit drops the span.
    reject: tuple[re.Pattern[str], ...] = ()
    # Drop matches that start right after an ASCII letter or digit.
    key_boundary: bool = False

    @property
    def ignore_case(self) -> bool:
        return self.pattern.startswith("(?i)")


def default_patterns_path() -> Path:
    """$STANDIN_PATTERNS_FILE, else patterns.yaml next to the module (wheel
    install; force-included by pyproject.toml), else at the repository root
    (editable install / source checkout)."""
    override = os.environ.get(PATTERNS_ENV)
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for candidate in (here.with_name("patterns.yaml"), here.parents[2] / "patterns.yaml"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("patterns.yaml not found; set " + PATTERNS_ENV)


def load_raw(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    data = yaml.safe_load((path or default_patterns_path()).read_text())
    if not isinstance(data, dict):
        raise ValueError("patterns.yaml must be a mapping")
    return {tier: list(data.get(f"tier_{tier}") or []) for tier in TIERS}


def load_rules(path: Path | None = None) -> list[Rule]:
    rules: list[Rule] = []
    for tier, entries in load_raw(path).items():
        for index, entry in enumerate(entries):
            rules.append(_build_rule(tier, index, entry))
    if not rules:
        raise ValueError("patterns.yaml has no rules")
    return rules


def _build_rule(tier: str, index: int, entry: Any) -> Rule:
    where = f"tier {tier} rule {index}"
    if not isinstance(entry, dict) or not isinstance(entry.get("pattern"), str):
        raise ValueError(f"{where}: expected a mapping with a string `pattern`")
    pattern: str = entry["pattern"]
    handling = entry.get("handling", "whole")
    if handling not in HANDLINGS:
        raise ValueError(f"{where}: unknown handling {handling!r}")
    if "required" in entry:
        required = _str_list(entry["required"], f"{where}: required")
        if not all(required):
            raise ValueError(f"{where}: required literals must be non-empty")
    else:
        required = derive_required(pattern)
    if pattern.startswith("(?i)"):
        required = tuple(dict.fromkeys(lit.lower() for lit in required))
    lead = entry.get("lead", 0)
    if not isinstance(lead, int) or isinstance(lead, bool) or lead < 0:
        raise ValueError(f"{where}: lead must be a non-negative integer")
    key_boundary = entry.get("key_boundary", False)
    if not isinstance(key_boundary, bool):
        raise ValueError(f"{where}: key_boundary must be a boolean")
    reject = tuple(re.compile(r) for r in _str_list(entry.get("reject"), f"{where}: reject"))
    return Rule(
        tier,
        index,
        pattern,
        cast(Handling, handling),
        re.compile(pattern),
        required,
        lead,
        reject,
        key_boundary,
    )


def _str_list(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{what} must be a list of strings")
    return tuple(value)


class _TooMany(Exception):
    pass


def derive_required(pattern: str) -> tuple[str, ...]:
    """Literal prefixes that every match of ``pattern`` starts with (lowercased
    for a leading ``(?i)``), or () when that would need a literal shorter than
    ``MIN_PREFILTER_LEN``.

    Understood: plain characters, escaped punctuation, ``\\xHH``, ``(?:..)``
    / ``(..)`` / ``(?P<n>..)`` groups, top-level alternation, character
    classes, and the quantifiers ``?``, ``*``, ``+``, ``{m,n}`` on any of
    those, as long as the expansion stays within ``MAX_PREFILTER_LITERALS``.
    Anything else ends the prefix. A match starts with one of the literals, so
    ``lead`` is 0."""
    ignore_case = pattern.startswith("(?i)")
    body = pattern[4:] if ignore_case else pattern
    try:
        prefixes, end, _ = _alternation(body, 0)
    except (_TooMany, IndexError, ValueError):
        return ()
    if end != len(body):
        return ()
    if ignore_case:
        prefixes = {p.lower() for p in prefixes}
    # A literal that extends another one finds no additional positions; a
    # common prefix long enough to be selective saves one scan per literal.
    minimal = sorted(p for p in prefixes if not any(o != p and p.startswith(o) for o in prefixes))
    common = os.path.commonprefix(minimal) if minimal else ""
    if len(minimal) > 1 and len(common) >= MIN_PREFILTER_LEN:
        minimal = [common]
    if not minimal or any(len(p) < MIN_PREFILTER_LEN for p in minimal):
        return ()
    return tuple(minimal)


def _cross(left: set[str], right: set[str]) -> set[str]:
    out = {a + b for a in left for b in right}
    if len(out) > MAX_PREFILTER_LITERALS:
        raise _TooMany
    return out


_REPEAT = re.compile(r"\{([0-9]*)(?:,[0-9]*)?\}")


def _alternation(p: str, i: int) -> tuple[set[str], int, bool]:
    """Branches from ``i`` up to an unmatched ``)`` or the end: the union of
    their prefixes, that position, and whether every branch was literal to its
    end (only then may what follows the group extend the prefixes)."""
    out: set[str] = set()
    complete = True
    while True:
        prefixes, i, done = _sequence(p, i)
        out |= prefixes
        if len(out) > MAX_PREFILTER_LITERALS:
            raise _TooMany
        if not done:
            complete = False
            i = _skip_branch(p, i)
        if i < len(p) and p[i] == "|":
            i += 1
            continue
        return out, i, complete


def _sequence(p: str, i: int) -> tuple[set[str], int, bool]:
    """Literal prefixes of one branch; an atom that would push the expansion
    past the cap ends the prefix before it instead."""
    prefixes = {""}
    while True:
        if i >= len(p) or p[i] in "|)":
            return prefixes, i, True
        try:
            atom = _atom(p, i)
        except _TooMany:
            atom = None
        if atom is None:
            return prefixes, i, False
        chars, j, complete = atom
        quant = p[j] if j < len(p) else ""
        if quant == "{":
            rep = _REPEAT.match(p, j)
            if rep is None:
                return prefixes, i, False
            optional = int(rep.group(1) or 0) == 0
        else:
            optional = quant in ("?", "*")
        try:
            extended = _cross(prefixes, chars | {""} if optional else chars)
        except _TooMany:
            return prefixes, i, False
        if not complete or quant in ("*", "+", "{"):
            return extended, j, False
        prefixes = extended
        i = j
        if quant == "?":
            i += 2 if p[j + 1 : j + 2] == "?" else 1


def _atom(p: str, i: int) -> tuple[set[str], int, bool] | None:
    c = p[i]
    if c == "(":
        if p.startswith("(?:", i):
            k = i + 3
        elif p.startswith("(?P<", i):
            k = p.index(">", i) + 1
        elif p.startswith("(?", i):
            return None
        else:
            k = i + 1
        inner, k, complete = _alternation(p, k)
        if k >= len(p) or p[k] != ")":
            return None
        return inner, k + 1, complete
    if c == "[":
        cls = _char_class(p, i)
        return None if cls is None else (cls[0], cls[1], True)
    if c == "\\":
        esc = _escape(p, i)
        return None if esc is None else ({esc[0]}, esc[1], True)
    if c in ".^$*+?{}[]|)":
        return None
    return {c}, i + 1, True


def _escape(p: str, i: int) -> tuple[str, int] | None:
    if i + 1 >= len(p):
        return None
    nxt = p[i + 1]
    if nxt == "x":
        digits = p[i + 2 : i + 4]
        if len(digits) == 2 and all(d in "0123456789abcdefABCDEF" for d in digits):
            return chr(int(digits, 16)), i + 4
        return None
    if nxt.isalnum() or nxt == "_" or nxt.isspace():
        return None
    return nxt, i + 2


def _char_class(p: str, i: int) -> tuple[set[str], int] | None:
    j = i + 1
    if j < len(p) and p[j] == "^":
        return None
    chars: set[str] = set()
    first = True
    while j < len(p):
        c = p[j]
        if c == "]" and not first:
            return chars, j + 1
        first = False
        if c == "[":
            return None
        if c == "\\":
            esc = _escape(p, j)
            if esc is None:
                return None
            lo, j = esc
        else:
            lo, j = c, j + 1
        if j + 1 < len(p) and p[j] == "-" and p[j + 1] != "]":
            if p[j + 1] == "\\":
                esc = _escape(p, j + 1)
                if esc is None:
                    return None
                hi, j = esc
            elif p[j + 1] == "[":
                return None
            else:
                hi, j = p[j + 1], j + 2
            if not 0 <= ord(hi) - ord(lo) < MAX_PREFILTER_LITERALS:
                return None
            chars.update(chr(x) for x in range(ord(lo), ord(hi) + 1))
        else:
            chars.add(lo)
        if len(chars) > MAX_PREFILTER_LITERALS:
            return None
    return None


def _skip_branch(p: str, i: int) -> int:
    depth = 0
    while i < len(p):
        c = p[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i = _skip_class(p, i)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return i
            depth -= 1
        elif c == "|" and depth == 0:
            return i
        i += 1
    return i


def _skip_class(p: str, i: int) -> int:
    j = i + 1
    if j < len(p) and p[j] == "^":
        j += 1
    if j < len(p) and p[j] == "]":
        j += 1
    while j < len(p) and p[j] != "]":
        j += 2 if p[j] == "\\" else 1
    return j + 1
