"""OCR Adapter 与识别管线（RC-09A / OCR-001）。

方案 §12.1 最小可用方案：

    上传图片 → 保存 image hash/reference → OCR Adapter（可配置）
    → field + confidence + region → Draft Preview → 用户修正 → Confirm

产品边界：
- Domain 不绑定供应商：OcrAdapter 是协议，本地 tesseract / 云 OCR /
  未来模型视觉都以同一契约接入；
- 不可用必须诚实：binary 缺失或未配置 provider 时抛 OcrUnavailableError，
  绝不伪造识别结果；
- 识别结果只是 Draft：低置信字段显式标出，必须人工确认后才进入账本。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.v3.contracts.base import V3Contract
from app.v3.domain.portfolio import PositionSnapshotDraftCreate, TradeDraftCreate

LOW_CONFIDENCE_THRESHOLD = 0.6

# A 股券商截图常见标签 → 标准字段名
_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "security_code": ("证券代码", "代码", "证券"),
    "price": ("成交价格", "价格", "成交均价", "均价"),
    "quantity": ("成交数量", "数量", "成交股数", "股数"),
    "fee": ("手续费", "佣金", "费用", "规费"),
    "trade_time": ("成交时间", "时间", "成交日期", "日期"),
    "average_cost": ("摊薄成本", "成本价", "成本"),
    "amount": ("成交金额", "金额"),
}

# 数值型字段：标签后的值取首个 token（行内可能还有其它词，如方向）
_VALUE_FIRST_TOKEN = {"security_code", "price", "quantity", "fee", "amount",
                      "average_cost"}

_SIDE_WORDS = {
    "BUY": ("买入", "买进", "购买", "证券买入"),
    "SELL": ("卖出", "卖出", "出售", "证券卖出"),
}


class OcrField(V3Contract):
    key: str
    value: str
    confidence: float
    region: dict[str, float] | None = None


class OcrResult(V3Contract):
    provider: str
    fields: tuple[OcrField, ...]
    raw: dict[str, Any] = {}


class OcrUnavailableError(Exception):
    """OCR 供应商不可用：binary 缺失 / 未配置。绝不伪造识别结果。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OcrAdapter(Protocol):
    provider: str

    async def recognize(self, image: bytes) -> OcrResult:
        ...


def parse_tesseract_tsv(tsv_text: str) -> list[dict[str, Any]]:
    """把 tesseract TSV 输出按行分组：text / confidence(0-1) / region。"""
    rows: list[dict[str, Any]] = []
    header: list[str] = []
    for line in tsv_text.splitlines():
        parts = line.split("\t")
        # 真实 tesseract TSV：level=5 为词行（level=1..4 是页/块/段/行容器）
        if header and parts[0] == "5" and len(parts) >= 12:
            rows.append({
                "block": int(parts[2]), "par": int(parts[3]), "line": int(parts[4]),
                "left": int(parts[6]), "top": int(parts[7]),
                "width": int(parts[8]), "height": int(parts[9]),
                "conf": float(parts[10]), "text": parts[11],
            })
        elif parts and parts[0] == "level":
            header = parts
    grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for row in rows:
        if row["text"].strip() and row["conf"] >= 0:
            grouped.setdefault((row["block"], row["par"], row["line"]), []).append(row)
    lines: list[dict[str, Any]] = []
    for words in grouped.values():
        words.sort(key=lambda item: item["left"])
        confidences = [word["conf"] for word in words]
        lines.append({
            "text": " ".join(word["text"] for word in words),
            "confidence": sum(confidences) / len(confidences) / 100,
            "region": {
                "x": min(word["left"] for word in words),
                "y": min(word["top"] for word in words),
                "w": max(word["left"] + word["width"] for word in words)
                - min(word["left"] for word in words),
                "h": max(word["top"] + word["height"] for word in words)
                - min(word["top"] for word in words),
            },
        })
    return lines


def _compact(text: str) -> str:
    """中文标签常被逐字识别（'证 券 代 码'），匹配时先去空格。"""
    return text.replace(" ", "")


_ALL_LABELS = tuple(
    label for labels in _FIELD_LABELS.values() for label in labels
)


def _find_value_partner(lines: list[dict[str, Any]], label_line: dict[str, Any]):
    """标签列与数值列常分属不同 block：在同一视觉行右侧找值行。

    约束：值行必须整体位于标签行右缘之后（标签列自匹配排除）、
    行顶端落在标签行纵向范围内、且不是另一条标签行。
    """
    region = label_line["region"]
    right_edge = region["x"] + region["w"]
    best = None
    for candidate in lines:
        if candidate is label_line:
            continue
        compacted = _compact(candidate["text"])
        if compacted.startswith(_ALL_LABELS):
            continue
        other = candidate["region"]
        if other["x"] < right_edge:
            continue
        if other["y"] < region["y"] - 8:
            continue
        if other["y"] > region["y"] + region["h"]:
            continue
        if best is None or other["x"] < best["region"]["x"]:
            best = candidate
    return best


def map_fields(lines: list[dict[str, Any]]) -> tuple[OcrField, ...]:
    """把识别行按标签映射成标准字段；side 通过买卖词直接判定。"""
    fields: list[OcrField] = []
    claimed: set[str] = set()
    for line in lines:
        compacted = _compact(line["text"])
        if "side" not in claimed:
            for side, words in _SIDE_WORDS.items():
                if any(word in compacted for word in words):
                    fields.append(OcrField(
                        key="side", value=side,
                        confidence=round(line["confidence"], 4),
                        region=line["region"],
                    ))
                    claimed.add("side")
                    break
        for key, labels in _FIELD_LABELS.items():
            if key in claimed:
                continue
            label = next((item for item in labels if compacted.startswith(item)), None)
            if label is None:
                continue
            claimed.add(key)
            value = compacted[len(label):]
            if not value:
                partner = _find_value_partner(lines, line)
                value = partner["text"] if partner else ""
            if not value:
                continue
            if key in _VALUE_FIRST_TOKEN:
                tokens = value.split()
                value = tokens[0] if tokens else value
            fields.append(OcrField(
                key=key, value=value,
                confidence=round(line["confidence"], 4),
                region=line["region"],
            ))
            break
    return tuple(fields)


class TesseractOcrAdapter:
    """本地 OCR（tesseract 二进制）。可选依赖：binary 缺失时诚实报不可用。"""

    def __init__(self, languages: str = "chi_sim+eng", timeout_seconds: float = 30.0):
        self.provider = "tesseract"
        self._languages = languages
        self._timeout = timeout_seconds

    async def recognize(self, image: bytes) -> OcrResult:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(image)
            image_path = handle.name
        try:
            process = await asyncio.create_subprocess_exec(
                "tesseract", image_path, "stdout", "-l", self._languages, "tsv",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), self._timeout)
            except TimeoutError:
                process.kill()
                raise OcrUnavailableError("TESSERACT_TIMEOUT") from None
        except FileNotFoundError:
            raise OcrUnavailableError("TESSERACT_NOT_AVAILABLE") from None
        finally:
            try:
                os.unlink(image_path)
            except OSError:
                pass
        lines = parse_tesseract_tsv(stdout.decode("utf-8", errors="replace"))
        return OcrResult(
            provider=self.provider, fields=map_fields(lines),
            raw={"line_count": len(lines)},
        )


class NullOcrAdapter:
    """未配置 provider：显式不可用，保持现状（客户端提交 OCR 结果导入）。"""

    provider = "none"

    async def recognize(self, image: bytes) -> OcrResult:
        raise OcrUnavailableError("OCR_PROVIDER_NOT_CONFIGURED")


def build_ocr_adapter(provider: str | None = None) -> OcrAdapter:
    provider = provider or os.getenv("V3_OCR_PROVIDER", "tesseract")
    if provider == "none":
        return NullOcrAdapter()
    if provider == "tesseract":
        return TesseractOcrAdapter()
    raise ValueError(f"unknown V3_OCR_PROVIDER: {provider}")


def _parse_decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None


def _try_strptime(text: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            # 券商截图为本地市场时间，V3 合约要求 aware——按东八区落位
            from datetime import timedelta, timezone as _tz
            return parsed.replace(tzinfo=_tz(timedelta(hours=8)))
        return parsed
    return None


def _parse_datetime(value: str) -> datetime | None:
    text = value.strip().replace("T", " ")
    parsed = _try_strptime(text)
    if parsed is None:
        # tesseract 偶发截断尾随冒号（"09:31:"）——去除尾随标点后重试
        trimmed = text.rstrip(":：. ")
        if trimmed != text:
            parsed = _try_strptime(trimmed)
    if parsed is not None:
        return parsed
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


class DraftAssembler:
    """OCR 字段 → Draft Preview：显式低置信标记，缺字段绝不硬补。"""

    def __init__(self, low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD):
        self._threshold = low_confidence_threshold

    def _prepare(self, result: OcrResult, overrides: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        confidences: dict[str, float] = {}
        regions: dict[str, dict[str, float]] = {}
        for field in result.fields:
            values[field.key] = field.value
            confidences[field.key] = field.confidence
            if field.region is not None:
                regions[field.key] = field.region
        for key, value in overrides.items():
            if value is None:
                continue
            values[key] = value
            confidences[key] = 1.0
        return values, confidences, regions

    def build_trade(
        self, result: OcrResult, *, account_id, security_id, overrides: dict[str, Any],
    ) -> dict[str, Any]:
        values, confidences, regions = self._prepare(result, overrides)
        low_confidence = [
            key for key, confidence in confidences.items()
            if confidence < self._threshold
        ]
        missing: list[str] = []
        side = values.get("side")
        if side not in {"BUY", "SELL"}:
            side = None
            missing.append("side")
        price = _parse_decimal(str(values.get("price", "")))
        if price is None:
            missing.append("price")
        quantity = _parse_decimal(str(values.get("quantity", "")))
        if quantity is None:
            missing.append("quantity")
        trade_time = (
            _parse_datetime(str(values["trade_time"]))
            if values.get("trade_time") else None
        )
        if trade_time is None:
            missing.append("trade_time")
        fee = _parse_decimal(str(values.get("fee", ""))) or Decimal("0")
        draft = None
        if not missing and side and price and quantity and trade_time:
            draft = TradeDraftCreate(
                account_id=account_id, security_id=security_id, side=side,
                trade_time=trade_time, price=price, quantity=quantity, fee=fee,
                source=f"OCR_{result.provider.upper()}"[:64],
                field_confidence={
                    key: value for key, value in confidences.items()
                    if key in {"side", "price", "quantity", "fee", "trade_time"}
                },
            )
        return {
            "draft": draft,
            "missing_fields": missing,
            "low_confidence_fields": low_confidence,
            "preview_fields": [
                {"key": key, "value": values.get(key), "confidence": confidences.get(key),
                 "region": regions.get(key)}
                for key in sorted(values)
            ],
        }

    def build_position(
        self, result: OcrResult, *, account_id, security_id, overrides: dict[str, Any],
    ) -> dict[str, Any]:
        values, confidences, regions = self._prepare(result, overrides)
        low_confidence = [
            key for key, confidence in confidences.items()
            if confidence < self._threshold
        ]
        missing: list[str] = []
        quantity = _parse_decimal(str(values.get("quantity", "")))
        if quantity is None:
            missing.append("quantity")
        average_cost = _parse_decimal(str(values.get("average_cost", "")))
        if average_cost is None:
            missing.append("average_cost")
        as_of = None
        for time_key in ("as_of", "trade_time"):
            if values.get(time_key):
                as_of = _parse_datetime(str(values[time_key]))
                if as_of is not None:
                    break
        if as_of is None:
            missing.append("as_of")
        draft = None
        if not missing and quantity is not None and average_cost is not None and as_of:
            draft = PositionSnapshotDraftCreate(
                account_id=account_id, security_id=security_id, as_of=as_of,
                quantity=quantity, average_cost=average_cost,
                field_confidence={
                    key: value for key, value in confidences.items()
                    if key in {"quantity", "average_cost", "as_of"}
                },
            )
        return {
            "draft": draft,
            "missing_fields": missing,
            "low_confidence_fields": low_confidence,
            "preview_fields": [
                {"key": key, "value": values.get(key), "confidence": confidences.get(key),
                 "region": regions.get(key)}
                for key in sorted(values)
            ],
        }
