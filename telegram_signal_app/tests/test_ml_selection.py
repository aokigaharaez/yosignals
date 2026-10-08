"""Synthetic patterns here verify learning and isolation, not market profitability."""
import numpy as np
from sklearn.linear_model import LogisticRegression

from app.analysis import chronological_split, fit_selected_model


def test_nonlinear_candidate_learns_interactions_linear_model_cannot():
    rng = np.random.default_rng(19)
    x = rng.normal(size=(2200, 30))
    targets = ((x[:, 0] > 0) != (x[:, 1] > 0)).astype(int)
    indices = np.arange(len(x))
    train = np.arange(1600)
    model, width, selection = fit_selected_model(x, indices, targets, train, 4)
    assert selection["selected"] in {"boosting", "extra_trees", "ensemble"}
    assert selection["folds"] == 2
    baseline = LogisticRegression(C=.2, max_iter=400).fit(x[train, :10], targets[train])
    baseline_accuracy = np.mean(baseline.predict(x[1800:, :10]) == targets[1800:])
    accuracy = np.mean(model.predict(x[1800:, :width]) == targets[1800:])
    assert accuracy > .65 and accuracy > baseline_accuracy + .1


def test_outer_calibration_and_test_labels_never_select_or_train_model():
    rng = np.random.default_rng(23)
    x = rng.normal(size=(1000, 30))
    targets = (x[:, 0] + rng.normal(size=1000) > 0).astype(int)
    indices = np.arange(len(x))
    train, calibration, test = chronological_split(indices, 5, 2)
    assert indices[train[-1]] + 7 < indices[calibration[0]]
    assert indices[calibration[-1]] + 7 < indices[test[0]]
    changed = targets.copy()
    changed[np.setdiff1d(np.arange(len(x)), train)] = 1 - changed[np.setdiff1d(np.arange(len(x)), train)]
    first, width, selection = fit_selected_model(x, indices, targets, train, 7)
    second, width2, selection2 = fit_selected_model(x, indices, changed, train, 7)
    assert selection == selection2 and width == width2
    np.testing.assert_allclose(first.predict_proba(x[:, :width]), second.predict_proba(x[:, :width2]))
