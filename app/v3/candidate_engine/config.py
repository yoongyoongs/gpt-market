"""候选生成引擎配置。

任务书 §39：所有关键参数配置化，禁止散落 magic number。
第一版默认值全部来自《V3_低位埋伏候选生成与排序_详细设计.md》
§40 配置示例与各专家评分节；覆盖方式：
- 环境变量前缀 ``V3_CE_``（如 V3_CE_MODE / V3_CE_LP_TOP_N）；
- 直接构造 CandidateEngineConfig 传入服务（测试与脚本场景）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class EngineMode(BaseModel):
    """新旧管线并跑模式（任务书 §2）：old / new / dual。"""

    mode: Literal["old", "new", "dual"] = "dual"


class SafetyConfig(BaseModel):
    """L1 Hard Safety Filter 配置（设计 §4）。

    只允许交易资格/数据质量类硬过滤；exclude_st 与 reject_stale
    可配置开关，其余为事实性判断。
    """

    exclude_st: bool = True
    reject_stale: bool = True
    min_daily_bars: int = 120
    close_floor: float = Field(default=0.0, gt=0, description="close 须严格大于该值")


class SoftConfig(BaseModel):
    """Soft scoring 工具默认精度（设计 §4.3 Smoothstep）。"""

    # 预留：如需 logistic 替代 smoothstep，在此切换
    curve: Literal["smoothstep"] = "smoothstep"


class CandidateEngineConfig(BaseModel):
    """候选引擎总配置（后续步骤逐步扩充各段）。"""

    mode: EngineMode = Field(default_factory=EngineMode)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)

    @classmethod
    def from_env(cls) -> "CandidateEngineConfig":
        return cls(
            mode=EngineMode(mode=os.getenv("V3_CE_MODE", "dual")),
            safety=SafetyConfig(
                exclude_st=_env_bool("V3_CE_SAFETY_EXCLUDE_ST", True),
                reject_stale=_env_bool("V3_CE_SAFETY_REJECT_STALE", True),
                min_daily_bars=_env_int("V3_CE_SAFETY_MIN_DAILY_BARS", 120),
            ),
        )


@lru_cache(maxsize=1)
def candidate_engine_config_from_env() -> CandidateEngineConfig:
    """进程级缓存的环境配置；测试中请直接构造 CandidateEngineConfig。"""
    return CandidateEngineConfig.from_env()
