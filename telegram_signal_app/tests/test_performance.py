import time
from types import SimpleNamespace

import pytest
import aiosqlite

from app.config import Settings
from app.db import Database
from app.models import Candle
from app.performance import PerformanceTracker, summarize, model_key


def record(i, outcome='WIN', model='gpt:gpt-6-luna:market-v6', entry=None, expiry=1):
    start=1800000000+i*60 if entry is None else entry
    return {'run_id':i,'model_key':model,'symbol':'EURUSD','expiry':expiry,'entry_delay':1,
            'direction':'CALL','entry_at':start,'close_at':start+expiry*60,'outcome':outcome}


def test_eighty_percent_point_estimate_does_not_certify_eighty_percent():
    weak=summarize([record(i,'WIN' if i<80 else 'LOSS') for i in range(100)], Settings())['groups'][0]
    assert weak['accuracy']==80 and weak['interval'][0]<80 and not weak['passed']
    good=summarize([record(i) for i in range(100)],Settings())['groups'][0]
    assert good['passed'] and good['interval'][0]<100
    scarce=summarize([record(i) for i in range(3)],Settings())['groups'][0]
    assert scarce['accuracy']==100 and not scarce['passed']


def test_outcomes_are_separate_by_model_and_draws_are_nonwins():
    rows=[record(1),record(2,'DRAW'),record(3,'LOSS','gpt:gpt-6-astra:market-v6')]
    groups=summarize(rows,Settings())['groups']
    first=next(g for g in groups if 'luna' in g['model_key'])
    assert first['accuracy']==50 and first['samples']==2 and first['draws']==1
    assert len(groups)==2


def test_overlapping_trades_are_not_independent_samples():
    rows=[record(i,entry=1800000000+i*60,expiry=3) for i in range(12)]
    group=summarize(rows,Settings())['groups'][0]
    assert group['samples']==4 and group['overlapping_excluded']==8


@pytest.mark.asyncio
async def test_settlement_matches_entry_open_and_expiry_close_and_is_immutable(tmp_path,monkeypatch):
    base=(int(time.time())//60+2)*60
    monkeypatch.setattr('app.performance.time',SimpleNamespace(time=lambda:base+180))
    db=Database(str(tmp_path/'results.db'));await db.init()
    rows=[Candle(base,1.,1.2,.9,1.1),Candle(base+60,1.1,1.3,1.,1.2)]
    await db.store_candles('EURUSD',rows)
    run={'symbol':'EURUSD','expiry':2,'entry_delay':1,'engine':'gpt','model':'gpt-6-luna',
         'prompt_version':'market-v6','direction':'CALL','entry_at':base,'close_at':base+120,
         'candle_time':base-120,'fresh':True,'constituent_forecasts':[{'model_id':'gpt-6-luna','direction':'CALL'}]}
    saved=await db.save(run)
    class Market:
        async def candles(self,symbol): return rows
    tracker=PerformanceTracker(Settings(),db,Market());await tracker.init()
    await tracker.tick()
    async with aiosqlite.connect(db.path) as conn:
        row=await (await conn.execute('SELECT outcome,entry_price,close_price FROM forecast_outcomes')).fetchone()
    assert row==('WIN',1.,1.2)
    assert (await db.get(saved['id']))['result'] is None
    # A manually reported opposite broker outcome remains a separate source.
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute("UPDATE analysis_runs_v2 SET result='LOSS',result_source='user_reported'")
        await conn.commit()
    await tracker.tick()
    assert (await tracker.report())['groups'][0]['accuracy']==100
    assert len((await tracker.report())['groups'])==1 and (await tracker.report())['groups'][0]['cohort']=='standalone'
    assert (await db.get(saved['id']))['result']=='LOSS'


@pytest.mark.asyncio
async def test_partial_consensus_outcomes_wait_for_complete_prices(tmp_path,monkeypatch):
    base=(int(time.time())//60+2)*60
    monkeypatch.setattr('app.performance.time',SimpleNamespace(time=lambda:base+180))
    db=Database(str(tmp_path/'consensus.db'));await db.init()
    first=Candle(base,1.,1.2,.9,1.1)
    await db.store_candles('EURUSD',[first])
    run={'symbol':'EURUSD','expiry':2,'entry_delay':1,'engine':'gpt','model':'gpt-consensus',
         'prompt_version':'market-v6','direction':'WAIT','raw_direction':'WAIT',
         'entry_at':base,'close_at':base+120,'candle_time':base-120,'fresh':True,
         'constituent_forecasts':[{'model_id':'gpt-6-luna','direction':'CALL'},{'model_id':'gpt-6-astra','direction':'PUT'},{'model_id':'gpt-6.1-sol','direction':None}]}
    await db.save(run)
    class Market:
        async def candles(self,symbol): return [first]
    tracker=PerformanceTracker(Settings(),db,Market());await tracker.init()
    await tracker.tick()
    assert (await tracker.report())['groups']==[]
    await db.store_candles('EURUSD',[Candle(base+60,1.1,1.3,1.,1.2)])
    await tracker.tick()
    groups=(await tracker.report())['groups']
    assert len(groups)==4
    assert next(g for g in groups if 'luna' in g['model_key'])['accuracy']==100
    assert next(g for g in groups if 'astra' in g['model_key'])['accuracy']==0
    assert next(g for g in groups if 'sol' in g['model_key'])['direction_coverage']==0
    assert all(g['cohort']=='constituent:gpt-consensus' for g in groups if 'consensus' not in g['model_key'].split(':')[1])


@pytest.mark.asyncio
async def test_late_forecasts_are_excluded(tmp_path,monkeypatch):
    base=int(time.time())//60*60
    monkeypatch.setattr('app.performance.time',SimpleNamespace(time=lambda:base+300))
    db=Database(str(tmp_path/'late.db'));await db.init()
    await db.store_candles('EURUSD',[Candle(base-120,1.,1.2,.9,1.1)])
    await db.save({'symbol':'EURUSD','expiry':1,'entry_delay':1,'direction':'CALL',
        'entry_at':base-120,'close_at':base-60,'candle_time':base-180,'model':'ML','fresh':True})
    class Market:
        async def candles(self,symbol): return []
    tracker=PerformanceTracker(Settings(),db,Market());await tracker.init();await tracker.tick()
    group=(await tracker.report())['groups'][0]
    assert group['samples']==0 and group['excluded']==1 and not group['passed']


@pytest.mark.asyncio
async def test_strict_gpt_keeps_raw_forecast_for_forward_evaluation(tmp_path):
    from app.services import SignalService
    base=(int(time.time())//60+5)*60
    db=Database(str(tmp_path/'strict.db'));await db.init()
    service=SignalService(Settings(),None,db)
    async def snapshot(symbol,expiry,ml=True,entry_at=None):
        return {'symbol':symbol,'expiry':expiry,'fresh':True,'data_as_of':int(time.time()),
                'candle_time':int(time.time())//60*60-60,'candles':[]}
    class Review:
        async def review(self,data,model,user_id,signal=False):
            return {'prediction':{'direction':'CALL','summary':'Контекст вверх','risks':[]},
                    'prompt_version':'market-v6','input_hash':'same-input',
                    'constituent_forecasts':[{'model_id':'gpt-6-luna','direction':'CALL'}]}
    service.snapshot=snapshot
    run=await service.create_gpt('EURUSD',3,'gpt-6-luna',42,Review(),entry_at=base,strict=True)
    assert run['direction']=='WAIT' and run['raw_direction']=='CALL'
    assert run['status']=='filtered' and not run['signal_eligible']
    assert run['input_hash']=='same-input' and run['prompt_version']=='market-v6'
    assert (await db.get(run['id']))['raw_direction']=='CALL'


def test_consensus_members_cannot_certify_standalone_model():
    run={'engine':'gpt','model':'gpt-consensus','prompt_version':'market-v6'}
    member=model_key(run,'gpt-6-luna',cohort='constituent:gpt-consensus')
    single=model_key({'engine':'gpt','model':'gpt-6-luna','prompt_version':'market-v6'})
    assert member!=single and ':constituent:' in member and single.endswith(':standalone')

@pytest.mark.asyncio
async def test_old_missing_bars_do_not_starve_recent_available_outcome(tmp_path,monkeypatch):
    import json
    base=(int(time.time())//60+2)*60
    monkeypatch.setattr('app.performance.time',SimpleNamespace(time=lambda:base+30000))
    db=Database(str(tmp_path/'queue.db'));await db.init()
    payloads=[]
    for i in range(202):
        start=base+i*60
        run={'symbol':'EURUSD','expiry':1,'entry_delay':1,'engine':'gpt','model':'gpt-6-luna',
             'prompt_version':'market-v6','direction':'CALL','entry_at':start,'close_at':start+60,
             'candle_time':start-120,'fresh':True}
        payloads.append(('EURUSD',1,start-120,'CALL',start,start+60,int(time.time()),json.dumps(run),'gpt-6-luna'))
    async with aiosqlite.connect(db.path) as conn:
        await conn.executemany('INSERT INTO analysis_runs_v2 '
            '(symbol,expiry,candle_time,direction,entry_at,close_at,created_at,payload,engine_key) VALUES(?,?,?,?,?,?,?,?,?)',payloads)
        await conn.commit()
    await db.store_candles('EURUSD',[Candle(base-60,1.,1.2,.9,1.1),Candle(base+201*60,1.,1.2,.9,1.1)])
    class Market:
        async def candles(self,symbol): return []
    tracker=PerformanceTracker(Settings(),db,Market());await tracker.init();await tracker.tick()
    report=await tracker.report()
    assert report['groups'][0]['samples']==1 and report['groups'][0]['wins']==1
    assert report['pending_matured']==201


def test_consensus_api_forwards_strict_and_protects_performance(tmp_path):
    from fastapi.testclient import TestClient
    from app.web import create_app
    from test_core import signed,TOKEN
    app=create_app(Settings(bot_token=TOKEN,owner_id=42,bot_enabled=False,openai_api_key='test',
                           model_target_win_rate=.70,db_path=str(tmp_path/'api.db')))
    calls=[]
    async def create(symbol,expiry,model,user_id,gpt,entry_at=None,strict=False):
        calls.append((model,strict,user_id))
        return {'direction':'WAIT','engine':'gpt','model':model}
    with TestClient(app) as client:
        app.state.service.create_gpt=create
        assert client.get('/api/performance').status_code==401
        assert client.get('/api/performance',headers={'Authorization':'tma '+signed(43)}).status_code==403
        client.headers['Authorization']='tma '+signed()
        assert client.get('/api/session').json()['model_target_win_rate']==80
        response=client.post('/api/analyses',json={'symbol':'EURUSD','expiry':3,'engine':'gpt',
                                                 'model':'gpt-consensus','strict':True})
        assert response.status_code==200 and calls==[('gpt-consensus',True,42)]
        assert client.get('/api/performance').json()['groups']==[]
