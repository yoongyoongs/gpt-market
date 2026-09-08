"""Step 17-18：mature 编排——扫描 T 日后 20 交易日回填结果标签与审计。

流程（设计 §26/§30/§31）：
1. resolve 目标扫描（latest 或指定 scan_id）
2. 从落库快照重建 TraceView（不依赖内存 pipeline result）
3. 批量拉 QFQ 日K：T 日首根作 close_T 基准，其后 20 根为观察窗
4. OutcomeLabelService 逐股评级（观察窗不足/停牌 → PENDING 不猜）
5. upsert outcome_labels；重算 miss_audit_rows / shadow_pool_rows（幂等覆盖）

调用方负责 UoW 提交；T 日无 bar（停牌）→ label=None PENDING。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from app.v3.application.miss_audit import MissAuditService
from app.v3.application.outcome_label import OutcomeLabelService
from app.v3.application.shadow_pool import ShadowPoolService
from app.v3.domain.candidate_engine import OutcomeLabelResult

__all__ = ["MatureScanOutcomesService"]

_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


def t_day_window(market_date: datetime) -> tuple[datetime, int]:
    """T 日（上海时区）起点的 UTC 时刻与 T 日日期序号。"""
    t_day = market_date.astimezone(_TZ_SHANGHAI).date()
    start = datetime(t_day.year, t_day.month, t_day.day, tzinfo=_TZ_SHANGHAI)
    return start.astimezone(timezone.utc), t_day.toordinal()


class MatureScanOutcomesService:
    def __init__(
        self,
        label_service: OutcomeLabelService | None = None,
        miss_service: MissAuditService | None = None,
        shadow_service: ShadowPoolService | None = None,
    ) -> None:
        self._labels = label_service or OutcomeLabelService()
        self._miss = miss_service or MissAuditService()
        self._shadow = shadow_service or ShadowPoolService()

    async def execute(
        self,
        scans,  # SQLAlchemyScanRepository
        *,
        scan_id: UUID | None = None,
    ) -> dict:
        run = await scans.run_by_id(scan_id) if scan_id else await scans.latest_run()
        if run is None:
            return {"status": "no_run"}

        views = await scans.trace_views(run.scan_run_id)
        since, t_ordinal = t_day_window(run.market_date)
        bars = await scans.bars_from(
            [view.security_id for view in views], since,
        )

        labels: list[OutcomeLabelResult] = []
        pending = 0
        for view in views:
            series = bars.get(view.security_id) or []
            close_t, future = self._split_t_day(series, t_ordinal)
            label = self._labels.evaluate(
                view.code, close_t, future, security_id=view.security_id,
            )
            if label.label is None:
                pending += 1
            labels.append(label)

        miss_entries = self._miss.collect(views, labels)
        shadow_entries = self._shadow.sample(views)

        saved_labels = await scans.save_outcome_labels(run.scan_run_id, labels)
        saved_miss = await scans.save_miss_rows(run.scan_run_id, miss_entries)
        saved_shadow = await scans.save_shadow_rows(
            run.scan_run_id,
            shadow_entries,
            {entry.code: entry.label for entry in labels},
        )
        return {
            "status": "ok",
            "scan_run_id": str(run.scan_run_id),
            "market_date": run.market_date.isoformat(),
            "labels_upserted": saved_labels,
            "labeled": saved_labels - pending,
            "pending": pending,
            "miss_audit_rows": saved_miss,
            "shadow_rows": saved_shadow,
        }

    @staticmethod
    def _split_t_day(
        series: list[tuple[datetime, float, float, float]],
        t_ordinal: int,
    ) -> tuple[float | None, list[tuple[float, float]]]:
        """首根若为 T 日（上海时区）→ 取 close 作基准，余下为观察窗。

        T 日停牌（首根晚于 T 日）→ 基准缺失，宁可 PENDING 不猜。
        """
        if not series:
            return None, []
        first_time = series[0][0]
        if first_time.astimezone(_TZ_SHANGHAI).date().toordinal() != t_ordinal:
            return None, []
        close_t = series[0][3]
        future = [(high, low) for _, high, low, _ in series[1:]]
        return close_t, future


async def run_mature_backfill(scan_id: UUID | None = None) -> dict:
    """带 UoW 的便捷入口（Step 20 扫描编排尾步 / 手动 CLI 均可用）。"""
    from app.container import container

    if not container.v3.enabled:
        return {"status": "v3_disabled"}
    service = MatureScanOutcomesService()
    async with container.v3.uow() as uow:
        summary = await service.execute(uow.scans, scan_id=scan_id)
        if summary.get("status") == "ok":
            await uow.commit()
    return summary
