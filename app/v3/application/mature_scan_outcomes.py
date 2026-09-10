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

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from app.v3.application.miss_audit import MissAuditService
from app.v3.application.outcome_label import OutcomeLabelService
from app.v3.application.shadow_pool import ShadowPoolService
from app.v3.domain.candidate_engine import OutcomeLabelResult

__all__ = ["MatureScanOutcomesService"]

_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")

# R2.1-P1-03：批量补跑的保守自然日上界——20 个未来交易日最长可跨
# 春节等长假（>40 自然日），45 天多抓无害（观察窗不足自然保持
# PENDING，已成熟行不可变）。可用 env 覆盖。
_MATURE_LOOKBACK_CALENDAR_DAYS = 45
_DEFAULT_BATCH_LIMIT = 10


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
        matured = 0
        for view in views:
            series = bars.get(view.security_id) or []
            close_t, future = self._split_t_day(series, t_ordinal)
            label = self._labels.evaluate(
                view.code, close_t, future, security_id=view.security_id,
            )
            # R2.1-P0-03 §5.6：按 status 计数——成熟负样本（MATURED/NONE）
            # 不再被误计为 pending
            if label.status == "PENDING":
                pending += 1
            else:
                matured += 1
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
            "matured": matured,
            "pending": pending,
            "miss_audit_rows": saved_miss,
            "shadow_rows": saved_shadow,
            # P0-10：影子池不足 200 时显式上报，不静默
            "shadow_shortfall_reason": self._shadow.last_shortfall_reason,
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

    async def execute_pending(
        self,
        uow_factory,
        *,
        as_of: datetime | None = None,
        batch_limit: int | None = None,
    ) -> dict:
        """R2.1-P1-03（任务书 §11）：批量补跑所有仍 PENDING 的历史 scan。

        - 绝不只成熟 latest：查所有 PUBLISHED 且"仍 PENDING"（无 outcome
          行或存在 NULL label 行）的 scan，此前成熟失败的今日必须能补跑；
        - 候选窗口 = as_of − 45 自然日（20 未来交易日的保守上界，多抓
          无害：观察窗不足保持 PENDING，已成熟行由 upsert WHERE 兜底
          不可变）；
        - batch_limit 按 scan 分批（env V3_CANDIDATE_MATURE_BATCH_LIMIT
          可覆盖），单 scan 失败隔离记 errors，不阻断整批；
        - 每 scan 独立 UoW（逐 scan 提交，与维护链其它 Job 一致）。
        """
        as_of = as_of or datetime.now(timezone.utc)
        limit = batch_limit or int(
            os.getenv("V3_CANDIDATE_MATURE_BATCH_LIMIT", _DEFAULT_BATCH_LIMIT)
        )
        older_than = as_of - timedelta(days=_MATURE_LOOKBACK_CALENDAR_DAYS)
        async with uow_factory() as uow:
            scan_ids = await uow.scans.pending_mature_scan_ids(
                older_than=older_than, limit=limit,
            )
        summaries: list[dict] = []
        errors: list[dict[str, str]] = []
        for scan_id in scan_ids:
            try:
                async with uow_factory() as uow:
                    summary = await self.execute(uow.scans, scan_id=scan_id)
                    await uow.commit()
            except Exception as exc:  # noqa: BLE001 —— 单 scan 失败隔离
                errors.append({
                    "scan_run_id": str(scan_id),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            summaries.append(summary)
        matured_scans = sum(
            1 for item in summaries if item.get("status") == "ok"
            and item.get("pending", 0) == 0
        )
        return {
            "status": "ok",
            "candidate_count": len(scan_ids),
            "processed_count": len(summaries),
            "matured_scans": matured_scans,
            "pending_count": len(summaries) - matured_scans,
            "matured_labels": sum(
                item.get("matured", 0) for item in summaries
            ),
            "pending_labels": sum(
                item.get("pending", 0) for item in summaries
            ),
            "error_count": len(errors),
            "errors": errors,
        }


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
