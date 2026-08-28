from __future__ import annotations

import re


def validate_code(code: str) -> str:
    value = code.strip()
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("code must contain exactly 6 digits")
    return value


def market_of(code: str) -> str:
    code = validate_code(code)
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if code.startswith(("5", "6", "9")):
        return "SH"
    if code.startswith(("0", "1", "2", "3")):
        return "SZ"
    raise ValueError(f"unsupported security code: {code}")
