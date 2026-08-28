from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.serialization import serialize_business
from app.utils.time import SHANGHAI, now_shanghai


def _snapshot_date(value: Any) -> str:
    timestamp = getattr(value, "server_timestamp", None) or now_shanghai()
    if isinstance(timestamp, datetime):
        return timestamp.astimezone(SHANGHAI).date().isoformat()
    return now_shanghai().date().isoformat()


def _record(score_version: str, result: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "saved_at": now_shanghai().isoformat(),
        "date": _snapshot_date(result),
        "score_version": score_version,
        "scan_id": getattr(result, "scan_id", None),
        "inputs": inputs,
        "result": serialize_business(result),
        "future_return_placeholders": {
            "t_plus_1": None,
            "t_plus_3": None,
            "t_plus_5": None,
            "t_plus_10": None,
            "t_plus_20": None,
            "max_upside": None,
            "max_drawdown": None,
            "stop_loss_hit": None,
            "target_1_hit": None,
            "target_2_hit": None,
        },
    }


def _append(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


async def save_scan_snapshot(score_version: str, result: Any, inputs: dict[str, Any]) -> None:
    root = Path(get_settings().scan_history_path)
    path = root / f"{_snapshot_date(result)}.jsonl"
    await asyncio.to_thread(_append, path, _record(score_version, result, inputs))
