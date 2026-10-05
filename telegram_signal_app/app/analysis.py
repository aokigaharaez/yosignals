"""Causal features and a purged chronological holdout; no generated market data."""
from __future__ import annotations
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from .config import Settings
from .models import Candle


def features(candles: list[Candle]) -> tuple[np.ndarray, dict]:
    close = np.array([c.close for c in candles])
    high = np.array([c.high for c in candles])
    low = np.array([c.low for c in candles])
    opens = np.array([c.open for c in candles])
    ema9, ema21 = close.copy(), close.copy()
    for i in range(1, len(close)):
        ema9[i] = .2 * close[i] + .8 * ema9[i - 1]
        ema21[i] = (2 / 22) * close[i] + (20 / 22) * ema21[i - 1]
    result = np.zeros((len(close), 10))
    indicators = {}
    for i in range(30, len(close)):
        delta = np.diff(close[i - 14:i + 1])
        gain, loss = np.maximum(delta, 0).mean(), np.maximum(-delta, 0).mean()
        rsi = 50. if gain + loss == 0 else 100 * gain / (gain + loss)
        tr = np.maximum(high[i - 13:i + 1] - low[i - 13:i + 1],
                        np.maximum(abs(high[i - 13:i + 1] - close[i - 14:i]), abs(low[i - 13:i + 1] - close[i - 14:i])))
        atr = float(tr.mean())
        scale = max(atr, close[i] * 1e-8)
        result[i] = [*(float(close[i] - close[i - k]) / scale for k in (1, 3, 5, 10)),
                     (ema9[i] - ema21[i]) / scale, (rsi - 50) / 50,
                     (close[i] - opens[i]) / scale, (high[i] - low[i]) / scale,
                     float(np.std(delta)) / scale, (close[i] - ema21[i]) / scale]
        indicators = {"rsi": round(rsi, 1), "ema9": float(ema9[i]), "ema21": float(ema21[i]),
                      "atr": atr, "trend": "up" if ema9[i] > ema21[i] else "down"}
    return result, indicators


def dataset(candles: list[Candle], horizon: int):
    x, indicators = features(candles)
    times = np.array([c.time for c in candles])
    indices, targets = [], []
    # Feature at i closes at i+1. Entry at i+2 allows one minute of notice.
    # Expiration h minutes later uses the close of candle i+1+h.
    for i in range(30, len(candles) - horizon - 1):
        if not np.all(np.diff(times[i - 30:i + horizon + 2]) == 60):
            continue
        delta = candles[i + horizon + 1].close - candles[i + 2].open
        if delta == 0:
            continue
        indices.append(i)
        targets.append(int(delta > 0))
    return x, np.array(indices, dtype=int), np.array(targets), indicators


def analyze(candles: list[Candle], horizon: int, settings: Settings) -> dict:
    base = {"direction": "WAIT", "score": None, "model": "Logistic regression · v1",
            "validation": None, "indicators": {}, "reasons": [], "model_ready": False}
    if len(candles) < 300:
        base["reasons"] = ["Для обучения нужно минимум 300 закрытых минутных свечей."]
        return base
    x, indices, targets, indicators = dataset(candles, horizon)
    base["indicators"] = indicators
    if len(indices) < 220 or len(np.unique(targets)) < 2:
        base["reasons"] = ["Недостаточно непрерывной истории с движением в обе стороны."]
        return base
    boundary = int(len(indices) * .75)
    train = indices + horizon + 1 < indices[boundary]
    test_positions = []
    last_index = -1000
    for p in range(boundary, len(indices)):
        if indices[p] - last_index >= horizon + 1:
            test_positions.append(p)
            last_index = indices[p]
    test = np.array(test_positions)
    if train.sum() < 150 or len(test) < 12 or len(np.unique(targets[train])) < 2:
        base["reasons"] = ["Для этой экспирации недостаточно независимых проверочных примеров."]
        return base
    model = make_pipeline(StandardScaler(), LogisticRegression(C=.2, max_iter=400))
    model.fit(x[indices[train]], targets[train])
    probabilities = model.predict_proba(x[indices[test]])[:, 1]
    prediction = probabilities >= .5
    accuracy = float(np.mean(prediction == targets[test]))
    majority = int(np.mean(targets[train]) >= .5)
    baseline = float(np.mean(targets[test] == majority))
    selected = np.maximum(probabilities, 1 - probabilities) >= settings.model_min_score
    selected_count = int(selected.sum())
    selected_accuracy = float(np.mean(prediction[selected] == targets[test][selected])) if selected_count else None
    validation = {"samples": len(test), "training_samples": int(train.sum()),
                  "accuracy": round(accuracy * 100, 1), "baseline": round(baseline * 100, 1),
                  "selected_samples": selected_count,
                  "selected_accuracy": round(selected_accuracy * 100, 1) if selected_accuracy is not None else None,
                  "method": "chronological_holdout_purged_nonoverlapping"}
    up = float(model.predict_proba(x[-1:])[:, 1][0])
    score = max(up, 1 - up)
    reasons = []
    if not all(candles[i].time - candles[i - 1].time == 60 for i in range(len(candles) - 30, len(candles))):
        reasons.append("В последних свечах есть разрывы: прогноз остановлен.")
    if accuracy < max(settings.model_min_validation, baseline + .02):
        reasons.append("На отложенной истории модель не показала достаточного преимущества над базовым прогнозом.")
    if selected_count < 20 or selected_accuracy is None or selected_accuracy < settings.model_min_validation:
        reasons.append("Недостаточно подтверждений для сильных прогнозов на отложенной истории.")
    if score < settings.model_min_score:
        reasons.append(f"Оценка модели ниже порога {settings.model_min_score:.0%}.")
    base.update(score=round(score * 100, 1), validation=validation, indicators=indicators, model_ready=True,
                direction="WAIT" if reasons else ("CALL" if up >= .5 else "PUT"),
                reasons=reasons or ["Порог модели и проверка на отложенной истории пройдены.",
                                   f"EMA 9 {'выше' if indicators['trend'] == 'up' else 'ниже'} EMA 21; RSI {indicators['rsi']}."])
    return base
