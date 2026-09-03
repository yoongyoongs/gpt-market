"""仓库级正式 Executor Registry（STR-001）。

整改复验 §3.6 STR-001：Shadow 引擎已有，但缺"将具体 Strategy Version
持续映射到真正执行器"的仓库级注册表，也没有生产调用方。本模块补齐
Registry；Scheduler 接线见 scripts/v3_scheduler.py 的 shadow-observation Job。

Executor 契约（与 ShadowExecutorService 的 Executor 签名一致）：

    executor(subject_key, as_of) -> dict（服务器可确定性执行的机器层）

- subject_key 契约："{market}:{code}"（如 "SH:600000"），即 Evidence
  域的 SECURITY subject_id 格式；解析失败/未知证券 → 异常，由 Shadow
  Runtime 如实记 error，绝不伪造输出；
- 执行内容 = 系统唯一确定性的策略机器层：该证券在 as_of 时点最新
  PUBLISHED Feature Run 的特征视图 × 标准 Recall 通道谓词求值；
- Strategy Version 差异点：configuration 可选键 "recall_channel_codes"
  （通道子集选择）；缺省 = 全部标准通道。未注册/配置了未知通道码 →
  异常（诚实失败，不静默降级）；
- 输出不含 wall-clock（as_of 只是回显），同一 (subject, as_of, 数据)
  两次执行输出逐字节一致 → ShadowObservation 的 hash/diff 有意义；
- AI Decision 层不做 Shadow 重算（SERVER_HAS_NO_MODEL_API），边界由
  ShadowExecutorService 保持。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from app.v3.application.evaluate_recall_channels import feature_recall_channels
from app.v3.domain.recall import RecallFeatureView
from app.v3.providers.recall import RecallChannelUnavailable

# 与 ShadowExecutorService.Executor 签名一致
Executor = Callable[[str, datetime], Any]


class SubjectKeyError(ValueError):
    """subject_key 不符合 "{market}:{code}" 契约。"""


def parse_subject_key(subject_key: str) -> tuple[str, str]:
    market, _, code = subject_key.partition(":")
    if not market or not code or market not in {"SH", "SZ", "BJ"}:
        raise SubjectKeyError(
            f"subject_key must be 'MARKET:CODE' (SH|SZ|BJ), got {subject_key!r}"
        )
    return market, code


def _channel_view(view) -> RecallFeatureView:
    """PublishedSecurityFeatureView → RecallFeatureView（通道求值入参）。"""
    return RecallFeatureView(
        feature_run_id=view.feature_run_id,
        security_id=view.security_id,
        as_of=view.as_of,
        close=view.close,
        return_3d=view.return_3d,
        return_5d=view.return_5d,
        return_20d=view.return_20d,
        position_60d=view.position_60d,
        ma20_slope=view.ma20_slope,
        breakout_20d=view.breakout_20d,
        pullback_20d=view.pullback_20d,
        volume_ratio_5d=view.volume_ratio_5d,
        volume_expansion=view.volume_expansion,
        relative_index_strength=view.relative_index_strength,
        relative_industry_strength=view.relative_industry_strength,
        coverage=view.coverage,
        stale=view.stale,
        features=view.features,
        source_content_hash=view.source_content_hash,
    )


def resolve_channel_selection(
    configuration: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Strategy configuration → 通道码子集（缺省 = 全部标准通道）。"""
    standard = tuple(channel.channel.code for channel in feature_recall_channels())
    requested = (configuration or {}).get("recall_channel_codes")
    if requested is None:
        return standard
    if not isinstance(requested, (list, tuple)) or not requested:
        raise ValueError("recall_channel_codes must be a non-empty list")
    unknown = [str(code) for code in requested if str(code) not in standard]
    if unknown:
        raise ValueError(f"unknown recall channel codes: {', '.join(unknown)}")
    return tuple(dict.fromkeys(str(code) for code in requested))


async def _executor_for(
    uow_factory: Callable,
    strategy_version_id: UUID,
    channel_codes: tuple[str, ...],
) -> Executor:
    channels = tuple(
        channel for channel in feature_recall_channels()
        if channel.channel.code in channel_codes
    )

    async def executor(subject_key: str, as_of: datetime) -> dict[str, Any]:
        market, code = parse_subject_key(subject_key)
        async with uow_factory() as uow:
            security_id = await uow.universes.security_id_by_key(market, code)
            if security_id is None:
                raise LookupError(f"unknown security subject: {subject_key}")
            view = await uow.features.latest_security_feature(
                security_id, as_of=as_of,
            )
        if view is None:
            raise LookupError(
                f"no published feature view for {subject_key} at as_of"
            )
        feature_view = _channel_view(view)
        channel_facts: dict[str, dict[str, Any]] = {}
        hit_channel_count = 0
        for channel in channels:
            entry: dict[str, Any] = {"code": channel.channel.code}
            try:
                evaluation = channel.evaluate((feature_view,))
            except RecallChannelUnavailable as exc:
                entry.update(hit=False, strength=None,
                             unavailable_reason=str(exc))
                channel_facts[channel.channel.code] = entry
                continue
            candidate = next(
                (item for item in evaluation.candidates
                 if item.security_id == feature_view.security_id),
                None,
            )
            entry.update(
                hit=candidate is not None,
                strength=None if candidate is None else candidate.strength,
                unavailable_reason=None,
            )
            channel_facts[channel.channel.code] = entry
            if candidate is not None:
                hit_channel_count += 1
        return {
            "subject_key": subject_key,
            "as_of": as_of.isoformat(),
            "feature_run_id": str(feature_view.feature_run_id),
            "channels": channel_facts,
            "evaluated_channel_count": len(channels),
            "hit_channel_count": hit_channel_count,
        }

    return executor


async def build_executor_registry(uow_factory: Callable) -> dict[str, Any]:
    """构建 strategy_version_id → Executor 注册表。

    返回 {"executors": {UUID: Executor}, "registered_count": int}。
    每个 Strategy Version 绑定一个真执行器：对该 subject 的最新
    PUBLISHED 特征视图求值标准 Recall 通道（configuration 可选通道子集）。
    """
    async with uow_factory() as uow:
        catalog = await uow.strategies.strategy_catalog(limit=1_000)
    executors: dict[UUID, Executor] = {}
    issues: list[dict[str, str]] = []
    for version in catalog["strategy_versions"]:
        strategy_version_id = version["strategy_version_id"]
        try:
            channel_codes = resolve_channel_selection(version.get("configuration"))
        except ValueError as exc:
            # 单版本配置错误只让该版本没有执行器（Shadow 侧如实记
            # EXECUTOR_NOT_AVAILABLE），不让整个 Registry 构建失败
            issues.append({
                "strategy_version_id": str(strategy_version_id),
                "reason": str(exc),
            })
            continue
        executors[strategy_version_id] = await _executor_for(
            uow_factory, strategy_version_id, channel_codes,
        )
    return {
        "executors": executors,
        "registered_count": len(executors),
        "issues": issues,
    }
