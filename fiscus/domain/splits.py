"""Splitting a shared bill.

Modes:
  equal    everyone pays the same; leftover cents go to the owner first
  percent  each share is basis points of the total (10000 = 100 %), summing to 10000
  fixed    each share is a cents amount, summing to the bill

The shares always add up to the bill exactly.
"""

from __future__ import annotations

from dataclasses import dataclass


class SplitError(ValueError):
    pass


@dataclass(frozen=True)
class ShareSpec:
    user_id: int
    value: int = 0


def compute(total_cents: int, mode: str, shares: list[ShareSpec], owner_id: int) -> dict[int, int]:
    if not shares:
        raise SplitError('A bill needs at least one participant')
    ids = [s.user_id for s in shares]
    if len(set(ids)) != len(ids):
        raise SplitError('Each person can only be in a bill once')
    if owner_id not in ids:
        raise SplitError('The owner must hold a share')
    # The owner absorbs rounding first, then the others in order.
    ordered = sorted(shares, key=lambda s: (s.user_id != owner_id, ids.index(s.user_id)))
    sign = -1 if total_cents < 0 else 1
    total = abs(total_cents)

    if mode == 'equal':
        base, extra = divmod(total, len(ordered))
        out = {s.user_id: base + (1 if i < extra else 0) for i, s in enumerate(ordered)}
    elif mode == 'percent':
        if any(s.value < 0 for s in shares):
            raise SplitError('Percentages cannot be negative')
        if sum(s.value for s in shares) != 10000:
            raise SplitError('Percentages must add up to 100')
        raw = {s.user_id: total * s.value // 10000 for s in ordered}
        remainders = sorted(ordered, key=lambda s: (-(total * s.value % 10000), ordered.index(s)))
        left = total - sum(raw.values())
        for s in remainders[:left]:
            raw[s.user_id] += 1
        out = raw
    elif mode == 'fixed':
        if any(s.value < 0 for s in shares):
            raise SplitError('Amounts cannot be negative')
        if sum(s.value for s in shares) != total:
            raise SplitError('Fixed amounts must add up to the bill amount')
        out = {s.user_id: s.value for s in ordered}
    else:
        raise SplitError(f'Unknown split mode {mode!r}')

    assert sum(out.values()) == total
    return {uid: sign * v for uid, v in out.items()}
