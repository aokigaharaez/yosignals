"""Small technical context for GPT, independent of ML training and numpy imports."""
import math
from .models import Candle


def indicators(candles):
    if len(candles) < 30:
        return {}
    ema9 = ema21 = candles[0].close
    for candle in candles[1:]:
        ema9 = .2 * candle.close + .8 * ema9
        ema21 = (2 / 22) * candle.close + (20 / 22) * ema21
    deltas = [b.close - a.close for a, b in zip(candles[-15:-1], candles[-14:])]
    gain = sum(max(v, 0) for v in deltas) / 14
    loss = sum(max(-v, 0) for v in deltas) / 14
    rsi = 50 if gain + loss == 0 else 100 * gain / (gain + loss)
    ranges = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
              for a, b in zip(candles[-15:-1], candles[-14:])]
    return {"ema9": ema9, "ema21": ema21, "rsi": round(rsi, 1), "atr": sum(ranges) / 14,
            "trend": "up" if ema9 > ema21 else "down"}


def market_context(candles):
    """Causal summaries of complete UTC bars; never aggregate across missing minutes."""
    rows = candles[-600:]
    frames = {}
    for minutes in (1, 5, 15):
        groups = {}
        for c in rows:
            groups.setdefault(c.time // (minutes * 60), []).append(c)
        bars = []
        for bucket, group in sorted(groups.items()):
            start = bucket * minutes * 60
            if len(group) != minutes or [c.time for c in group] != list(range(start, start + minutes * 60, 60)):
                continue
            bars.append(Candle(start, group[0].open, max(c.high for c in group),
                               min(c.low for c in group), group[-1].close))
        if not bars:
            frames[str(minutes) + "min"] = {"available": False}
            continue
        last = bars[-1]
        tail = bars[-20:]
        changes = [math.log(b.close / a.close) for a, b in zip(tail, tail[1:])]
        mean = sum(changes) / len(changes) if changes else 0
        volatility = math.sqrt(sum((v-mean)**2 for v in changes) / len(changes)) if changes else 0
        span = last.high-last.low
        frames[str(minutes) + "min"] = {
            "available": True, "bar_count": len(bars), "data_as_of": last.time + minutes * 60,
            "indicators": indicators(bars),
            "returns_percent": {str(n): round((last.close / bars[-n-1].close - 1) * 100, 5)
                                for n in (1, 3, 5, 10) if len(bars) > n},
            "support_20": min(c.low for c in tail), "resistance_20": max(c.high for c in tail),
            "volatility_percent": round(volatility * 100, 5),
            "last_bar": {**last.to_dict(), "body_range_ratio": round(abs(last.close-last.open)/span, 3) if span else 0,
                         "close_position": round((last.close-last.low)/span, 3) if span else .5},
        }
    return {"timeframes": frames, "history_minutes": len(rows),
            "missing_minutes_last_60": sum(max(0, (b.time-a.time)//60-1) for a, b in zip(rows[-60:], rows[-60:][1:])),
            "news_available": False, "volume_available": False,
            "note": "Only complete source candles. Quote is separate from candle close; no order book or news feed."}
