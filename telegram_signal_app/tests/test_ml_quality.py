import asyncio
import threading
import time

import numpy as np
import pytest

from app.analysis import selective_validation, fit_success_model, success_features
from app.config import Settings
from app.db import Database
from app.market import MarketError
from app.services import SignalService
from test_core import candles


def test_session_filter_is_selected_without_test_labels():
    settings = Settings()
    x = np.zeros((400,40))
    x[:,7] = 1
    minutes = np.concatenate((np.full(200,600),np.zeros(200)))
    x[:,38],x[:,39] = np.sin(minutes*2*np.pi/1440),np.cos(minutes*2*np.pi/1440)
    scores = np.full(400,.7)
    wins = np.concatenate((np.ones(200,dtype=bool),np.zeros(200,dtype=bool)))
    good = selective_validation(scores,wins,scores,wins,settings,x,x)
    bad = selective_validation(scores,wins,scores,~wins,settings,x,x)
    assert good["regime"]==bad["regime"]=="london"
    assert good["threshold"]==bad["threshold"]
    assert good["passed"] and not bad["passed"]
    assert good["samples"]==200 and good["coverage"]==50


def test_success_model_learns_context_using_out_of_fold_predictions():
    rng=np.random.default_rng(31)
    x=rng.normal(size=(1000,2));indices=np.arange(1000)
    targets=(x[:,1]>0).astype(int)
    class Base:
        def fit(self,x,y): return self
        def predict_proba(self,x): return np.tile([.4,.6],(len(x),1))
    folds=[(np.arange(0,start-4),np.arange(start,start+200,4)) for start in (300,500,700)]
    model,report=fit_success_model(lambda name:Base(),"fixed",2,x,indices,targets,folds,4)
    assert report["enabled"] and report["validation_auc"]>.9
    context=success_features(np.array([[0.,3.],[0.,-3.]]),np.array([.6,.6]))
    scores=model.predict_proba(context)[:,1]
    assert scores[0]>.5 and scores[1]<.5


@pytest.mark.asyncio
async def test_slow_training_does_not_block_quotes_or_save_placeholder(tmp_path):
    rows=candles(1000)
    class Market:
        async def candles(self,symbol): return rows
    started,release=threading.Event(),threading.Event()
    class Trainer:
        def __init__(self): self.ready=False
        def needs_training(self,rows,settings): return not self.ready
        def predict(self,rows,settings): return None
        def __call__(self,*args,**kwargs):
            started.set();release.wait(5);self.ready=True
            return {"direction":"CALL","model_ready":True,"probability":{"value":71},"reasons":[]}
    db=Database(str(tmp_path/"slow.db"));await db.init()
    service=SignalService(Settings(),Market(),db)
    service.analyzer=lambda *a,**k: None
    service.trainer_factory=Trainer;service.model_status="ready"
    try:
        before=time.monotonic()
        pending=await service.snapshot("EURUSD",3)
        assert time.monotonic()-before < 1
        assert pending["training_pending"] and pending["status"]=="loading"
        assert await asyncio.to_thread(started.wait,1)
        with pytest.raises(MarketError) as exc:
            await service.create("EURUSD",3)
        assert exc.value.code=="model_training" and await db.history()==[]
        release.set()
        await asyncio.gather(*service.training_tasks.values())
        ready=await service.snapshot("EURUSD",3)
        assert ready["direction"]=="CALL" and ready["model_ready"]
    finally:
        release.set();await service.close()
