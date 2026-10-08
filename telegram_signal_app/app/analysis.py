"""Causal features and a purged chronological holdout; no generated market data."""
from __future__ import annotations
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import log_loss
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
    result = np.zeros((len(close), 30))
    indicators = {}
    for i in range(30, len(close)):
        delta = np.diff(close[i - 14:i + 1])
        gain, loss = np.maximum(delta, 0).mean(), np.maximum(-delta, 0).mean()
        rsi = 50. if gain + loss == 0 else 100 * gain / (gain + loss)
        tr = np.maximum(high[i - 13:i + 1] - low[i - 13:i + 1],
                        np.maximum(abs(high[i - 13:i + 1] - close[i - 14:i]), abs(low[i - 13:i + 1] - close[i - 14:i])))
        atr = float(tr.mean())
        scale = max(atr, close[i] * 1e-8)
        original = [*(float(close[i] - close[i - k]) / scale for k in (1, 3, 5, 10)),
                     (ema9[i] - ema21[i]) / scale, (rsi - 50) / 50,
                     (close[i] - opens[i]) / scale, (high[i] - low[i]) / scale,
                     float(np.std(delta)) / scale, (close[i] - ema21[i]) / scale]
        extra = []
        for window in (5, 15, 30):
            recent = close[i-window:i+1]
            returns = np.diff(recent)
            ceiling, floor = high[i-window+1:i+1].max(), low[i-window+1:i+1].min()
            spread = max(ceiling-floor, scale * 1e-6)
            extra.extend([(close[i]-recent[0])/scale, np.std(returns)/scale,
                          (close[i]-floor)/spread, (close[i]-recent.mean())/scale,
                          abs(close[i]-recent[0])/max(np.abs(returns).sum(), scale*1e-6)])
        span = max(high[i]-low[i], scale*1e-6)
        minute = (candles[i].time // 60) % 1440
        extra.extend([(high[i]-max(opens[i], close[i]))/span,
                      (min(opens[i], close[i])-low[i])/span,
                      (close[i]-low[i])/span,
                      np.sin(2*np.pi*minute/1440), np.cos(2*np.pi*minute/1440)])
        result[i] = original + extra
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


def fit_selected_model(x, indices, targets, train, gap):
    """Select only within training history; calibration/test are never consulted.

    Two expanding windows purge outcomes overlapping the next window. Keep the
    old model as a candidate and require lower log loss to change it.
    """
    def build(name):
        if name == "boosting":
            return HistGradientBoostingClassifier(max_iter=60, max_leaf_nodes=7,
                min_samples_leaf=30, learning_rate=.05, l2_regularization=5,
                early_stopping=False, random_state=17)
        return make_pipeline(StandardScaler(), LogisticRegression(C=.2, max_iter=400))

    candidates = (("legacy_linear", 10), ("expanded_linear", x.shape[1]), ("boosting", x.shape[1]))
    folds = []
    for fraction in (.6, .8):
        start = int(len(train)*fraction)
        end = min(len(train), start + max(1, int(len(train)*.2)))
        fit = train[indices[train]+gap < indices[train[start]]]
        check, previous = [], -10000
        for p in train[start:end]:
            if indices[p]-previous >= gap:
                check.append(p)
                previous = indices[p]
        if len(fit) >= 60 and len(check) >= 10 and len(np.unique(targets[fit])) == 2:
            folds.append((fit, np.array(check)))
    losses = {}
    if len(folds) == 2:
        for name, width in candidates:
            actual, predictions = [], []
            for fit, check in folds:
                candidate = build(name)
                candidate.fit(x[indices[fit], :width], targets[fit])
                actual.extend(targets[check])
                predictions.extend(candidate.predict_proba(x[indices[check], :width])[:, 1])
            losses[name] = float(log_loss(actual, predictions, labels=[0, 1]))
    # Small margin avoids replacing the simpler model on a tiny difference.
    winner = "legacy_linear"
    for name, _ in candidates[1:]:
        if name in losses and losses[name] < losses.get(winner, float("inf")) - .005:
            winner = name
    width = dict(candidates)[winner]
    model = build(winner)
    model.fit(x[indices[train], :width], targets[train])
    return model, width, {"selected": winner, "folds": len(folds),
                          "log_loss": {k: round(v, 4) for k, v in losses.items()},
                          "method": "purged_expanding_training_only"}


def analyze(candles: list[Candle], horizon: int, settings: Settings, entry_delay: int = 1) -> dict:
    base = {"direction": "WAIT", "score": None, "probability": None,
            "quality": "unavailable", "model": "Adaptive ML · v3",
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
    if not len(calibration) or not len(test):
        base["reasons"] = ["Для выбранного времени входа и экспирации недостаточно истории для независимой проверки."]
        return base
    train = train[targets[train] >= 0]
    if len(train) < 120 or len(np.unique(targets[train])) < 2:
        base["reasons"] = ["Недостаточно обучающих примеров с движением в обе стороны."]
        return base
    model, width, selection = fit_selected_model(x, indices, targets, train, horizon+entry_delay)

    def predict(positions):
        up = model.predict_proba(x[indices[positions], :width])[:, 1]
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
    up = float(model.predict_proba(x[-1:, :width])[:, 1][0])
    score = max(up, 1-up)
    probability = probability_estimate(score, cal_scores, cal_wins, draw_rate)
    test_estimates = np.array([probability_estimate(s, cal_scores, cal_wins, draw_rate)["value"] / 100 for s in test_scores])
    validation = {"samples": len(test), "training_samples": len(train), "calibration_samples": len(calibration),
                  "accuracy": round(accuracy*100, 1), "baseline": round(baseline*100, 1),
                  "selected_samples": selected_count,
                  "selected_accuracy": round(selected_accuracy*100, 1) if selected_accuracy is not None else None,
                  "brier_score": round(float(np.mean((test_estimates-wins)**2)), 4),
                  "model_selection": selection, "feature_count": width,
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
                validation=validation, model_ready=True, model=f"Adaptive ML · v3 · {selection['selected']}",
                reasons=[f"EMA 9 {'выше' if indicators['trend'] == 'up' else 'ниже'} EMA 21; RSI {indicators['rsi']}.",
                         *(warnings or ["Фильтры качества на контрольной истории пройдены."])])
    return base


