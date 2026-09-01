from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.v3.application.calculate_features import CalculateSecurityFeatureService
from app.v3.application.calculate_index_benchmark_return import (
    CalculateIndexBenchmarkReturn,
)
from app.v3.application.calculate_market_regime import CalculateMarketRegimeService
from app.v3.domain.features import FeatureRun, FeatureRunStatus
from app.v3.domain.hashing import canonical_hash
from app.v3.repositories.protocols import UnitOfWork


class RunFullMarketFeaturesService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        *,
        calculator: CalculateSecurityFeatureService | None = None,
        index_benchmark_code: str = "HS300",
        regime_calculator: CalculateMarketRegimeService | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        batch_size: int = 100,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._uow_factory = uow_factory
        self._calculator = calculator or CalculateSecurityFeatureService()
        self._index_return_calculator = CalculateIndexBenchmarkReturn()
        self._index_benchmark_code = index_benchmark_code
        self._regime_calculator = regime_calculator or CalculateMarketRegimeService()
        self._clock = clock
        self._batch_size = batch_size

    async def execute(
        self,
        *,
        universe_snapshot_id: UUID,
        as_of: datetime,
        feature_version: str = "full-market-v1",
    ) -> FeatureRun:
        started_at = self._clock()
        async with self._uow_factory() as uow:
            targets = await uow.universes.targets(universe_snapshot_id)
        # RC-04-02：按同一 as_of 点时加载匹配的指数基准 revision，确定性
        # 计算 20 日收益后传入 Calculator；基准缺失时保持 None 并显式记录。
        async with self._uow_factory() as uow:
            index_revision = await uow.index_benchmarks.latest(
                self._index_benchmark_code, as_of=as_of
            )
        index_return = self._index_return_calculator.execute(
            revision=index_revision, as_of=as_of
        )
        features = []
        revision_manifest: list[tuple[str, str]] = []
        errors: dict[str, str] = {}
        target_by_id = {target.security_id: target for target in targets}
        for offset in range(0, len(targets), self._batch_size):
            batch = targets[offset:offset + self._batch_size]
            async with self._uow_factory() as uow:
                revisions = await uow.bars.latest_daily_revisions(
                    tuple(target.security_id for target in batch), as_of=as_of
                )
                weekly = {
                    revision.security_id: revision
                    for revision in await uow.bars.latest_weekly_revisions(
                        tuple(target.security_id for target in batch), as_of=as_of
                    )
                }
            seen = set()
            for revision in revisions:
                seen.add(revision.security_id)
                target = target_by_id[revision.security_id]
                try:
                    item = self._calculator.execute(
                        feature_run_id=UUID(int=0), revision=revision, as_of=as_of,
                        weekly_revision=weekly.get(revision.security_id),
                        index_return_20d=index_return.return_20d,
                    )
                    features.append(item)
                    revision_manifest.append((str(revision.security_id), revision.content_hash))
                except Exception as exc:
                    errors[f"{target.market.value}:{target.code}"] = f"{type(exc).__name__}: {exc}"
            for target in batch:
                if target.security_id not in seen:
                    errors[f"{target.market.value}:{target.code}"] = "latest published QFQ DAY revision unavailable at as_of"

        run_id = uuid4()
        features = [item.model_copy(update={"feature_run_id": run_id}) for item in features]
        # Rebuild hashes after binding the final run identity.
        features = [
            type(item).model_validate(item.model_copy(update={
                "content_hash": canonical_hash(item.model_dump(exclude={"content_hash"}))
            }))
            for item in features
        ]
        expected = len(targets)
        successful = len(features)
        completed_at = self._clock()
        manifest_hash = canonical_hash(sorted(revision_manifest))
        run = FeatureRun(
            feature_run_id=run_id,
            as_of=as_of,
            universe_snapshot_id=universe_snapshot_id,
            feature_version=feature_version,
            status=FeatureRunStatus.RUNNING,
            expected_count=expected,
            successful_count=successful,
            failed_count=expected - successful,
            coverage=successful / expected if expected else 0.0,
            bar_revision_set_hash=manifest_hash,
            input_manifest={
                "adjust_type": "QFQ",
                "period": "DAY",
                "revision_count": str(successful),
                "revision_manifest_hash": manifest_hash,
                "index_benchmark_code": self._index_benchmark_code,
                "index_benchmark_revision_id": (
                    str(index_return.revision_id) if index_return.revision_id else ""
                ),
                "index_benchmark_known_at": (
                    index_return.known_at.isoformat() if index_return.known_at else ""
                ),
                "index_benchmark_source": index_return.source or "",
                "index_benchmark_calculation_version": (
                    index_return.calculation_version
                ),
                "index_benchmark_return_20d": (
                    "" if index_return.return_20d is None
                    else repr(index_return.return_20d)
                ),
            },
            error_summary={"count": len(errors), "errors": errors},
            started_at=started_at,
        ).published(completed_at=completed_at)
        regime = self._regime_calculator.execute(
            feature_run_id=run_id,
            features=tuple(features),
            as_of=as_of,
            known_at=completed_at,
            expected_count=expected,
        )
        async with self._uow_factory() as uow:
            inserted = await uow.features.publish(run, tuple(features), regime)
            if not inserted:
                existing = await uow.features.get_run_by_content_hash(run.content_hash)
                if existing is None:
                    raise RuntimeError("feature run publish conflicted without an existing run")
                return existing
            await uow.commit()
        return run
