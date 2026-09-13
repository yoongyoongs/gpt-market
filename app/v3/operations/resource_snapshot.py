"""P1-01（run_once 资源治理）：轻量 Runtime Resource Snapshot。

不引入 psutil，只读 /proc 与 cgroup 文件；任何一步失败都降级为
null + reason，绝不抛错——Observability 永远是辅助，资源采集失败
绝不能让 market-data 之类的 Job 变成 FAILED。

采集项（按文档 §14.1）：
- timestamp
- load1/5/15（os.getloadavg；Windows 无此 API → null）
- process rss / max_rss（VmRSS 来自 /proc/self/status，Windows 降级 null）
- cgroup memory.current / memory.max（v2 顶层路径优先，v1 回退）
- cgroup cpu.stat（若存在）
"""

from __future__ import annotations

import os
import time

_CGROUP_V2_MEMORY_CURRENT = "/sys/fs/cgroup/memory.current"
_CGROUP_V2_MEMORY_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_V1_MEMORY_CURRENT = "/sys/fs/cgroup/memory/memory.current"
_CGROUP_V1_MEMORY_MAX = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CGROUP_CPU_STAT = "/sys/fs/cgroup/cpu.stat"
_PROC_STATUS = "/proc/self/status"


def _read_int_file(path: str) -> int | None:
    """读单个整数文件（cgroup 值可能带换行；"max" 字面量 → None）。"""
    try:
        with open(path, encoding="ascii") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    if not text or text == "max":
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def _proc_status_rss_kb() -> int | None:
    """从 /proc/self/status 提取 VmRSS（kB）。Linux only。"""
    try:
        with open(_PROC_STATUS, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _load_averages() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except (AttributeError, OSError):  # Windows 无 getloadavg
        return None


def _cgroup_memory() -> tuple[int | None, int | None, str]:
    """返回 (current_bytes, max_bytes, source)；两代 cgroup 都探测。"""
    current = _read_int_file(_CGROUP_V2_MEMORY_CURRENT)
    maximum = _read_int_file(_CGROUP_V2_MEMORY_MAX)
    if current is not None:
        source = "cgroup-v2"
    else:
        current = _read_int_file(_CGROUP_V1_MEMORY_CURRENT)
        maximum = _read_int_file(_CGROUP_V1_MEMORY_MAX)
        source = "cgroup-v1" if current is not None else "unavailable"
    return current, maximum, source


def snapshot() -> dict:
    """采集一次资源快照。任何失败降级为 null/None + available=false。

    返回结构（mb 字段统一两位小数）：
    {
      "available": bool,
      "timestamp": iso8601,
      "load1" / "load5" / "load15": float | None,
      "rss_mb": float | None,
      "max_rss_mb": float | None,   # 暂缺：/proc 之外无轻量通道 → None
      "cgroup_memory_mb": float | None,
      "cgroup_memory_max_mb": float | None,
      "cgroup_memory_source": str,
      "cgroup_cpu_stat": str | None,
      "reasons": [str, ...],        # 每个 null 的原因，便于诊断
    }
    """
    reasons: list[str] = []
    load = _load_averages()
    if load is None:
        reasons.append("loadavg unavailable on this platform")
    rss_kb = _proc_status_rss_kb()
    if rss_kb is None:
        reasons.append(f"VmRSS unavailable ({_PROC_STATUS} not readable)")
    try:
        current_bytes, max_bytes, source = _cgroup_memory()
    except Exception:  # noqa: BLE001 - 任何采集异常都降级，绝不影响业务
        current_bytes, max_bytes, source = None, None, "unavailable"
        reasons.append("cgroup memory probe raised")
    if current_bytes is None:
        reasons.append("cgroup memory.current unavailable")
    cpu_stat: str | None = None
    try:
        with open(_CGROUP_CPU_STAT, encoding="ascii") as handle:
            cpu_stat = handle.read().strip() or None
    except OSError:
        pass

    return {
        "available": bool(load is not None or rss_kb is not None or current_bytes is not None),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "load1": None if load is None else round(load[0], 2),
        "load5": None if load is None else round(load[1], 2),
        "load15": None if load is None else round(load[2], 2),
        "rss_mb": None if rss_kb is None else round(rss_kb / 1024, 2),
        "max_rss_mb": None,
        "cgroup_memory_mb": (
            None if current_bytes is None else round(current_bytes / 1024 / 1024, 2)
        ),
        "cgroup_memory_max_mb": (
            None if max_bytes is None else round(max_bytes / 1024 / 1024, 2)
        ),
        "cgroup_memory_source": source,
        "cgroup_cpu_stat": cpu_stat,
        "reasons": reasons,
    }
