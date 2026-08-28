from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.models import Kline


@dataclass(frozen=True)
class CachedKlineSeries:
    klines: list[Kline]
    source: str
    updated_at: datetime


class KlineCache:
    """Small L1 memory + L2 SQLite cache for normalized K-line bars."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._memory: dict[tuple[str, str, str], CachedKlineSeries] = {}
        self._db_lock = asyncio.Lock()

    async def start(self) -> None:
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS kline_bars (
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL,
                    adjust TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    source TEXT NOT NULL,
                    provisional INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, period, adjust, trade_date)
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(kline_bars)")}
            if "provisional" not in columns:
                db.execute("ALTER TABLE kline_bars ADD COLUMN provisional INTEGER NOT NULL DEFAULT 0")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS kline_series (
                    symbol TEXT NOT NULL,
                    period TEXT NOT NULL,
                    adjust TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, period, adjust)
                )
                """
            )
            db.commit()

    async def get(self, symbol: str, period: str, adjust: str, limit: int) -> CachedKlineSeries | None:
        key = (symbol, period, adjust)
        memory = self._memory.get(key)
        if memory is not None and len(memory.klines) >= limit:
            return CachedKlineSeries(memory.klines[-limit:], memory.source, memory.updated_at)
        async with self._db_lock:
            loaded = await asyncio.to_thread(self._load, symbol, period, adjust, limit)
        if loaded is not None:
            self._memory[key] = loaded
        return loaded

    def _load(self, symbol: str, period: str, adjust: str, limit: int) -> CachedKlineSeries | None:
        if not self.path.exists():
            return None
        with sqlite3.connect(self.path) as db:
            meta = db.execute(
                "SELECT source, updated_at FROM kline_series WHERE symbol=? AND period=? AND adjust=?",
                (symbol, period, adjust),
            ).fetchone()
            rows = db.execute(
                """
                SELECT timestamp, open, high, low, close, volume, amount, provisional
                FROM kline_bars
                WHERE symbol=? AND period=? AND adjust=?
                ORDER BY trade_date DESC LIMIT ?
                """,
                (symbol, period, adjust, limit),
            ).fetchall()
        if not meta or not rows:
            return None
        klines = [
            Kline(
                timestamp=datetime.fromisoformat(row[0]),
                open=row[1], high=row[2], low=row[3], close=row[4], volume=row[5], amount=row[6], provisional=bool(row[7]),
            )
            for row in reversed(rows)
        ]
        return CachedKlineSeries(klines=klines, source=meta[0], updated_at=datetime.fromisoformat(meta[1]))

    async def put(
        self,
        symbol: str,
        period: str,
        adjust: str,
        klines: list[Kline],
        source: str,
        updated_at: datetime,
    ) -> None:
        if not klines:
            return
        async with self._db_lock:
            await asyncio.to_thread(self._put, symbol, period, adjust, klines, source, updated_at)
        key = (symbol, period, adjust)
        existing = self._memory.get(key)
        by_date = {item.timestamp.date(): item for item in (existing.klines if existing else [])}
        by_date.update({item.timestamp.date(): item for item in klines})
        merged = [by_date[date] for date in sorted(by_date)]
        self._memory[key] = CachedKlineSeries(merged, source, updated_at)

    def _put(
        self,
        symbol: str,
        period: str,
        adjust: str,
        klines: list[Kline],
        source: str,
        updated_at: datetime,
    ) -> None:
        with sqlite3.connect(self.path) as db:
            db.executemany(
                """
                INSERT INTO kline_bars
                    (symbol, period, adjust, trade_date, timestamp, open, high, low, close, volume, amount, source, provisional, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, period, adjust, trade_date) DO UPDATE SET
                    timestamp=excluded.timestamp, open=excluded.open, high=excluded.high,
                    low=excluded.low, close=excluded.close, volume=excluded.volume,
                    amount=excluded.amount, source=excluded.source, provisional=excluded.provisional,
                    updated_at=excluded.updated_at
                """,
                [
                    (
                        symbol, period, adjust, item.timestamp.date().isoformat(), item.timestamp.isoformat(),
                        item.open, item.high, item.low, item.close, item.volume, item.amount, source,
                        int(item.provisional), updated_at.isoformat(),
                    )
                    for item in klines
                ],
            )
            db.execute(
                """
                INSERT INTO kline_series(symbol, period, adjust, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol, period, adjust) DO UPDATE SET
                    source=excluded.source, updated_at=excluded.updated_at
                """,
                (symbol, period, adjust, source, updated_at.isoformat()),
            )
            db.commit()

    def memory_entries(self) -> int:
        return len(self._memory)
