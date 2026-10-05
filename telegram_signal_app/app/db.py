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
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS analysis_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL, expiry INTEGER NOT NULL, candle_time INTEGER NOT NULL,
                    direction TEXT NOT NULL, entry_at INTEGER NOT NULL, close_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, payload TEXT NOT NULL,
                    result TEXT, result_source TEXT,
                    UNIQUE(symbol, expiry, candle_time)
                );
                CREATE TABLE IF NOT EXISTS watch_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    symbol TEXT NOT NULL DEFAULT 'EURUSD',
                    expiry INTEGER NOT NULL DEFAULT 3
                );
                INSERT OR IGNORE INTO watch_settings(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS notification_events (
                    run_id INTEGER NOT NULL, event TEXT NOT NULL,
                    PRIMARY KEY(run_id, event)
                );
            """)
            await db.commit()

    async def save(self, payload: dict) -> dict:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT OR IGNORE INTO analysis_runs
                (symbol, expiry, candle_time, direction, entry_at, close_at, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (payload["symbol"], payload["expiry"], payload["candle_time"], payload["direction"],
                  payload["entry_at"], payload["close_at"], int(time.time()), json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            await db.commit()
            cursor = await db.execute("SELECT * FROM analysis_runs WHERE symbol=? AND expiry=? AND candle_time=?",
                                      (payload["symbol"], payload["expiry"], payload["candle_time"]))
            row = await cursor.fetchone()
        return self._record(row)

    @staticmethod
    def _record(row) -> dict:
        result = json.loads(row[8])
        result.update(id=row[0], created_at=row[7], result=row[9], result_source=row[10])
        return result

    async def get(self, run_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT * FROM analysis_runs WHERE id=?", (run_id,))
            row = await cursor.fetchone()
        return self._record(row) if row else None

    async def history(self, limit: int = 60) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT * FROM analysis_runs ORDER BY id DESC LIMIT ?", (limit,))
            rows = await cursor.fetchall()
        return [self._record(row) for row in rows]

    async def upcoming(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("""
                SELECT * FROM analysis_runs WHERE direction IN ('CALL','PUT')
                AND result IS NULL AND close_at > ? ORDER BY entry_at
            """, (int(time.time()) - 60,))
            rows = await cursor.fetchall()
        return [self._record(row) for row in rows]

    async def set_result(self, run_id: int, result: str) -> bool:
        if result not in {"WIN", "LOSS", "DRAW"}:
            raise ValueError("Invalid result")
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("""
                UPDATE analysis_runs SET result=?, result_source='user_reported'
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
                FROM analysis_runs
            """)
            total, signals, wins, losses, draws = await cursor.fetchone()
        return {"analyses": total, "signals": signals, "wins": wins, "losses": losses, "draws": draws,
                "win_rate": round(wins / (wins + losses) * 100, 1) if wins + losses else None,
                "result_source": "user_reported"}

    async def watch(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT enabled,symbol,expiry FROM watch_settings WHERE id=1")
            row = await cursor.fetchone()
        return {"enabled": bool(row[0]), "symbol": row[1], "expiry": row[2]}

    async def set_watch(self, enabled: bool, symbol: str, expiry: int) -> dict:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE watch_settings SET enabled=?,symbol=?,expiry=? WHERE id=1",
                             (int(enabled), symbol, expiry))
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

