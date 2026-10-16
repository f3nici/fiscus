"""Categorisation rules: "description contains X" or a regex, highest priority first."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RuleSpec:
    id: int
    pattern: str
    is_regex: bool
    category_id: int
    priority: int


class Matcher:
    def __init__(self, rules: list[RuleSpec]) -> None:
        # Highest priority first, then the oldest rule.
        ordered = sorted(rules, key=lambda r: (-r.priority, r.id))
        self._compiled: list[tuple[RuleSpec, re.Pattern[str] | None, str]] = []
        for r in ordered:
            if r.is_regex:
                self._compiled.append((r, re.compile(r.pattern, re.IGNORECASE), ''))
            else:
                self._compiled.append((r, None, r.pattern.casefold()))

    def category_for(self, description: str) -> int | None:
        folded = description.casefold()
        for rule, rx, needle in self._compiled:
            if rx is not None:
                if rx.search(description):
                    return rule.category_id
            elif needle in folded:
                return rule.category_id
        return None
