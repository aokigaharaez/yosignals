"""A reproducible frozen chronological evaluation of the real candle archive.

Example: python -m app.evaluate --db signals.db --symbol EURUSD --expiry 3 --all-models
No broker orders, generated candles, or API keys are used.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from .analysis import analyze, benchmark_candidates
from .config import Settings
from .db import Database
from .market import MarketData, MarketError
from .models import INSTRUMENTS


async def evaluate_archive(path, symbol, expiry, *, all_models=False, history_limit=20000,
                           entry_delay=1, target=.80, exploratory=False):
    if not Path(path).is_file():
        raise ValueError("Архив не найден. Сначала получите реальные свечи в Mini App.")
    if not 1500 <= history_limit <= 50000 or not 1 <= entry_delay <= 1440 or not .55 <= target < 1:
        raise ValueError("Некорректные параметры проверки.")
    settings = Settings(model_history_limit=history_limit, model_target_win_rate=target)
    rows = MarketData.validate(await Database(path).candle_history(symbol, history_limit))
    if len(rows) < 1500:
        raise ValueError("Для проверки нужно минимум 1500 реальных минутных свечей.")
    encoded = json.dumps([[c.time,c.open,c.high,c.low,c.close] for c in rows],
                         separators=(",",":"), allow_nan=False).encode()
    if all_models:
        result = await asyncio.to_thread(benchmark_candidates, rows, expiry, settings, entry_delay)
    else:
        run = await asyncio.to_thread(analyze, rows, expiry, settings, entry_delay)
        selection = (run.get("validation") or {}).get("model_selection", {})
        result = {"selected_before_final":selection.get("selected"),
                  "reports":[{"model":run["model"],"validation":run["validation"],
                              "probability":run["probability"],
                              "signal_eligible":run.get("signal_eligible",False)}]}
    result.update(symbol=symbol, expiry=expiry, entry_delay=entry_delay, provider=INSTRUMENTS[symbol].provider,
                  history_candles=len(rows), first_time=rows[0].time, data_as_of=rows[-1].time+60,
                  archive_sha256=hashlib.sha256(encoded).hexdigest(), target=round(target*100,1),
                  data_status="exploratory_previously_inspected" if exploratory else "historical_not_forward_verified",
                  method="one_frozen_chronological_four_way_partition",
                  note="Диагностика внешних котировок. Итоговый тест не выбирает модель. Повторно изученный архив — исследование, а не новый независимый результат.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="signals.db")
    parser.add_argument("--symbol", choices=INSTRUMENTS, default="EURUSD")
    parser.add_argument("--expiry", type=int, choices=range(1,61), default=3)
    parser.add_argument("--entry-delay", type=int, default=1)
    parser.add_argument("--history-limit", type=int, default=20000)
    parser.add_argument("--target", type=float, default=.80)
    parser.add_argument("--all-models", action="store_true")
    parser.add_argument("--exploratory", action="store_true", help="Mark previously inspected historical data")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = asyncio.run(evaluate_archive(args.db,args.symbol,args.expiry,
            all_models=args.all_models,history_limit=args.history_limit,entry_delay=args.entry_delay,
            target=args.target,exploratory=args.exploratory))
    except (ValueError, MarketError) as exc:
        parser.error(str(exc))
    output = json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    if args.output:
        Path(args.output).write_text(output,encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
