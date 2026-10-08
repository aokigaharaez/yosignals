"""V6 isolation and qualification tests; synthetic outcomes are not market evidence."""
import numpy as np

from app.analysis import (MODEL_CANDIDATES, benchmark_candidates, chronological_partitions,
                          probability_estimate, selective_validation)
from app.config import Settings


def test_four_partitions_have_disjoint_outcomes_at_every_boundary():
    indices=np.arange(2000)
    groups=chronological_partitions(indices,5,2)
    assert len(groups)==4 and all(len(group)>0 for group in groups)
    for before,after in zip(groups,groups[1:]):
        assert indices[before[-1]]+7 < indices[after[0]]
        assert not np.intersect1d(before,after).size
    assert indices[groups[0][-1]] < 1000
    assert indices[groups[1][0]] == 1000
    assert indices[groups[2][0]] == 1300
    assert indices[groups[3][0]] == 1600


def test_confidence_without_calibration_is_not_a_win_probability():
    estimate=probability_estimate(.99,np.full(12,.99),np.ones(12,dtype=bool))
    assert estimate["value"] is None and estimate["interval"] is None
    assert estimate["confidence_score"]==99
    assert estimate["method"]=="uncalibrated_model"


def test_eighty_percent_point_estimate_does_not_confirm_eighty_percent():
    scores=np.full(200,.95)
    policy_wins=np.ones(200,dtype=bool)
    test_wins=np.tile([True,True,True,True,False],40)
    result=selective_validation(scores,policy_wins,scores,test_wins,Settings())
    assert result["threshold"]==.50 and result["accuracy"]==80
    assert result["interval"][0] < 80 and not result["passed"]
    assert result["required_lower_bound"]==80


def test_recent_drift_blocks_otherwise_strong_historical_policy():
    scores=np.full(400,.95)
    policy_wins=np.ones(400,dtype=bool)
    test_wins=np.concatenate((np.ones(200,dtype=bool),np.tile([True,True,True,False],50)))
    result=selective_validation(scores,policy_wins,scores,test_wins,Settings())
    assert result["interval"][0] >= 80
    assert result["recent_accuracy"]==75 and not result["passed"]


def test_high_final_accuracy_never_selects_benchmark_winner(monkeypatch):
    import app.analysis as module
    calls=[]
    def fake_analyze(candles,horizon,settings,entry_delay=1,training_state=None,candidate_name=None,_prepared_dataset=None):
        calls.append(candidate_name)
        name=candidate_name or "boosting"
        return {"validation":{"model_selection":{"selected":name,
            "method":"purged_expanding_training_only","log_loss":{key:.69 for key in MODEL_CANDIDATES}},
            "accuracy":100 if name=="extra_trees" else 50},
            "probability":None,"signal_eligible":name=="extra_trees"}
    monkeypatch.setattr(module,"analyze",fake_analyze)
    result=benchmark_candidates([],3,Settings())
    assert len(result["reports"])==10 and len(calls)==10
    assert result["selected_before_final"]=="boosting"
    assert {report["candidate"] for report in result["reports"]}==set(MODEL_CANDIDATES)
    assert next(report for report in result["reports"] if report["selected_inside_train"])["candidate"]=="boosting"


def test_policy_threshold_does_not_consult_final_test_labels():
    scores=np.full(200,.95)
    policy=np.ones(200,dtype=bool)
    good=selective_validation(scores,policy,scores,policy,Settings())
    bad=selective_validation(scores,policy,scores,~policy,Settings())
    assert good["threshold"]==bad["threshold"] and good["regime"]==bad["regime"]
    assert good["passed"] and not bad["passed"]



def test_expanded_features_are_causal_and_keep_session_indices():
    from app.analysis import FEATURE_COUNT, features
    from test_core import candles
    rows=candles(450)
    all_features,_=features(rows)
    prefix,_=features(rows[:400])
    assert all_features.shape[1]==FEATURE_COUNT==56
    np.testing.assert_allclose(all_features[:400],prefix)
    minute=(rows[-1].time//60)%1440
    np.testing.assert_allclose(all_features[-1,38:40],
        [np.sin(2*np.pi*minute/1440),np.cos(2*np.pi*minute/1440)])
