"""Causal features and a purged chronological holdout; no generated market data."""
from __future__ import annotations
import numpy as np
import time
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier, ExtraTreesClassifier
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from .config import Settings
from .models import Candle

LOOKBACK = 120


class ProbabilityEnsemble:
    """Average complementary estimators; weights never depend on final test outcomes."""
    def fit(self, x, y):
        self.models = [make_pipeline(StandardScaler(), LogisticRegression(C=.05, max_iter=400)),
            HistGradientBoostingClassifier(max_iter=80, max_leaf_nodes=7, min_samples_leaf=40,
                learning_rate=.05, l2_regularization=10, early_stopping=False, random_state=17),
            ExtraTreesClassifier(n_estimators=80, max_depth=7, min_samples_leaf=30,
                max_features=.7, n_jobs=1, random_state=17)]
        for model in self.models:
            model.fit(x, y)
        return self

    def predict_proba(self, x):
        return np.mean([model.predict_proba(x) for model in self.models], axis=0)

    def predict(self, x):
        return (self.predict_proba(x)[:, 1] >= .5).astype(int)


def features(candles: list[Candle]) -> tuple[np.ndarray, dict]:
    close = np.array([c.close for c in candles])
    high = np.array([c.high for c in candles])
    low = np.array([c.low for c in candles])
    opens = np.array([c.open for c in candles])
    ema9, ema21 = close.copy(), close.copy()
    for i in range(1, len(close)):
        ema9[i] = .2 * close[i] + .8 * ema9[i - 1]
        ema21[i] = (2 / 22) * close[i] + (20 / 22) * ema21[i - 1]
    result = np.zeros((len(close), 40))
    indicators = {}
    for i in range(LOOKBACK, len(close)):
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
        for window in (5, 15, 30, 60, 120):
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
    for i in range(LOOKBACK, len(candles) - horizon - entry_delay):
        if not np.all(np.diff(times[i - LOOKBACK:i + horizon + entry_delay + 1]) == 60):
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
    bin_id = int(np.clip(np.floor(score * 10 + 1e-9), 0, 9))
    bins = np.clip(np.floor(calibration_scores * 10 + 1e-9), 0, 9).astype(int)
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

    Three expanding windows purge outcomes overlapping the next window. Keep the
    old model as a candidate and require lower log loss to change it.
    """
    def build(name):
        if name == "strong_linear":
            return make_pipeline(StandardScaler(), LogisticRegression(C=.002, max_iter=600))
        if name == "shallow_boosting":
            return HistGradientBoostingClassifier(max_iter=100, max_leaf_nodes=3,
                min_samples_leaf=60, learning_rate=.03, l2_regularization=20,
                early_stopping=False, random_state=17)
        if name == "deep_boosting":
            return HistGradientBoostingClassifier(max_iter=100, max_leaf_nodes=15,
                min_samples_leaf=60, learning_rate=.04, l2_regularization=15,
                early_stopping=False, random_state=17)
        if name == "shallow_trees":
            return ExtraTreesClassifier(n_estimators=100, max_depth=4, min_samples_leaf=60,
                max_features=.7, n_jobs=1, random_state=17)
        if name == "ensemble":
            return ProbabilityEnsemble()
        if name == "extra_trees":
            return ExtraTreesClassifier(n_estimators=80, max_depth=7, min_samples_leaf=30,
                max_features=.7, n_jobs=1, random_state=17)
        if name == "regularized_linear":
            return make_pipeline(StandardScaler(), LogisticRegression(C=.05, max_iter=400))
        if name == "boosting":
            return HistGradientBoostingClassifier(max_iter=60, max_leaf_nodes=7,
                min_samples_leaf=30, learning_rate=.05, l2_regularization=5,
                early_stopping=False, random_state=17)
        return make_pipeline(StandardScaler(), LogisticRegression(C=.2, max_iter=400))

    candidates = (("legacy_linear", 10), ("expanded_linear", x.shape[1]),
                  ("regularized_linear", x.shape[1]), ("boosting", x.shape[1]),
                  ("extra_trees", x.shape[1]), ("ensemble", x.shape[1]),
                  ("strong_linear", x.shape[1]), ("shallow_boosting", x.shape[1]),
                  ("deep_boosting", x.shape[1]), ("shallow_trees", x.shape[1]))
    folds = []
    for fraction in (.4, .6, .8):
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
    if len(folds) >= 2:
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
    model.success_model, success_report = fit_success_model(build, winner, width, x, indices, targets, folds, gap)
    return model, width, {"selected": winner, "folds": len(folds),
                          "success_model": success_report,
                          "log_loss": {k: round(v, 4) for k, v in losses.items()},
                          "method": "purged_expanding_training_only"}


def success_features(x, up):
    return np.column_stack((x, np.maximum(up,1-up), (up >= .5).astype(float)))


def fit_success_model(build, winner, width, x, indices, targets, folds, gap):
    """Fit a forecast-success classifier on out-of-fold predictions inside train only."""
    contexts, wins, positions = [], [], []
    for fit, check in folds:
        model = build(winner).fit(x[indices[fit], :width], targets[fit])
        up = model.predict_proba(x[indices[check], :width])[:,1]
        contexts.extend(success_features(x[indices[check]], up))
        wins.extend((up >= .5).astype(int) == targets[check])
        positions.extend(indices[check])
    contexts, wins, positions = np.array(contexts), np.array(wins, dtype=int), np.array(positions)
    report = {"enabled":False, "samples":len(wins), "method":"out_of_fold_training_only"}
    if len(wins) < 150 or len(np.unique(wins)) < 2:
        return None, report
    split = int(len(wins)*.6)
    fit = np.flatnonzero(positions+gap < positions[split])
    check = np.arange(split, len(wins))
    if len(np.unique(wins[fit])) < 2 or len(np.unique(wins[check])) < 2:
        return None, report
    candidates = {"linear":make_pipeline(StandardScaler(),LogisticRegression(C=.02,max_iter=400)),
        "boosting":HistGradientBoostingClassifier(max_iter=80,max_leaf_nodes=7,min_samples_leaf=40,
            learning_rate=.04,l2_regularization=15,early_stopping=False,random_state=31)}
    baseline = float(np.mean((contexts[check,-2]-wins[check])**2))
    best, best_loss, auc = None, baseline, None
    for name, candidate in candidates.items():
        candidate.fit(contexts[fit], wins[fit])
        probability = candidate.predict_proba(contexts[check])[:,1]
        loss, quality = float(np.mean((probability-wins[check])**2)), float(roc_auc_score(wins[check], probability))
        if loss < best_loss-.005 and quality > .55:
            best, best_loss, auc = name, loss, quality
    report.update(selected=best, baseline_brier=round(baseline,4), validation_brier=round(best_loss,4),
                  validation_auc=round(auc,4) if auc is not None else None)
    if best is None:
        return None, report
    model = candidates[best].fit(contexts,wins)
    report["enabled"] = True
    return model, report


def forecast_success_score(model, x, up):
    success_model = getattr(model, "success_model", None)
    if success_model is not None:
        return success_model.predict_proba(success_features(x,up))[:,1]
    return np.maximum(up,1-up)


def wilson_interval(wins, n):
    if not n:
        return None
    p, z = wins/n, 1.96
    center = (p+z*z/(2*n))/(1+z*z/n)
    radius = z*np.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return [round((center-radius)*100, 1), round((center+radius)*100, 1)]


def regime_mask(name, x):
    if name == "all" or x is None:
        return np.ones(len(x) if x is not None else 1, dtype=bool)
    if name == "trend":
        return np.abs(x[:, 4]) >= .5
    if name == "range":
        return np.abs(x[:, 4]) < .3
    if name == "large_candle":
        return x[:, 7] >= 1.5
    if name == "small_candle":
        return x[:, 7] <= .6
    minute = np.mod(np.arctan2(x[:, 38], x[:, 39]), 2*np.pi)*1440/(2*np.pi)
    start, end = {"london":(480,960), "us":(720,1200), "asia":(0,480)}[name]
    return (minute >= start) & (minute < end)


def selective_validation(cal_scores, cal_wins, test_scores, test_wins, settings, cal_x=None, test_x=None):
    """Freeze a threshold using calibration only, then audit it on untouched test."""
    threshold = None
    regime, best_count, calibration_wins, tried = "all", 0, 0, 0
    regimes = ("all","trend","range","large_candle","small_candle","london","us","asia") if cal_x is not None else ("all",)
    for name in regimes:
        for candidate in (.50, .55, .60, .65, .70, .75, .80, .85, .90):
            mask = (cal_scores >= candidate) & regime_mask(name, cal_x)
            n, wins = int(mask.sum()), int(cal_wins[mask].sum())
            tried += 1
            if (n >= settings.model_min_calibration_samples and wins/n >= settings.model_target_win_rate
                    and wilson_interval(wins, n)[0] >= 60 and n > best_count):
                threshold, regime, best_count, calibration_wins = candidate, name, n, wins
    mask = ((test_scores >= threshold) & regime_mask(regime, test_x)
            if threshold is not None else np.zeros(len(test_scores), dtype=bool))
    n, wins = int(mask.sum()), int(test_wins[mask].sum())
    interval = wilson_interval(wins, n)
    recent = np.flatnonzero(mask)[n//2:]
    recent_accuracy = float(np.mean(test_wins[recent])) if len(recent) else None
    passed = bool(n >= settings.model_min_test_signals and wins/n >= settings.model_target_win_rate
                  and interval[0] >= 60 and recent_accuracy >= settings.model_target_win_rate-.10)
    return {"target": round(settings.model_target_win_rate*100, 1), "threshold": threshold,
            "regime": regime, "calibration_samples": best_count, "calibration_wins": calibration_wins,
            "calibration_accuracy": round(calibration_wins/best_count*100,1) if best_count else None,
            "policies_considered": tried,
            "samples": n, "wins": wins, "accuracy": round(wins/n*100, 1) if n else None,
            "interval": interval, "coverage": round(n/max(1, len(test_scores))*100, 1),
            "recent_accuracy": round(recent_accuracy*100, 1) if recent_accuracy is not None else None,
            "passed": passed, "method": "calibration_threshold_untouched_test"}


class MLTrainer:
    """One in-memory fitted model per symbol/expiry/entry delay; never deserialize pickle."""
    def __init__(self):
        self.state = {}

    def __call__(self, candles, horizon, settings, entry_delay=1):
        if not self.needs_training(candles, settings):
            return self.predict(candles, settings)
        state = {}
        result = analyze(candles, horizon, settings, entry_delay, training_state=state)
        if result["model_ready"]:
            self.state = state
        return result

    def needs_training(self, candles, settings):
        return (not self.state or time.monotonic()-self.state["fitted_monotonic"] >= settings.model_retrain_seconds
                or self.state["candle_time"] > candles[-1].time
                or len(candles) > self.state["history_count"]+max(500, self.state["history_count"]//5))

    def predict(self, candles, settings):
        state = self.state
        if not state:
            return None
        if not all(candles[i].time-candles[i-1].time == 60 for i in range(len(candles)-LOOKBACK, len(candles))):
            return {"direction":"WAIT", "probability":None, "quality":"unavailable", "model_ready":False,
                    "reasons":["В последних свечах есть разрывы: нужен непрерывный участок рынка."]}
        x, indicators = features(candles[-181:])
        up = float(state["model"].predict_proba(x[-1:, :state["width"]])[:, 1][0])
        return finish_forecast({}, up, indicators, state, settings, current_x=x[-1:])


def analyze(candles: list[Candle], horizon: int, settings: Settings, entry_delay: int = 1, training_state=None) -> dict:
    base = {"direction": "WAIT", "score": None, "probability": None,
            "quality": "unavailable", "model": "Adaptive ML · v5",
            "validation": None, "indicators": {}, "reasons": [], "model_ready": False}
    if len(candles) < 300:
        base["reasons"] = ["Для обучения нужно минимум 300 закрытых минутных свечей."]
        return base
    if not all(candles[i].time-candles[i-1].time == 60 for i in range(len(candles)-LOOKBACK, len(candles))):
        base["reasons"] = ["В последних свечах есть разрывы: нужен непрерывный участок рынка."]
        return base
    if (training_state and time.monotonic()-training_state["fitted_monotonic"] < settings.model_retrain_seconds
            and training_state["candle_time"] <= candles[-1].time
            and len(candles) <= training_state["history_count"]+max(500, training_state["history_count"]//5)):
        x, indicators = features(candles[-181:])
        up = float(training_state["model"].predict_proba(x[-1:, :training_state["width"]])[:, 1][0])
        return finish_forecast(base, up, indicators, training_state, settings, current_x=x[-1:])
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
        return forecast_success_score(model,x[indices[positions]],up), (up >= .5).astype(int)

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
    test_estimates = np.array([probability_estimate(s, cal_scores, cal_wins, draw_rate)["value"] / 100 for s in test_scores])
    validation = {"samples": len(test), "training_samples": len(train), "calibration_samples": len(calibration),
                  "accuracy": round(accuracy*100, 1), "baseline": round(baseline*100, 1),
                  "selected_samples": selected_count,
                  "selected_accuracy": round(selected_accuracy*100, 1) if selected_accuracy is not None else None,
                  "brier_score": round(float(np.mean((test_estimates-wins)**2)), 4),
                  "model_selection": selection, "feature_count": width,
                  "selective": selective_validation(cal_scores, cal_wins, test_scores, wins, settings,
                                                     x[indices[calibration]], x[indices[test]]),
                  "training_cutoff": candles[indices[train[-1]]+horizon+entry_delay].time+60,
                  "evaluated_until": candles[indices[test[-1]]+horizon+entry_delay].time+60,
                  "method": "train_calibration_test_purged_nonoverlapping"}
    fitted = {"model": model, "width": width, "selection": selection,
              "cal_scores": cal_scores, "cal_wins": cal_wins, "cal_x": x[indices[calibration]], "draw_rate": draw_rate,
              "validation": validation, "fitted_monotonic": time.monotonic(),
              "candle_time": candles[-1].time, "history_count": len(candles)}
    if training_state is not None:
        training_state.clear()
        training_state.update(fitted)
    return finish_forecast(base, up, indicators, fitted, settings, current_x=x[-1:])


def finish_forecast(base, up, indicators, fitted, settings, current_x=None):
    score = max(up, 1-up)
    direction_confidence = score
    if current_x is not None:
        score = float(forecast_success_score(fitted["model"],current_x,np.array([up]))[0])
    probability = probability_estimate(score, fitted["cal_scores"], fitted["cal_wins"], fitted["draw_rate"])
    validation = fitted["validation"]
    selective = validation["selective"]
    in_policy = bool(selective["threshold"] is not None and score >= selective["threshold"]
                     and current_x is not None and regime_mask(selective["regime"], current_x)[0])
    if in_policy:
        mask = (fitted["cal_scores"] >= selective["threshold"]) & regime_mask(selective["regime"], fitted["cal_x"])
        probability = probability_estimate(score, fitted["cal_scores"][mask], fitted["cal_wins"][mask], fitted["draw_rate"])
        probability["regime"] = selective["regime"]
    accuracy, baseline = validation["accuracy"]/100, validation["baseline"]/100
    selected_count = validation["selected_samples"]
    selected_accuracy = validation["selected_accuracy"]
    selected_accuracy = selected_accuracy/100 if selected_accuracy is not None else None
    warnings = []
    if validation["samples"] < 30 or accuracy < max(settings.model_min_validation, baseline + .02):
        warnings.append("Преимущество модели на контрольной истории не подтверждено: сигнал слабый.")
    if selected_count < 20 or selected_accuracy is None or selected_accuracy < settings.model_min_validation:
        warnings.append("Сильных успешных прогнозов на проверочной выборке недостаточно.")
    if score < settings.model_min_score:
        warnings.append(f"Уверенность ниже фильтра качества {settings.model_min_score:.0%}; направление показано как предварительный прогноз.")
    if probability["method"] != "historical_bin":
        warnings.append("Процент пока не откалиброван на достаточной выборке.")
    if probability["value"] < settings.model_min_validation * 100:
        warnings.append("Оценка шанса низкая; это не рекомендация входить в сделку.")
    eligible = bool(selective["passed"] and in_policy
                    and probability["method"] == "historical_bin"
                    and probability["samples"] >= settings.model_min_calibration_samples
                    and probability["value"] >= settings.model_target_win_rate*100
                    and probability["interval"][0] >= 60)
    base.update(direction="CALL" if up >= .5 else "PUT", score=round(score*100, 1),
                direction_confidence=round(direction_confidence*100,1),
                signal_eligible=eligible, training={"history_candles": fitted["history_count"],
                    "trained_at": fitted["candle_time"]+60,
                    "next_retrain_seconds": max(0, int(settings.model_retrain_seconds-(time.monotonic()-fitted["fitted_monotonic"])))},
                probability=probability, quality="weak" if warnings else "qualified",
                indicators=indicators,
                validation=validation, model_ready=True, model=f"Adaptive ML · v5 · {fitted['selection']['selected']}",
                reasons=[f"EMA 9 {'выше' if indicators['trend'] == 'up' else 'ниже'} EMA 21; RSI {indicators['rsi']}.",
                         *(warnings or ["Фильтры качества на контрольной истории пройдены."])])
    return base


