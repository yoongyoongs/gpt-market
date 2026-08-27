from __future__ import annotations

from typing import Any


def serialize_business(value: Any) -> Any:
    """Canonical transport-neutral serialization for MCP and Web adapters."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [serialize_business(item) for item in value]
    return value
