"""Read-only, repeatable chronological evaluation of the real candle archive.

Example: python -m app.evaluate --db signals.db --symbol EURUSD --expiry 3
No broker orders, random market fallback, or API keys are used.
"""
import argparse
import asyncio
import json
from pathlib import Path

from .analysis import analyze
from .config import Settings
from .db import Database
from .market import MarketData, MarketError
from .models import INSTRUMENTS


async def evaluate_archive(path, symbol, expiry):
    if not Path(path).is_file():
        raise ValueError("Архив не найден. Сначала получите реальные свечи в Mini App.")
    settings = Settings()
    rows = MarketData.validate(await Database(path).candle_history(symbol, settings.model_history_limit))
    reports = []
    for fraction in (.6, .8, 1.):
        prefix = rows[:int(len(rows)*fraction)]
        if len(prefix) < 1500:
            continue
        run = await asyncio.to_thread(analyze, prefix, expiry, settings)
        reports.append({"history_candles":len(prefix), "data_as_of":prefix[-1].time+60,
                        "model":run["model"], "validation":run["validation"],
                        "probability":run["probability"], "signal_eligible":run.get("signal_eligible", False)})
    return {"symbol":symbol, "expiry":expiry, "method":"three_chronological_prefixes",
            "reports":reports, "note":"Проверка внешних котировок; исторические результаты не гарантируют будущие."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="signals.db")
    parser.add_argument("--symbol", choices=INSTRUMENTS, default="EURUSD")
    parser.add_argument("--expiry", type=int, choices=range(1,61), default=3)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = asyncio.run(evaluate_archive(args.db, args.symbol, args.expiry))
    except (ValueError, MarketError) as exc:
        parser.error(str(exc))
    output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
