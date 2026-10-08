const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{textContent:'',value:'',checked:false,hidden:false,style:{},classList:{toggle(){}},addEventListener(){},setAttribute(){},getBoundingClientRect(){return {width:0}},querySelector(){return node(id+'-span')}});return nodes.get(id);}
let finish;
const timers=[];
const current=Math.floor(Date.now()/1000);
const market={symbol:'BTCUSDT',expiry:3,engine:'gpt',direction:'WAIT',forecast_pending:true,model:'GPT',reasons:['Pending'],fresh:true,delayed:false,data_as_of:current,data_age_seconds:0,max_data_age_seconds:90,server_time:current,entry_at:current+180,close_at:current+360,price:100,change_percent:0,sample_count:400,provider:'Binance',indicators:{},candles:[]};
const context={console,Date,Intl,Math,Number,JSON,Set,Map,URLSearchParams,AbortController,Error,
  setTimeout(){return 1},clearTimeout(){},setInterval(fn,ms){timers.push({fn,ms})},ResizeObserver:class{observe(){}},
  window:{location:{hash:'',search:''},Telegram:undefined},
  document:{hidden:false,getElementById:node,querySelectorAll(){return []},addEventListener(){}},
  fetch:async(path)=>({ok:true,headers:{get(){return 'application/json'}},json:async()=>path==='/api/analyses'?await new Promise(resolve=>{finish=resolve}):{...market}})};
vm.createContext(context);
const source=fs.readFileSync('app/static/app.js','utf8').replace(/^boot\(\);\r?$/m,'');
vm.runInContext(source+'\nglobalThis.harness={state,loadMarket,requestAnalysis,renderSnapshot,pollLive};loadHistory=async()=>{};',context);
const state=context.harness.state;
state.engine='gpt';state.symbol='BTCUSDT';state.session={gpt_ready:true,preview:false,timezone:'UTC',max_data_age_seconds:90};
node('entry-mode').value='auto';node('gpt-model').value='gpt-6-luna';
(async()=>{
  await context.harness.loadMarket();
  assert.equal(node('signal-action').textContent,'ЗАПРОСИТЕ GPT-ПРОГНОЗ');
  const pending=context.harness.requestAnalysis();
  await new Promise(resolve=>setImmediate(resolve));
  await context.harness.loadMarket();
  finish({...market,id:1,label:'BTC/USDT',direction:'CALL',model:'gpt-6-luna',forecast_pending:false,reasons:['Up']});
  await pending;
  assert.equal(node('signal-direction').textContent,'CALL');
  assert.equal(node('signal-action').textContent,'ВВЕРХ ↑');
  await context.harness.loadMarket();
  assert.equal(state.snapshot.direction,'CALL');
  assert.equal(node('signal-direction').textContent,'CALL');
  console.log('GPT forecast survives refreshes before and after completion');
  assert(timers.some(t=>t.ms===1000&&t.fn===context.harness.pollLive));
  let liveRelease,calls=0;
  context.fetch=async()=>{calls++;return {ok:true,headers:{get(){return 'application/json'}},
    json:async()=>await new Promise(resolve=>{liveRelease=resolve})}};
  const poll=context.harness.pollLive();
  await new Promise(resolve=>setImmediate(resolve));
  await context.harness.pollLive();
  assert.equal(calls,1);
  liveRelease({symbol:'BTCUSDT',server_time:current,candle_time:market.candle_time,fresh:true,
    quote:{price:101,time:current,fresh:true},stream_status:'connected'});
  await poll;
  assert.equal(state.snapshot.direction,'CALL');
  assert.equal(node('asset-price').textContent,'101.00');
  assert.equal(state.liveLoading,false);
  console.log('One-second live updates retain the forecast and prevent overlapping requests');
  state.engine='ml';node('strict-ml').checked=true;
  state.snapshot={...market,direction:'CALL',engine:'ml',forecast_pending:false,signal_eligible:false};
  context.harness.renderSnapshot();
  assert.equal(node('signal-direction').textContent,'—');
  assert.equal(node('signal-action').textContent,'НЕТ ПОДТВЕРЖДЁННОГО СИГНАЛА');
  assert.match(node('signal-summary').textContent,/CALL/);
  state.snapshot.signal_eligible=true;
  context.harness.renderSnapshot();
  assert.equal(node('signal-direction').textContent,'CALL');
  console.log('Strict ML rejects unverified forecasts and retains eligible directions');
  state.snapshot={...state.snapshot,symbol:'EURUSD',label:'EUR/USD',provider:'Twelve Data',
    probability:{value:56,method:'historical_bin',samples:100,note:'Calibrated'},
    validation:{accuracy:55,samples:50,baseline:50}};
  context.harness.renderSnapshot();
  assert.match(node('chance-note').textContent,/EUR\/USD, экспирация 3 мин, по котировкам Twelve Data/);
  assert.match(node('probability-detail').textContent,/EUR\/USD/);
  assert.match(node('validation-detail').textContent,/EUR\/USD/);
  console.log('EUR/USD probability is explicitly labeled with pair, expiry and source');
  node('strict-ml').checked=false;
  context.fetch=async()=>({ok:false,status:503,headers:{get(){return 'application/json'}},
    json:async()=>({code:'model_training',detail:'Обучение в фоне'})});
  await context.harness.requestAnalysis();
  assert.equal(state.analysisError,null);
  assert.equal(node('signal-action').textContent,'ОБУЧЕНИЕ ML В ФОНЕ');
  console.log('Background ML training is shown as progress rather than an error');
})().catch(error=>{console.error(error);process.exitCode=1});
