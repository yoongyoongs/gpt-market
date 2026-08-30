from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from uuid import UUID, uuid4

from app.utils.time import SHANGHAI
from app.v3.domain.market_data import (
    AdjustType,
    AdjustmentFactorPoint,
    AdjustmentFactorRevision,
    AdjustmentFactorRevisionContent,
    BarPeriod,
    BarSeriesRevision,
    BarSeriesRevisionContent,
    HistoricalBarFetchResult,
    MarketBar,
    PointInTimePrecision,
)
from app.v3.providers.bars import HistoricalBarProvider


class AllHistoricalBarProvidersFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class DailyBarIngestionBundle:
    source_code: str
    raw_revision: BarSeriesRevision | None
    adjusted_revision: BarSeriesRevision
    hfq_revision: BarSeriesRevision | None
    factor_revision: AdjustmentFactorRevision | None
    provider_errors: tuple[str, ...]
    partial_bars: tuple[MarketBar, ...]


class BuildDailyBarRevisionsService:
    def __init__(
        self,
        providers: Sequence[HistoricalBarProvider],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one historical bar provider is required")
        self._providers = tuple(providers)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(
        self,
        security_id: UUID,
        code: str,
        *,
        limit: int = 300,
        minimum_last_bar_date: date | None = None,
    ) -> DailyBarIngestionBundle:
        errors: list[str] = []
        limited_candidate: HistoricalBarFetchResult | None = None
        for provider in self._providers:
            try:
                qfq = await provider.fetch(code, BarPeriod.DAY, AdjustType.QFQ, limit)
                self._validate_fetch(provider.code, code, AdjustType.QFQ, qfq)
                self._validate_freshness(qfq, minimum_last_bar_date)
            except Exception as exc:
                errors.append(f"{provider.code}:QFQ:{type(exc).__name__}:{exc}")
                continue
            try:
                raw = await provider.fetch(code, BarPeriod.DAY, AdjustType.RAW, limit)
                self._validate_fetch(provider.code, code, AdjustType.RAW, raw)
                self._validate_freshness(raw, minimum_last_bar_date)
            except Exception as exc:
                errors.append(f"{provider.code}:RAW:{type(exc).__name__}:{exc}")
                if limited_candidate is None:
                    limited_candidate = qfq
                continue
            return self._build_paired(security_id, raw, qfq, tuple(errors))
        if limited_candidate is not None:
            return self._build_qfq_only(security_id, limited_candidate, tuple(errors))
        raise AllHistoricalBarProvidersFailed(" | ".join(errors))

    @staticmethod
    def _validate_fetch(
        provider_code: str,
        code: str,
        adjust_type: AdjustType,
        fetched: HistoricalBarFetchResult,
    ) -> None:
        if fetched.source_code != provider_code:
            raise ValueError("bar provider returned a mismatched source_code")
        if fetched.code != code or fetched.period is not BarPeriod.DAY:
            raise ValueError("bar provider returned a mismatched symbol or period")
        if fetched.adjust_type is not adjust_type:
            raise ValueError("bar provider returned a mismatched adjustment type")

    @staticmethod
    def _validate_freshness(
        fetched: HistoricalBarFetchResult, minimum_last_bar_date: date | None
    ) -> None:
        if minimum_last_bar_date is None:
            return
        last_bar_date = fetched.bars[-1].bar_time.astimezone(SHANGHAI).date()
        if last_bar_date < minimum_last_bar_date:
            raise ValueError(
                f"last bar {last_bar_date} is older than required {minimum_last_bar_date}"
            )

    def _build_paired(
        self,
        security_id: UUID,
        raw: HistoricalBarFetchResult,
        qfq: HistoricalBarFetchResult,
        errors: tuple[str, ...],
    ) -> DailyBarIngestionBundle:
        raw_formal, raw_partial = self._formal_bars(raw)
        qfq_formal, qfq_partial = self._formal_bars(qfq)
        raw_by_time = {bar.bar_time: bar for bar in raw_formal}
        pairs = [(bar, raw_by_time.get(bar.bar_time)) for bar in qfq_formal]
        complete_pairs = [(adjusted, source) for adjusted, source in pairs if source is not None]
        factors_complete = len(complete_pairs) == len(qfq_formal) and bool(qfq_formal)
        known_at = max(self._clock(), raw.fetch_time, qfq.fetch_time)
        factor_revision = None
        if complete_pairs:
            factor_revision = AdjustmentFactorRevision.build(
                AdjustmentFactorRevisionContent(
                    factor_revision_id=uuid4(),
                    security_id=security_id,
                    source="derived:qfq_over_raw",
                    upstream_source=f"{raw.upstream_source}+{qfq.upstream_source}",
                    derivation_method="QFQ_CLOSE_DIV_RAW_CLOSE",
                    fetch_time=max(raw.fetch_time, qfq.fetch_time),
                    known_at=known_at,
                    factors=tuple(
                        AdjustmentFactorPoint(
                            trading_time=adjusted.bar_time,
                            factor=adjusted.close / source.close,
                        )
                        for adjusted, source in complete_pairs
                    ),
                )
            )
        raw_revision = self._build_bar_revision(
            security_id,
            raw,
            raw_formal,
            known_at=known_at,
            raw_available=True,
            precision=PointInTimePrecision.FULL,
        )
        adjusted_revision = self._build_bar_revision(
            security_id,
            qfq,
            qfq_formal,
            known_at=known_at,
            raw_available=True,
            factor_revision_id=factor_revision.factor_revision_id if factors_complete else None,
            precision=(PointInTimePrecision.FULL if factors_complete else PointInTimePrecision.LIMITED),
            precision_reason=(None if factors_complete else "raw/qfq dates did not align completely"),
        )
        hfq_revision = None
        if factors_complete and factor_revision is not None:
            first_factor = factor_revision.factors[0].factor
            factor_by_time = {
                item.trading_time: item.factor / first_factor
                for item in factor_revision.factors
            }
            hfq_bars = tuple(
                MarketBar(
                    bar_time=bar.bar_time,
                    open=bar.open * factor_by_time[bar.bar_time],
                    high=bar.high * factor_by_time[bar.bar_time],
                    low=bar.low * factor_by_time[bar.bar_time],
                    close=bar.close * factor_by_time[bar.bar_time],
                    volume=bar.volume,
                    amount=bar.amount,
                    provisional=bar.provisional,
                    fetch_time=bar.fetch_time,
                )
                for bar in raw_formal
            )
            hfq_fetched = raw.model_copy(
                update={
                    "source_code": "derived:hfq_from_raw_qfq",
                    "upstream_source": f"{raw.upstream_source}+{qfq.upstream_source}",
                    "adjust_type": AdjustType.HFQ,
                    "fetch_time": max(raw.fetch_time, qfq.fetch_time),
                    "bars": hfq_bars,
                }
            )
            hfq_revision = self._build_bar_revision(
                security_id,
                hfq_fetched,
                hfq_bars,
                known_at=known_at,
                raw_available=True,
                factor_revision_id=factor_revision.factor_revision_id,
                precision=PointInTimePrecision.FULL,
            )
        return DailyBarIngestionBundle(
            source_code=qfq.source_code,
            raw_revision=raw_revision,
            adjusted_revision=adjusted_revision,
            hfq_revision=hfq_revision,
            factor_revision=factor_revision,
            provider_errors=errors,
            partial_bars=tuple((*raw_partial, *qfq_partial)),
        )

    def _build_qfq_only(
        self,
        security_id: UUID,
        qfq: HistoricalBarFetchResult,
        errors: tuple[str, ...],
    ) -> DailyBarIngestionBundle:
        formal, partial = self._formal_bars(qfq)
        revision = self._build_bar_revision(
            security_id,
            qfq,
            formal,
            known_at=max(self._clock(), qfq.fetch_time),
            raw_available=False,
            precision=PointInTimePrecision.LIMITED,
            precision_reason="upstream supplied QFQ history but RAW history was unavailable",
        )
        return DailyBarIngestionBundle(
            source_code=qfq.source_code,
            raw_revision=None,
            adjusted_revision=revision,
            hfq_revision=None,
            factor_revision=None,
            provider_errors=errors,
            partial_bars=partial,
        )

    def _formal_bars(
        self, fetched: HistoricalBarFetchResult
    ) -> tuple[tuple[MarketBar, ...], tuple[MarketBar, ...]]:
        now = self._clock().astimezone(SHANGHAI)
        formal: list[MarketBar] = []
        partial: list[MarketBar] = []
        for bar in fetched.bars:
            local_date = bar.bar_time.astimezone(SHANGHAI).date()
            is_current_unclosed = local_date == now.date() and now.time() < time(15, 10)
            if bar.provisional or is_current_unclosed:
                partial.append(bar.model_copy(update={"provisional": True}))
            else:
                formal.append(bar.model_copy(update={"provisional": False}))
        if not formal:
            raise ValueError("provider returned no completed daily bars")
        return tuple(formal), tuple(partial)

    @staticmethod
    def _build_bar_revision(
        security_id: UUID,
        fetched: HistoricalBarFetchResult,
        bars: tuple[MarketBar, ...],
        *,
        known_at: datetime,
        raw_available: bool,
        precision: PointInTimePrecision,
        factor_revision_id: UUID | None = None,
        precision_reason: str | None = None,
    ) -> BarSeriesRevision:
        return BarSeriesRevision.build(
            BarSeriesRevisionContent(
                revision_id=uuid4(),
                security_id=security_id,
                period=BarPeriod.DAY,
                adjust_type=fetched.adjust_type,
                source=fetched.source_code,
                upstream_source=fetched.upstream_source,
                raw_bar_available=raw_available,
                factor_revision_id=factor_revision_id,
                point_in_time_precision=precision,
                precision_reason=precision_reason,
                known_at=known_at,
                bars=bars,
            )
        )
