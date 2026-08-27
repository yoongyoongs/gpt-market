from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def freshness(
    data_timestamp: datetime,
    *,
    timestamp_source: str = "eastmoney",
    complete: bool = True,
    stale_after: int = 30,
    old_after: int = 60,
    unavailable_after: int = 300,
) -> dict:
    # Compatibility entry point for domain code; all decisions still live in one service.
    from app.services.data_quality import DataQualityService

    return DataQualityService(stale_after, old_after, unavailable_after).assess(
        data_timestamp, timestamp_source=timestamp_source, complete=complete
    )
