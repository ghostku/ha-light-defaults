"""Turning raw values (from a schedule block, which only holds plain values)
into what a light takes."""

import re
from typing import Any


def parse_color(value: Any) -> tuple[int, int, int] | None:
    """An RGB color from "#8c00ff" or "140,0,255"; None if it's neither."""
    text = str(value).strip()
    if m := re.fullmatch(r"#([0-9a-fA-F]{6})", text):
        hex_ = m.group(1)
        return int(hex_[0:2], 16), int(hex_[2:4], 16), int(hex_[4:6], 16)
    if m := re.fullmatch(r"(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", text):
        rgb = tuple(int(part) for part in m.groups())
        if all(part <= 255 for part in rgb):
            return rgb  # type: ignore[return-value]
    return None


def whole_number(value: Any, low: int, high: int) -> int | None:
    """A whole number in low–high from a number or numeric string; None
    otherwise (true/false don't count as numbers)."""
    if isinstance(value, bool):
        return None
    try:
        number = round(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if low <= number <= high else None
