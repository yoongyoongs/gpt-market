"""R5-P1-008/§31/§66：盘中事件轮询——NEW_EVIDENCE / STRUCTURE_CHANGED
接入 Resident Runtime；AttentionEvent != Trade（只产事件不改状态）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.v3.application.intraday_event_poll import IntradayEventPollService

NOW = datetime(2026, 9, 3, 5, 30, tzinfo=timezone.utc)


class _FakeEvaluation:
    def __init__(self, created=(), skipped=0):
        self.created = tuple(created)
        self.skipped = skipped


class _FakeEngine:
    def __init__(self):
        self.evidence_calls: list[dict] = []
        self.structure_calls: list[dict] = []

    async def record_new_evidence(self, items, *, universe, as_of):
        self.evidence_calls.append({
            "items": list(items), "universe": set(universe), "as_of": as_of,
        })
        return _FakeEvaluation(created=({"evidence_id": "x"},), skipped=1)

    async def record_structure_changes(self, changes, *, as_of):
        self.structure_calls.append({"changes": list(changes), "as_of": as_of})
        return _FakeEvaluation(created=({"structure": True},))


class _FakeDeep:
    def __init__(self, trends: dict[str, str], fail_codes=()):
        self._trends = trends  # code -> trend
        self._fail_codes = set(fail_codes)
        self.calls: list[str] = []

    async def get_intraday_structure(self, code, *, as_of):
        self.calls.append(code)
        if code in self._fail_codes:
            raise RuntimeError("deep down")
        return SimpleNamespace(periods={
            "60m": {"structure": {"trend": self._trends.get(code, "UP")}},
            "15m": {"structure": {"trend": "SIDEWAYS"}},
        })


class _FakeEvidenceRepo:
    def __init__(self, views_by_subject: dict[str, list] | None = None):
        self._views = views_by_subject or {}
        self.queries: list = []

    async def retrieve_view(self, *, query):
        self.queries.append(query)
        return SimpleNamespace(views=tuple(self._views.get(query.subject_id, ())))


class _FakeWatchlistRepo:
    def __init__(self, rows):
        self._rows = rows

    async def read_watchlist(self, state, limit):
        return list(self._rows)


class _FakeReads:
    def __init__(self, positions):
        self._positions = positions

    async def portfolio_overview(self, limit):
        return {"accounts": [{"positions": self._positions}]}


class _FakeUow:
    def __init__(self, watchlist, reads, evidence):
        self.ai_imports = watchlist
        self.reads = reads
        self.evidence = evidence

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _view(code, market="SZ", *, known_at=NOW, relevance=0.9, title="重大合同"):
    return SimpleNamespace(record=SimpleNamespace(
        evidence_id=uuid4(), known_at=known_at, relevance=relevance,
        evidence_type="NEWS", normalized_payload={"title": title},
    ))


def _service(engine, uow, deep=None):
    return IntradayEventPollService(
        lambda: uow, engine, deep, clock=lambda: NOW,
    )


def _uow(evidence_views=None):
    watchlist = _FakeWatchlistRepo([
        {"state": "WAIT_ENTRY", "security_market": "SZ",
         "security_code": "000001", "security_id": "sid-1"},
        {"state": "CLOSED", "security_market": "SZ",
         "security_code": "600000", "security_id": "sid-x"},
    ])
    reads = _FakeReads([{
        "quantity": 100, "security_market": "SH", "security_code": "600036",
        "security_id": "sid-2",
    }])
    evidence = _FakeEvidenceRepo(evidence_views or {})
    return _FakeUow(watchlist, reads, evidence)


@pytest.mark.asyncio
async def test_new_material_evidence_in_pool_emits() -> None:
    """§66 Intraday Evidence：池内券出现新 material Evidence →
    record_new_evidence 收到窗口内 items + 池 universe。"""
    engine = _FakeEngine()
    uow = _uow({"SZ:000001": [_view("000001")]})
    report = await _service(engine, uow).execute(as_of=NOW)
    assert report["pool_size"] == 2  # WATCHING 前态 + 持仓；CLOSED 不进池
    assert report["evidence"]["items"] == 1
    call = engine.evidence_calls[0]
    assert call["universe"] == {("SZ", "000001"), ("SH", "600036")}
    item = call["items"][0]
    assert item["market"] == "SZ" and item["code"] == "000001"
    assert item["materiality"] == 0.9
    assert item["title"] == "重大合同"
    assert report["evidence"]["created"] == 1
    # subject 精确匹配查询（不再客户端凑合）
    queried = {query.subject_id for query in uow.evidence.queries}
    assert queried == {"SZ:000001", "SH:600036"}
    assert uow.evidence.queries[0].subject_type == "SECURITY"


@pytest.mark.asyncio
async def test_stale_evidence_windowed_out() -> None:
    """窗口外 known_at 的证据不算盘中增量；窗口内 item 全部交给
    engine——materiality 阈值门是 engine 的冻结职责（§6.3）。"""
    engine = _FakeEngine()
    uow = _uow({"SZ:000001": [
        _view("a", known_at=NOW - timedelta(hours=2)),   # 窗口外
        _view("b", relevance=0.3),                        # 窗口内，engine 把关
        _view("c"),                                       # 窗口内有效
    ]})
    await _service(engine, uow).execute(as_of=NOW)
    items = engine.evidence_calls[0]["items"]
    assert len(items) == 2
    assert all(item["title"] == "重大合同" for item in items)
    assert {item["materiality"] for item in items} == {0.3, 0.9}


@pytest.mark.asyncio
async def test_structure_trend_flip_emits_once() -> None:
    """§66 Structure Change：60m 趋势翻转 → STRUCTURE_CHANGED 一次；
    下一轮同趋势不重复（进程内快照 + engine 去抖）。"""
    engine = _FakeEngine()
    deep = _FakeDeep({"000001": "DOWN"})
    uow = _uow()
    service = _service(engine, uow, deep)
    first = await service.execute(as_of=NOW)  # 快照建立：UP → 基线
    assert first["structure"]["changes"] == []
    deep._trends["000001"] = "UP"  # 翻转回 UP
    second = await service.execute(as_of=NOW)
    assert second["structure"]["scanned"] == 2  # 池内 000001 + 600036
    assert len(second["structure"]["changes"]) == 1
    change = second["structure"]["changes"][0]
    assert change["from_trend"] == "DOWN" and change["to_trend"] == "UP"
    assert change["timeframe"] == "60m"
    third = await service.execute(as_of=NOW)
    assert third["structure"]["changes"] == []


@pytest.mark.asyncio
async def test_deep_failure_isolated_per_security() -> None:
    """单券 Deep 抓取失败 → 跳过该券，其余结构扫描照常。"""
    engine = _FakeEngine()
    deep = _FakeDeep({"600036": "DOWN"}, fail_codes={"000001"})
    uow = _uow()
    report = await _service(engine, uow, deep).execute(as_of=NOW)
    assert report["structure"]["scanned"] == 1  # 600036 成功；000001 失败跳过


@pytest.mark.asyncio
async def test_structure_scan_is_bounded() -> None:
    """Deep 分钟抓取按券 3 次调用——结构扫描必须有界（默认 30）。"""
    engine = _FakeEngine()
    rows = [
        {"state": "WATCHING", "security_market": "SZ",
         "security_code": f"{index:06d}", "security_id": f"sid-{index}"}
        for index in range(40)
    ]
    uow = _FakeUow(_FakeWatchlistRepo(rows), _FakeReads([]), _FakeEvidenceRepo())
    deep = _FakeDeep({})
    report = await _service(engine, uow, deep).execute(as_of=NOW)
    assert report["pool_size"] == 40
    assert report["structure"]["scanned"] == 30
    assert len(deep.calls) == 30


@pytest.mark.asyncio
async def test_no_deep_service_reports_not_wired() -> None:
    engine = _FakeEngine()
    report = await _service(engine, _uow(), None).execute(as_of=NOW)
    assert report["structure"]["scanned"] == 0
    assert report["structure"]["created"] == 0
    assert report["status"] == "AVAILABLE"
