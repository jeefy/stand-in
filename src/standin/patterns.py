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


@dataclass(frozen=True)
class Rule:
    tier: str
    index: int
    pattern: str
    handling: Handling
    regex: re.Pattern[str]
    required: tuple[str, ...] = ()
    lead: int = 0

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
            pattern = entry["pattern"]
            handling = entry.get("handling", "whole")
            if handling not in HANDLINGS:
                raise ValueError(f"tier {tier} rule {index}: unknown handling {handling!r}")
            required = tuple(entry.get("required") or ())
            rule = Rule(
                tier,
                index,
                pattern,
                cast(Handling, handling),
                re.compile(pattern),
                tuple(r.lower() for r in required) if pattern.startswith("(?i)") else required,
                int(entry.get("lead", 0)),
            )
            rules.append(rule)
    if not rules:
        raise ValueError("patterns.yaml has no rules")
    return rules
