"""RT-08：EOD 流水线最新状态聚合（实时方案 §19/§27 RT-08）。

按 job 聚合 orchestrator 最近一次运行结果，给出整体 COMPLETED/PARTIAL/FAILED。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable


class PipelineEodLatestService:
    def __init__(
        self, uow_factory: Callable[[], Any], *, clock: Callable[[], datetime]
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(self, *, limit: int = 50) -> dict[str, Any]:
        now = self._clock()
        async with self._uow_factory() as uow:
            rows = await uow.orchestrator.latest_runs(limit=limit)
        jobs: dict[str, dict[str, Any]] = {}
        statuses: list[str] = []
        for row in rows:
            job_id = row.get("job_id")
            if not job_id or job_id in jobs:
                continue
            jobs[job_id] = row
            statuses.append(str(row.get("status", "")))
        if statuses and all(s == "SUCCEEDED" for s in statuses):
            overall = "COMPLETED"
        elif statuses and all(s in ("FAILED",) for s in statuses):
            overall = "FAILED"
        else:
            overall = "PARTIAL"
        return {
            "source": "pipeline-eod-latest-v1",
            "known_at": now,
            "jobs": jobs,
            "overall": overall,
            "run_count": len(rows),
        }
