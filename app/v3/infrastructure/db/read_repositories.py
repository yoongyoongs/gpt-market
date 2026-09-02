"""RC-08B READ Contract 仓储（API-002）：只读投影，不改任何状态。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.v3.infrastructure.db.models import (
    AccountModel,
    DecisionModel,
    EntryPlanModel,
    FeatureRunModel,
    BarSeriesRevisionModel,
    MarketReviewModel,
    PerformanceAttributionModel,
    PerformanceSummaryModel,
    PortfolioAdjustmentModel,
    PortfolioPreferenceModel,
    PositionProjectionModel,
    PositionReviewModel,
    ReviewModel,
    SecurityModel,
    WatchlistEventModel,
    WatchlistModel,
)


def _columns(row) -> dict:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class SQLAlchemyReadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def security_id_by_code(self, market: str, code: str):
        """RT-06：按 (market, code) 查 securities 主键。"""
        return await self._session.scalar(
            select(SecurityModel.security_id).where(
                SecurityModel.market == market,
                SecurityModel.code == code,
            )
        )

    async def portfolio_overview(self, limit: int) -> dict:
        accounts = (await self._session.scalars(
            select(AccountModel).order_by(AccountModel.created_at).limit(limit)
        )).all()
        positions = (await self._session.execute(
            select(PositionProjectionModel, SecurityModel)
            .join(SecurityModel, SecurityModel.security_id == PositionProjectionModel.security_id)
        )).all()
        by_account: dict[UUID, list] = {}
        for projection, security in positions:
            by_account.setdefault(projection.account_id, []).append({
                **_columns(projection),
                "security_code": security.code, "security_market": security.market,
            })
        return {"accounts": [
            {**_columns(account), "positions": by_account.get(account.account_id, [])}
            for account in accounts
        ]}

    async def position_reviews_by_code(self, code: str, limit: int) -> list[dict]:
        rows = (await self._session.execute(
            select(PositionReviewModel, SecurityModel)
            .join(SecurityModel, SecurityModel.security_id == PositionReviewModel.security_id)
            .where(SecurityModel.code == code)
            .order_by(PositionReviewModel.as_of.desc())
            .limit(limit)
        )).all()
        return [
            {**_columns(review), "security_code": security.code,
             "security_market": security.market}
            for review, security in rows
        ]

    async def adjustments_by_code(self, code: str, limit: int) -> list[dict]:
        rows = (await self._session.execute(
            select(PortfolioAdjustmentModel, SecurityModel)
            .join(SecurityModel, SecurityModel.security_id == PortfolioAdjustmentModel.security_id)
            .where(SecurityModel.code == code)
            .order_by(PortfolioAdjustmentModel.effective_time.desc())
            .limit(limit)
        )).all()
        return [
            {**_columns(adjustment), "security_code": security.code,
             "security_market": security.market}
            for adjustment, security in rows
        ]

    async def preferences(self, limit: int) -> list[dict]:
        rows = (await self._session.execute(
            select(PortfolioPreferenceModel, AccountModel)
            .join(AccountModel, AccountModel.account_id == PortfolioPreferenceModel.account_id)
            .order_by(PortfolioPreferenceModel.effective_from.desc())
            .limit(limit)
        )).all()
        return [
            {**_columns(preference), "account_name": account.name}
            for preference, account in rows
        ]

    async def entry_plan_versions(self, entry_plan_id: UUID) -> list[dict]:
        plan = await self._session.get(EntryPlanModel, entry_plan_id)
        if plan is None:
            return []
        rows = (await self._session.scalars(
            select(EntryPlanModel).where(
                EntryPlanModel.decision_id == plan.decision_id
            ).order_by(EntryPlanModel.version)
        )).all()
        return [_columns(item) for item in rows]

    async def watchlist_changes(self, limit: int) -> list[dict]:
        rows = (await self._session.execute(
            select(WatchlistEventModel, WatchlistModel, SecurityModel)
            .join(WatchlistModel, WatchlistModel.watchlist_id == WatchlistEventModel.watchlist_id)
            .join(SecurityModel, SecurityModel.security_id == WatchlistModel.security_id)
            .order_by(WatchlistEventModel.event_time.desc())
            .limit(limit)
        )).all()
        return [
            {**_columns(event), "current_state": watchlist.state,
             "security_code": security.code, "security_market": security.market}
            for event, watchlist, security in rows
        ]

    async def decisions(self, limit: int) -> list[dict]:
        rows = (await self._session.execute(
            select(DecisionModel, SecurityModel)
            .join(SecurityModel, SecurityModel.security_id == DecisionModel.security_id)
            .order_by(DecisionModel.produced_at.desc())
            .limit(limit)
        )).all()
        return [
            {**_columns(decision), "security_code": security.code,
             "security_market": security.market}
            for decision, security in rows
        ]

    async def reviews(self, limit: int) -> list[dict]:
        rows = (await self._session.scalars(
            select(ReviewModel).order_by(ReviewModel.as_of.desc()).limit(limit)
        )).all()
        return [_columns(item) for item in rows]

    async def market_reviews(self, limit: int) -> list[dict]:
        rows = (await self._session.scalars(
            select(MarketReviewModel).order_by(MarketReviewModel.as_of.desc()).limit(limit)
        )).all()
        return [_columns(item) for item in rows]

    async def performance(self, limit: int) -> dict:
        attributions = (await self._session.scalars(
            select(PerformanceAttributionModel)
            .order_by(PerformanceAttributionModel.as_of.desc()).limit(limit)
        )).all()
        summaries = (await self._session.scalars(
            select(PerformanceSummaryModel)
            .order_by(PerformanceSummaryModel.window_end.desc()).limit(limit)
        )).all()
        return {
            "attributions": [_columns(item) for item in attributions],
            "summaries": [_columns(item) for item in summaries],
        }

    async def data_quality(self) -> dict:
        feature_run = (await self._session.scalars(
            select(FeatureRunModel).order_by(FeatureRunModel.as_of.desc()).limit(1)
        )).first()
        bar_known_at = await self._session.scalar(
            select(func.max(BarSeriesRevisionModel.known_at))
        )
        return {
            "latest_feature_run": (
                {**_columns(feature_run),
                 "coverage": float(feature_run.coverage)} if feature_run else None
            ),
            "latest_revision_known_at": bar_known_at,
        }
