"""R2.1-P0-04/P0-05：Backtest 顶层三态 + Shadow 未成熟 good_rate=null 的 API 契约。

mock UoW（不依赖 DB）：
- backtest/metrics：status=PENDING/PARTIAL/OK + matured_count/pending_count；
- shadow/metrics：分组 sample/matured/pending/good_count，good_rate 分母=matured_count，
  matured=0 → status=PENDING / good_rate=null（不许假 0%）。
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v3_scan import router as scan_router
from app.container import container


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


SCAN_ID = uuid4()


class _Scans:
    def __init__(
        self,
        *,
        shadow_rows=(),
        labels=None,
        final_rows=(),
        label_rows=(),
        stage_rows=None,
    ):
        self._shadow = list(shadow_rows)
        self._labels = labels or {}
        self._final = list(final_rows)
        self._label_rows = list(label_rows)
        self._stage_rows = stage_rows or {}

    async def run_by_id(self, scan_id):
        return _Row(scan_run_id=SCAN_ID, market_date=date(2026, 9, 1))

    async def latest_run(self):
        return _Row(scan_run_id=SCAN_ID, market_date=date(2026, 9, 1))

    async def shadow_rows(self, scan_id):
        return self._shadow

    async def labels_by_code(self, scan_id):
        return self._labels

    async def snapshots(self, scan_id, *, stage=None, alive_only=False,
                        limit=None, code=None):
        if code is not None:
            return []
        if stage == "FINAL":
            return self._final
        return self._stage_rows.get(stage, [])

    async def outcome_labels(self, scan_id):
        return self._label_rows


class _Uow:
    def __init__(self, scans):
        self.scans = scans

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _V3:
    enabled = True

    def __init__(self, scans):
        self._scans = scans

    def uow(self):
        return _Uow(self._scans)


def _client(scans: _Scans, monkeypatch) -> TestClient:
    monkeypatch.setattr(container, "v3", _V3(scans))
    app = FastAPI()
    app.include_router(scan_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# P0-05：shadow/metrics
# ---------------------------------------------------------------------------


def test_shadow_all_pending_no_fake_zero(monkeypatch):
    """全未成熟 → 每组 status=PENDING / good_rate=null；Final 组同。"""
    scans = _Scans(
        shadow_rows=[
            _Row(code="600001", sample_group="near_miss", drop_reason="MACHINE",
                 outcome_label=None),
            _Row(code="600002", sample_group="random", drop_reason="RECALL",
                 outcome_label=None),
        ],
        final_rows=[_Row(code="700001"), _Row(code="700002")],
    )
    body = _client(scans, monkeypatch).get("/api/v3/shadow/metrics").json()
    assert body["scan_id"] == str(SCAN_ID)
    for group in body["groups"].values():
        assert group["status"] == "PENDING"
        assert group["good_rate"] is None
        assert group["matured_count"] == 0
        assert group["pending_count"] == group["sample_count"]
        assert group["good_count"] == 0
    assert body["final_group"]["status"] == "PENDING"
    assert body["final_group"]["good_rate"] is None


def test_shadow_partial_denominator_is_matured(monkeypatch):
    """good_rate 分母=matured_count；PENDING 不进分母（旧实现用全样本）。"""
    scans = _Scans(
        shadow_rows=[
            _Row(code="600001", sample_group="near_miss", drop_reason="MACHINE",
                 outcome_label="A"),
            _Row(code="600002", sample_group="near_miss", drop_reason="MACHINE",
                 outcome_label="NONE"),   # 成熟负样本
            _Row(code="600003", sample_group="near_miss", drop_reason="MACHINE",
                 outcome_label=None),     # PENDING 不进分母
            _Row(code="600004", sample_group="random", drop_reason="RECALL",
                 outcome_label=None),
        ],
        labels={"700001": "B", "700002": None},  # Final 1 成熟 GOOD + 1 PENDING
        final_rows=[_Row(code="700001"), _Row(code="700002")],
    )
    body = _client(scans, monkeypatch).get("/api/v3/shadow/metrics").json()
    near_miss = body["groups"]["near_miss"]
    assert near_miss["sample_count"] == 3
    assert near_miss["matured_count"] == 2
    assert near_miss["pending_count"] == 1
    assert near_miss["good_count"] == 1
    assert near_miss["status"] == "PARTIAL"
    # 旧实现 1/3=0.33（把 PENDING 当失败）；新语义 1/2=0.5
    assert near_miss["good_rate"] == pytest.approx(0.5)
    assert near_miss["drop_reasons"] == {"MACHINE": 3}
    random_group = body["groups"]["random"]
    assert random_group["status"] == "PENDING"
    assert random_group["good_rate"] is None
    final = body["final_group"]
    assert final["sample_count"] == 2
    assert final["matured_count"] == 1
    assert final["good_count"] == 1
    assert final["status"] == "PARTIAL"
    assert final["good_rate"] == pytest.approx(1.0)


def test_shadow_all_matured_status_ok(monkeypatch):
    """全部成熟 → status=OK。"""
    scans = _Scans(
        shadow_rows=[
            _Row(code="600001", sample_group="near_miss", drop_reason="MACHINE",
                 outcome_label="B"),
            _Row(code="600002", sample_group="near_miss", drop_reason="MACHINE",
                 outcome_label="NONE"),
        ],
        final_rows=[_Row(code="700001")],
        labels={"700001": "A"},
    )
    body = _client(scans, monkeypatch).get("/api/v3/shadow/metrics").json()
    near_miss = body["groups"]["near_miss"]
    assert near_miss["status"] == "OK"
    assert near_miss["good_rate"] == pytest.approx(0.5)
    assert near_miss["pending_count"] == 0


# ---------------------------------------------------------------------------
# P0-04：backtest/metrics 顶层三态
# ---------------------------------------------------------------------------


def test_backtest_api_pending_when_no_labels(monkeypatch):
    """无 outcome rows → status=PENDING（旧实现返回假 OK）。"""
    scans = _Scans()
    body = _client(scans, monkeypatch).get("/api/v3/backtest/metrics").json()
    assert body["status"] == "PENDING"
    assert body["matured_count"] == 0
    assert body["pending_count"] == 0
    assert body["note"] is not None


def test_backtest_api_partial(monkeypatch):
    """部分成熟 → PARTIAL；DB label NULL → status_from_label=PENDING。"""
    scans = _Scans(
        label_rows=[_Row(code="600001", label="A"), _Row(code="600002", label=None)],
        stage_rows={
            "MACHINE": [_Row(code="600001", rank=1, alive=True)],
            "SAFETY": [_Row(code="600001", rank=None, alive=True)],
        },
    )
    body = _client(scans, monkeypatch).get("/api/v3/backtest/metrics").json()
    assert body["status"] == "PARTIAL"
    assert body["matured_count"] == 1
    assert body["pending_count"] == 1


def test_backtest_api_ok_when_all_matured(monkeypatch):
    """全成熟（含 NONE 负样本）→ OK。"""
    scans = _Scans(
        label_rows=[
            _Row(code="600001", label="A"),
            _Row(code="600002", label="NONE"),
        ],
        stage_rows={
            "MACHINE": [_Row(code="600001", rank=1, alive=True)],
            "SAFETY": [_Row(code="600001", rank=None, alive=True)],
        },
    )
    body = _client(scans, monkeypatch).get("/api/v3/backtest/metrics").json()
    assert body["status"] == "OK"
    assert body["matured_count"] == 2
    assert body["good_count"] == 1
    assert body["note"] is None
