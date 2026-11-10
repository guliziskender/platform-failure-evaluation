"""Kubernetes resource quantities ("500m", "4k", "128Mi") as numbers."""

from __future__ import annotations

import re

_QUANTITY = re.compile(r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)([a-zA-Z]*)$")
_MULTIPLIERS = {
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "": 1,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}


def parse_quantity(value: str | int | float | None) -> float | None:
    """'500m' -> 0.5, '4k' -> 4000, '128Mi' -> 134217728. None if unparseable."""
    if value is None:
        return None
    m = _QUANTITY.match(str(value).strip())
    if not m or m.group(2) not in _MULTIPLIERS:
        return None
    return float(m.group(1)) * _MULTIPLIERS[m.group(2)]


def format_bytes(n: float) -> str:
    """Largest binary unit that keeps a whole number: 268435456 -> '256Mi'."""
    for unit in ("Ei", "Pi", "Ti", "Gi", "Mi", "Ki"):
        size = _MULTIPLIERS[unit]
        if n >= size and n % size == 0:
            return f"{int(n // size)}{unit}"
    return f"{int(n)}"
