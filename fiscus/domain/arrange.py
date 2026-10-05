"""Putting things in an order by hand: one step up or down at a time."""

from __future__ import annotations


def moved(ids: list[int], ident: int, step: int) -> list[int]:
    """`ids` with `ident` swapped with its neighbour `step` (-1 up, 1 down)
    away. Unchanged at either end, or when `ident` is not there."""
    out = list(ids)
    if ident not in out:
        return out
    i = out.index(ident)
    j = i + step
    if step not in (-1, 1) or not 0 <= j < len(out):
        return out
    out[i], out[j] = out[j], out[i]
    return out
