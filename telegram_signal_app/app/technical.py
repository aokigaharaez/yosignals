"""Small technical context for GPT, independent of ML training and numpy imports."""


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
