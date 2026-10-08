from __future__ import annotations

import json
import time
import aiosqlite


class Database:
    """New tables leave the original manual signals journal intact."""
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            existing = await (await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('analysis_runs','analysis_runs_v2')")).fetchall()
            names = {row[0] for row in existing}
            await db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS market_candles (
                    symbol TEXT NOT NULL, time INTEGER NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                    PRIMARY KEY(symbol, time)
                );
                CREATE TABLE IF NOT EXISTS analysis_runs_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, expiry INTEGER NOT NULL, candle_time INTEGER NOT NULL,
                    direction TEXT NOT NULL, entry_at INTEGER NOT NULL, close_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, payload TEXT NOT NULL,
                    result TEXT, result_source TEXT,
                    UNIQUE(symbol, expiry, candle_time, entry_at)
                );
                CREATE TABLE IF NOT EXISTS watch_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL DEFAULT 'EURUSD',
                    expiry INTEGER NOT NULL DEFAULT 3
                );
                INSERT OR IGNORE INTO watch_settings(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS app_owners (
                    telegram_id INTEGER PRIMARY KEY,
                    added_by INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_events (
                    run_id INTEGER NOT NULL, event TEXT NOT NULL,
                    PRIMARY KEY(run_id, event)
                );
            """)
            # One-time, non-destructive migration: preserve IDs/results and the original table.
            if "analysis_runs" in names and "analysis_runs_v2" not in names:
                await db.execute("INSERT INTO analysis_runs_v2 SELECT * FROM analysis_runs")
            await db.commit()

            watch_columns = await (await db.execute("PRAGMA table_info(watch_settings)")).fetchall()
            if "strict" not in {row[1] for row in watch_columns}:
                await db.execute("ALTER TABLE watch_settings ADD COLUMN strict INTEGER NOT NULL DEFAULT 0")
                await db.commit()
            columns = await (await db.execute("PRAGMA table_info(analysis_runs_v2)")).fetchall()
            if "engine_key" not in {row[1] for row in columns}:
                await db.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE analysis_runs_v2 RENAME TO analysis_runs_engine_migration;
                    CREATE TABLE analysis_runs_v2 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL, expiry INTEGER NOT NULL, candle_time INTEGER NOT NULL,
                        direction TEXT NOT NULL, entry_at INTEGER NOT NULL, close_at INTEGER NOT NULL,
                        created_at INTEGER NOT NULL, payload TEXT NOT NULL,
                        result TEXT, result_source TEXT, engine_key TEXT NOT NULL DEFAULT 'ml',
                        UNIQUE(symbol, expiry, candle_time, entry_at, engine_key)
                    );
                    INSERT INTO analysis_runs_v2 SELECT *, 'ml' FROM analysis_runs_engine_migration;
                    DROP TABLE analysis_runs_engine_migration;
                    COMMIT;
                """)

    async def store_candles(self, symbol, candles, limit=20000):
        async with aiosqlite.connect(self.path) as db:
            await db.executemany("INSERT OR IGNORE INTO market_candles VALUES(?,?,?,?,?,?)",
                [(symbol, c.time, c.open, c.high, c.low, c.close) for c in candles])
            await db.execute("DELETE FROM market_candles WHERE symbol=? AND time < "
                "(SELECT MIN(time) FROM (SELECT time FROM market_candles WHERE symbol=? ORDER BY time DESC LIMIT ?))",
                (symbol, symbol, limit))
            await db.commit()

    async def candle_history(self, symbol, limit=20000):
        from .models import Candle
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT time,open,high,low,close FROM market_candles "
                "WHERE symbol=? ORDER BY time DESC LIMIT ?", (symbol, limit))).fetchall()
        return [Candle(*row) for row in reversed(rows)]

    async def save(self, payload: dict) -> dict:
        engine_key = payload.get("model", "gpt") if payload.get("engine") == "gpt" else "ml"
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT OR IGNORE INTO analysis_runs_v2
                (symbol, expiry, candle_time, direction, entry_at, close_at, created_at, payload, engine_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (payload["symbol"], payload["expiry"], payload["candle_time"], payload["direction"],
                  payload["entry_at"], payload["close_at"], int(time.time()), json.dumps(payload, ensure_ascii=False, allow_nan=False), engine_key))
            await db.commit()
            cursor = await db.execute("SELECT * FROM analysis_runs_v2 WHERE symbol=? AND expiry=? AND candle_time=? AND entry_at=? AND engine_key=?",
                                      (payload["symbol"], payload["expiry"], payload["candle_time"], payload["entry_at"], engine_key))
            row = await cursor.fetchone()
        return self._record(row)

    @staticmethod
    def _record(row) -> dict:
        result = json.loads(row[8])
        result.update(id=row[0], created_at=row[7], result=row[9], result_source=row[10])
        return result

    async def get(self, run_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT * FROM analysis_runs_v2 WHERE id=?", (run_id,))
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def history(self, limit: int = 60) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT * FROM analysis_runs_v2 ORDER BY id DESC LIMIT ?", (limit,))
            rows = await cursor.fetchall()
        return [self._record(row) for row in rows]

    async def upcoming(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("""
                SELECT * FROM analysis_runs_v2 WHERE direction IN ('CALL','PUT')
                AND result IS NULL AND close_at > ? ORDER BY entry_at
            """, (int(time.time()) - 60,))
            rows = await cursor.fetchall()
        return [self._record(row) for row in rows]

    async def set_result(self, run_id: int, result: str) -> bool:
        if result not in {"WIN", "LOSS", "DRAW"}:
            raise ValueError("Invalid result")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("""
                UPDATE analysis_runs_v2 SET result=?, result_source='user_reported'
                WHERE id=? AND result IS NULL AND close_at<=? AND direction IN ('CALL','PUT')
            """, (result, run_id, int(time.time())))
            await db.commit()
            return cursor.rowcount == 1

    async def stats(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("""
                SELECT COUNT(*),
                COALESCE(SUM(direction IN ('CALL','PUT')),0),
                COALESCE(SUM(result='WIN'),0), COALESCE(SUM(result='LOSS'),0),
                COALESCE(SUM(result='DRAW'),0)
                FROM analysis_runs_v2
            """)
            total, signals, wins, losses, draws = await cursor.fetchone()
        return {"analyses": total, "signals": signals, "wins": wins, "losses": losses, "draws": draws,
                "win_rate": round(wins / (wins + losses) * 100, 1) if wins + losses else None,
                "result_source": "user_reported"}

    async def watch(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT enabled,symbol,expiry,strict FROM watch_settings WHERE id=1")
            row = await cursor.fetchone()
        return {"enabled": bool(row[0]), "symbol": row[1], "expiry": row[2], "strict": bool(row[3])}

    async def is_owner(self, telegram_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT 1 FROM app_owners WHERE telegram_id=?", (telegram_id,))).fetchone()
        return row is not None

    async def owners(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT telegram_id,added_by,created_at FROM app_owners ORDER BY created_at,telegram_id")).fetchall()
        return [{"telegram_id": r[0], "added_by": r[1], "created_at": r[2]} for r in rows]

    async def add_owner(self, telegram_id: int, added_by: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO app_owners VALUES(?,?,?)", (telegram_id, added_by, int(time.time())))
            await db.commit()

    async def remove_owner(self, telegram_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("DELETE FROM app_owners WHERE telegram_id=?", (telegram_id,))
            await db.commit()
            return cursor.rowcount == 1

    async def set_watch(self, enabled: bool, symbol: str, expiry: int, strict: bool = False) -> dict:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE watch_settings SET enabled=?,symbol=?,expiry=?,strict=? WHERE id=1",
                             (int(enabled), symbol, expiry, int(strict)))
            await db.commit()
        return await self.watch()

    async def claim_event(self, run_id: int, event: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("INSERT OR IGNORE INTO notification_events VALUES (?,?)", (run_id, event))
            await db.commit()
            return cursor.rowcount == 1

    async def release_event(self, run_id: int, event: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM notification_events WHERE run_id=? AND event=?", (run_id, event))
            await db.commit()

