from __future__ import annotations

from datetime import datetime

from app.utils.time import SHANGHAI, now_shanghai


class DataQualityService:
    """The single authority for timestamps, freshness, IDs and confidence."""

    def __init__(self, stale_after: int = 30, old_after: int = 60, unavailable_after: int = 300) -> None:
        if not 0 <= stale_after <= old_after <= unavailable_after:
            raise ValueError("quality thresholds must be ordered")
        self.stale_after = stale_after
        self.old_after = old_after
        self.unavailable_after = unavailable_after

    @staticmethod
    def snapshot_id(source_timestamp: datetime) -> str:
        normalized = source_timestamp.astimezone(SHANGHAI)
        return f"snapshot-{normalized.strftime('%Y%m%dT%H%M%S.%f')[:-3]}"

    @staticmethod
    def scan_id(source_timestamp: datetime) -> str:
        normalized = source_timestamp.astimezone(SHANGHAI)
        return f"scan-{normalized.strftime('%Y%m%dT%H%M%S.%f')[:-3]}"

    def assess(
        self,
        source_timestamp: datetime,
        *,
        timestamp_source: str = "eastmoney",
        complete: bool = True,
        conflict: bool = False,
        server_timestamp: datetime | None = None,
        source: str = "eastmoney",
    ) -> dict:
        observed_at = server_timestamp or now_shanghai()
        if source_timestamp.tzinfo is None:
            source_timestamp = source_timestamp.replace(tzinfo=SHANGHAI)
        source_timestamp = source_timestamp.astimezone(SHANGHAI)
        age = max(0.0, (observed_at - source_timestamp).total_seconds())
        if conflict:
            quality = "CONFLICT"
        elif age <= self.stale_after:
            quality = "LIVE"
        elif age <= self.old_after:
            quality = "STALE"
        elif age <= self.unavailable_after:
            quality = "OLD"
        else:
            quality = "UNAVAILABLE"

        if quality in {"UNAVAILABLE", "OLD", "CONFLICT"}:
            confidence = "LOW"
        elif not complete or timestamp_source == "fetch_time":
            confidence = "MEDIUM"
        else:
            confidence = "HIGH"

        return {
            "source": source,
            "source_timestamp": source_timestamp,
            "data_timestamp": source_timestamp,
            "server_timestamp": observed_at,
            "age_seconds": round(age, 3),
            "stale": quality != "LIVE",
            "quality": quality,
            "timestamp_source": timestamp_source,
            "snapshot_id": self.snapshot_id(source_timestamp),
            "confidence": confidence,
        }


default_data_quality_service = DataQualityService()
