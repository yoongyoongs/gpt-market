from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.v3.domain.market_data import CorporateAction, CorporateActionType
from app.v3.domain.portfolio import (
    AdjustmentConfirmation,
    AdjustmentType,
    PortfolioAdjustmentCreate,
)

# Corporate Action 类型到 Adjustment 类型的最小确定性映射；
# 金额不可证明时 source 必须为 UNKNOWN，数量/现金增量保持 0，不猜测。
_ACTION_TYPE_MAPPING: dict[CorporateActionType, AdjustmentType] = {
    CorporateActionType.CASH_DIVIDEND: AdjustmentType.CASH_DIVIDEND,
    CorporateActionType.STOCK_DISTRIBUTION: AdjustmentType.STOCK_DIVIDEND,
    CorporateActionType.CASH_AND_STOCK_DISTRIBUTION: AdjustmentType.OTHER,
    CorporateActionType.OTHER_DISTRIBUTION: AdjustmentType.OTHER,
}


@dataclass(frozen=True)
class CorporateActionMatchResult:
    scanned: int
    drafts_created: int


class MatchCorporateActionsService:
    """Corporate Action Match Job（POR-005）：

    扫描生效窗口内的 Corporate Action，为当前持仓账户生成
    `PENDING_RECONCILIATION` 的 Portfolio Adjustment Draft。
    只生成草稿，从不自动确认，不直接修改 Projection。
    """

    def __init__(self, uow_factory: Callable) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self, *, effective_from: datetime, effective_to: datetime
    ) -> CorporateActionMatchResult:
        if effective_from > effective_to:
            raise ValueError("effective_from must not be after effective_to")
        drafts_created = 0
        async with self._uow_factory() as uow:
            actions = await uow.corporate_actions.effective_between(
                effective_from, effective_to
            )
            for action in actions:
                holdings = await uow.portfolios.holdings(action.security_id)
                for account_id, quantity in holdings.items():
                    if await uow.portfolios.has_adjustment_for_action(
                        account_id, action.security_id, action.corporate_action_id
                    ):
                        continue
                    adjustment = self._build_adjustment(action, account_id, quantity)
                    await uow.portfolios.add_adjustment(adjustment)
                    drafts_created += 1
            await uow.commit()
        return CorporateActionMatchResult(
            scanned=len(actions), drafts_created=drafts_created
        )

    @staticmethod
    def _build_adjustment(
        action: CorporateAction, account_id, held_quantity: Decimal
    ) -> PortfolioAdjustmentCreate:
        payload = action.payload or {}
        quantity_delta = Decimal("0")
        cash_delta = Decimal("0")
        provable = True
        if action.action_type is CorporateActionType.CASH_DIVIDEND:
            cash_per_share = payload.get("cash_per_share")
            if cash_per_share is None:
                provable = False
            else:
                cash_delta = held_quantity * Decimal(str(cash_per_share))
        elif action.action_type is CorporateActionType.STOCK_DISTRIBUTION:
            stock_ratio = payload.get("stock_ratio")
            if stock_ratio is None:
                provable = False
            else:
                quantity_delta = held_quantity * Decimal(str(stock_ratio))
        else:
            # 混合分配/其它类型：无法从最小事实集证明金额，保持 UNKNOWN
            provable = False
        return PortfolioAdjustmentCreate(
            account_id=account_id,
            security_id=action.security_id,
            corporate_action_id=action.corporate_action_id,
            adjustment_type=_ACTION_TYPE_MAPPING[action.action_type],
            effective_time=action.effective_time,
            quantity_delta=quantity_delta,
            cash_delta=cash_delta,
            cost_basis_delta=Decimal("0"),
            source="CORPORATE_ACTION_MATCH" if provable else "UNKNOWN",
            source_reference=action.source_reference,
            known_at=action.known_at,
            confirmation_status=AdjustmentConfirmation.PENDING_RECONCILIATION,
        )
