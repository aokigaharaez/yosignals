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


def dataset(candles: list[Candle], horizon: int, entry_delay: int = 1):
    """Entry is entry_delay minutes after the feature candle closes. -1 means a draw."""
    x, indicators = features(candles)
    times = np.array([c.time for c in candles])
    indices, targets = [], []
    for i in range(30, len(candles) - horizon - entry_delay):
        if not np.all(np.diff(times[i - 30:i + horizon + entry_delay + 1]) == 60):
            continue
        delta = candles[i + horizon + entry_delay].close - candles[i + entry_delay + 1].open
        indices.append(i)
        targets.append(-1 if delta == 0 else int(delta > 0))
    return x, np.array(indices, dtype=int), np.array(targets), indicators


def chronological_split(indices, horizon, entry_delay):
    """60% train, 20% calibration, 20% test; purge overlapping outcomes at both edges."""
    a, b = int(len(indices) * .6), int(len(indices) * .8)
    gap = horizon + entry_delay
    train = np.flatnonzero(indices + gap < indices[a])

    def spaced(start, end, before=None):
        positions, previous = [], -10000
        for p in range(start, end):
            if before is not None and indices[p] + gap >= before:
                continue
            if indices[p] - previous >= gap:
                positions.append(p)
                previous = indices[p]
        return np.array(positions, dtype=int)

    return train, spaced(a, b, indices[b]), spaced(b, len(indices))


def probability_estimate(score, calibration_scores, calibration_wins, draw_rate=0.):
    """Fixed confidence bins on disjoint history, with Beta(1,1) smoothing.

    This estimates direction success on the external feed, not broker profitability.
    Draws count as non-wins. Test outcomes never enter this estimate.
    """
    bin_id = int(np.clip(np.floor(score * 10 - 5 + 1e-9), 0, 4))
    bins = np.clip(np.floor(calibration_scores * 10 - 5 + 1e-9), 0, 4).astype(int)
    matches = calibration_wins[bins == bin_id]
    n, wins = len(matches), int(matches.sum())
    if n < 30:
        return {"value": round(score * (1 - draw_rate) * 100, 1), "method": "uncalibrated_model",
                "samples": n, "wins": wins, "interval": None,
                "note": "Предварительная оценка модели: мало похожих примеров для калибровки."}
    p = wins / n
    z = 1.96
    center = (p + z*z/(2*n)) / (1 + z*z/n)
    radius = z * np.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
    return {"value": round((wins + 1) / (n + 2) * 100, 1), "method": "historical_bin",
            "samples": n, "wins": wins,
            "interval": [round((center-radius)*100, 1), round((center+radius)*100, 1)],
            "note": f"Калибровка по {n} похожим прогнозам на отдельном участке истории; ничьи не считаются выигрышем."}


def analyze(candles: list[Candle], horizon: int, settings: Settings, entry_delay: int = 1) -> dict:
    base = {"direction": "WAIT", "score": None, "probability": None,
            "quality": "unavailable", "model": "Logistic regression · v2",
            "validation": None, "indicators": {}, "reasons": [], "model_ready": False}
    if len(candles) < 300:
        base["reasons"] = ["Для обучения нужно минимум 300 закрытых минутных свечей."]
        return base
    x, indices, targets, indicators = dataset(candles, horizon, entry_delay)
    base["indicators"] = indicators
    if not all(candles[i].time - candles[i-1].time == 60 for i in range(len(candles)-30, len(candles))):
        base["reasons"] = ["В последних свечах есть разрывы: нужен непрерывный участок рынка."]
        return base
    if len(indices) < 220:
        base["reasons"] = ["Недостаточно непрерывной истории для этой экспирации."]
        return base
    train, calibration, test = chronological_split(indices, horizon, entry_delay)
    train = train[targets[train] >= 0]
    if len(train) < 120 or len(np.unique(targets[train])) < 2:
        base["reasons"] = ["Недостаточно обучающих примеров с движением в обе стороны."]
        return base
    model = make_pipeline(StandardScaler(), LogisticRegression(C=.2, max_iter=400))
    model.fit(x[indices[train]], targets[train])

    def predict(positions):
        up = model.predict_proba(x[indices[positions]])[:, 1]
        return np.maximum(up, 1-up), (up >= .5).astype(int)

    cal_scores, cal_directions = predict(calibration)
    cal_wins = cal_directions == targets[calibration]
    draw_rate = float(np.mean(targets[calibration] == -1))
    test_scores, test_directions = predict(test)
    wins = test_directions == targets[test]
    accuracy = float(np.mean(wins))
    majority = int(np.mean(targets[train]) >= .5)
    baseline = float(np.mean(targets[test] == majority))
    selected = test_scores >= settings.model_min_score
    selected_count = int(selected.sum())
    selected_accuracy = float(np.mean(wins[selected])) if selected_count else None
    up = float(model.predict_proba(x[-1:])[:, 1][0])
    score = max(up, 1-up)
    probability = probability_estimate(score, cal_scores, cal_wins, draw_rate)
    test_estimates = np.array([probability_estimate(s, cal_scores, cal_wins, draw_rate)["value"] / 100 for s in test_scores])
    validation = {"samples": len(test), "training_samples": len(train), "calibration_samples": len(calibration),
                  "accuracy": round(accuracy*100, 1), "baseline": round(baseline*100, 1),
                  "selected_samples": selected_count,
                  "selected_accuracy": round(selected_accuracy*100, 1) if selected_accuracy is not None else None,
                  "brier_score": round(float(np.mean((test_estimates-wins)**2)), 4),
                  "method": "train_calibration_test_purged_nonoverlapping"}
    warnings = []
    if len(test) < 30 or accuracy < max(settings.model_min_validation, baseline + .02):
        warnings.append("Преимущество модели на контрольной истории не подтверждено: сигнал слабый.")
    if selected_count < 20 or selected_accuracy is None or selected_accuracy < settings.model_min_validation:
        warnings.append("Сильных успешных прогнозов на проверочной выборке недостаточно.")
    if score < settings.model_min_score:
        warnings.append(f"Уверенность ниже фильтра качества {settings.model_min_score:.0%}; направление показано как предварительный прогноз.")
    if probability["method"] != "historical_bin":
        warnings.append("Процент пока не откалиброван на достаточной выборке.")
    if probability["value"] < settings.model_min_validation * 100:
        warnings.append("Оценка шанса низкая; это не рекомендация входить в сделку.")
    base.update(direction="CALL" if up >= .5 else "PUT", score=round(score*100, 1),
                probability=probability, quality="weak" if warnings else "qualified",
                validation=validation, model_ready=True,
                reasons=[f"EMA 9 {'выше' if indicators['trend'] == 'up' else 'ниже'} EMA 21; RSI {indicators['rsi']}.",
                         *(warnings or ["Фильтры качества на контрольной истории пройдены."])])
    return base


