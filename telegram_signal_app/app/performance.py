"""Prospective external-market outcomes, separate from reported broker results."""
import asyncio
import json
import logging
import math
import time
from collections import defaultdict

import aiosqlite


def model_key(run, model=None, cohort=None):
    engine = run.get("engine", "ml")
    version = run.get("prompt_version") if engine == "gpt" else run.get("model_version", "legacy")
    if cohort is None:
        cohort = "aggregate:gpt-consensus" if run.get("model") == "gpt-consensus" else "standalone"
    return f"{engine}:{model or run.get('model', 'unknown')}:{version or 'legacy'}:{cohort}"


def interval(wins, count):
    if not count:
        return None
    p, z = wins/count, 1.96
    center = (p+z*z/(2*count))/(1+z*z/count)
    radius = z*math.sqrt(p*(1-p)/count+z*z/(4*count*count))/(1+z*z/count)
    return [round((center-radius)*100, 2), round((center+radius)*100, 2)]


def summarize(rows, settings):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['model_key'], row['symbol'], row['expiry'], row['entry_delay'])].append(row)
    groups = []
    for (key, symbol, expiry, delay), records in grouped.items():
        ordered = sorted(records, key=lambda r:(r['entry_at'],r['run_id']))
        independent, boundary = [], -1
        for record in ordered:
            if record['outcome'] not in {'WIN','LOSS','DRAW'} or record['entry_at'] < boundary:
                continue
            independent.append(record)
            boundary = record['close_at']
        n = len(independent)
        wins = sum(r['outcome']=='WIN' for r in independent)
        ci = interval(wins,n)
        recent = independent[n//2:]
        recent_rate = sum(r['outcome']=='WIN' for r in recent)/len(recent) if recent else None
        target = settings.model_target_win_rate*100
        groups.append({'model_key':key,'symbol':symbol,'expiry':expiry,'entry_delay':delay,
            'cohort':records[0].get('cohort','standalone'),'samples':n,'wins':wins,'draws':sum(r['outcome']=='DRAW' for r in independent),
            'accuracy':round(wins/n*100,2) if n else None,'interval':ci,
            'recent_accuracy':round(recent_rate*100,2) if recent_rate is not None else None,
            'logged':len(records),'direction_coverage':round(sum(r['direction'] in {'CALL','PUT'} for r in records)/len(records)*100,2),
            'excluded':sum(r['outcome'] in {'LATE','GAP'} for r in records),
            'overlapping_excluded':sum(r['outcome'] in {'WIN','LOSS','DRAW'} for r in records)-n,
            'target':target,'minimum_samples':settings.model_min_test_signals,
            'passed':bool(n>=settings.model_min_test_signals and ci[0]>=target and recent_rate*100>=target),
            'source':'external_market_forward','first_entry_at':independent[0]['entry_at'] if n else None,
            'last_close_at':independent[-1]['close_at'] if n else None})
    return {'groups':groups,'target':settings.model_target_win_rate*100,
        'source':'external_market_forward','method':'logged_before_entry_nonoverlapping_draws_count_as_nonwins',
        'note':'Исходы по внешним котировкам; отдельно от результата Pocket Option. Модель и версия не смешиваются.'}


class PerformanceTracker:
    def __init__(self, settings, db, market):
        self.settings, self.db, self.market = settings, db, market
        self.lock = asyncio.Lock()
        self.last_fetch = {}

    async def init(self):
        async with aiosqlite.connect(self.db.path) as conn:
            await conn.execute('''CREATE TABLE IF NOT EXISTS forecast_outcomes (
                run_id INTEGER NOT NULL, model_key TEXT NOT NULL, symbol TEXT NOT NULL,
                expiry INTEGER NOT NULL, entry_delay INTEGER NOT NULL, direction TEXT NOT NULL,
                entry_at INTEGER NOT NULL, close_at INTEGER NOT NULL, outcome TEXT NOT NULL,
                entry_price REAL, close_price REAL, evaluated_at INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'external_market_forward', cohort TEXT NOT NULL DEFAULT 'standalone', PRIMARY KEY(run_id,model_key))''')
            columns = await (await conn.execute("PRAGMA table_info(forecast_outcomes)")).fetchall()
            if "cohort" not in {row[1] for row in columns}:
                await conn.execute("ALTER TABLE forecast_outcomes ADD COLUMN cohort TEXT NOT NULL DEFAULT 'standalone'")
            await conn.commit()

    async def report(self):
        async with aiosqlite.connect(self.db.path) as conn:
            conn.row_factory = aiosqlite.Row
            rows = await (await conn.execute('SELECT * FROM forecast_outcomes ORDER BY entry_at,run_id')).fetchall()
            pending = await (await conn.execute("""SELECT COUNT(*) FROM analysis_runs_v2 r
                WHERE r.close_at<=? AND NOT EXISTS(SELECT 1 FROM forecast_outcomes o WHERE o.run_id=r.id)""",
                (int(time.time()),))).fetchone()
        result = summarize([dict(row) for row in rows], self.settings)
        result["pending_matured"] = pending[0]
        return result

    async def group(self, run):
        report = await self.report()
        return next((g for g in report['groups'] if g['model_key']==model_key(run) and
                     g['symbol']==run['symbol'] and g['expiry']==run['expiry'] and
                     g['entry_delay']==run.get('entry_delay',1)), None)

    async def tick(self):
        async with self.lock:
            await self._tick()

    async def _tick(self):
        now = int(time.time())
        async with aiosqlite.connect(self.db.path) as conn:
            runs = await (await conn.execute('''WITH pending AS (
                SELECT r.id,r.created_at,r.payload,r.close_at FROM analysis_runs_v2 r
                WHERE r.close_at<=? AND NOT EXISTS(SELECT 1 FROM forecast_outcomes o WHERE o.run_id=r.id))
                SELECT id,created_at,payload FROM (SELECT * FROM pending ORDER BY close_at DESC,id DESC LIMIT 100)
                UNION
                SELECT id,created_at,payload FROM (SELECT * FROM pending ORDER BY close_at,id LIMIT 100)''', (now,))).fetchall()
        by_symbol = defaultdict(list)
        for run_id, created, payload in runs:
            run = json.loads(payload)
            by_symbol[run['symbol']].append((run_id,created,run))
        for symbol, items in by_symbol.items():
            if now//60 != self.last_fetch.get(symbol):
                self.last_fetch[symbol] = now//60
                try:
                    await self.market.candles(symbol)
                except Exception:
                    pass
            rows = await self.db.candle_history(symbol, self.settings.model_history_limit)
            lookup = {c.time:c for c in rows}
            for run_id, created, run in items:
                main_cohort = "aggregate:gpt-consensus" if run.get('model')=='gpt-consensus' else "standalone"
                raw = run.get('raw_direction') or run.get('direction') or 'WAIT'
                candidates = [(model_key(run),raw,main_cohort)]
                members = run.get('constituent_forecasts', []) if run.get('model')=='gpt-consensus' else []
                for member in members:
                    candidates.append((model_key(run,member['model_id'],cohort='constituent:gpt-consensus'),member.get('direction') or 'WAIT','constituent:gpt-consensus'))
                start, end = run['entry_at'],run['close_at']
                expected = list(range(start,end,60))
                complete = bool(expected and len(expected)==run['expiry'] and all(t in lookup for t in expected))
                values = []
                for key, direction, cohort in candidates:
                    direction = direction if direction in {"CALL","PUT"} else "WAIT"
                    entry_price = close_price = None
                    if direction not in {'CALL','PUT'}:
                        outcome = 'ABSTAIN'
                    elif created >= start or run.get('forecast_state') in {'stale_data','expired_entry'} or run.get('fresh') is False:
                        outcome = 'LATE'
                    elif complete:
                        entry_price, close_price = lookup[start].open,lookup[end-60].close
                        delta = close_price-entry_price
                        outcome = 'DRAW' if delta==0 else 'WIN' if ((delta>0)==(direction=='CALL')) else 'LOSS'
                    elif now-end > 86400 or rows and start < rows[0].time:
                        outcome = 'GAP'
                    else:
                        continue
                    values.append((run_id,key,symbol,run['expiry'],run.get('entry_delay',1),direction,start,end,
                                   outcome,entry_price,close_price,now,cohort))
                if values and len(values) == len(candidates):
                    async with aiosqlite.connect(self.db.path) as conn:
                        await conn.executemany('INSERT OR IGNORE INTO forecast_outcomes '
                            '(run_id,model_key,symbol,expiry,entry_delay,direction,entry_at,close_at,outcome,entry_price,close_price,evaluated_at,cohort) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
                        await conn.commit()

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning('Forward outcome evaluation unavailable; retrying')
            await asyncio.sleep(15)

