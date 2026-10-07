const fs=require('node:fs');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{textContent:'',value:'',checked:false,hidden:false,style:{},classList:{toggle(){}},addEventListener(){},setAttribute(){},getBoundingClientRect(){return {width:0}},querySelector(){return node(id+'-span')}});return nodes.get(id);}
let finish;
const current=Math.floor(Date.now()/1000);
const market={symbol:'BTCUSDT',expiry:3,engine:'gpt',direction:'WAIT',forecast_pending:true,model:'GPT',reasons:['Pending'],fresh:true,delayed:false,data_as_of:current,data_age_seconds:0,max_data_age_seconds:90,server_time:current,entry_at:current+180,close_at:current+360,price:100,change_percent:0,sample_count:400,provider:'Binance',indicators:{},candles:[]};
const context={console,Date,Intl,Math,Number,JSON,Set,Map,URLSearchParams,AbortController,Error,
  setTimeout(){return 1},clearTimeout(){},setInterval(){},ResizeObserver:class{observe(){}},
  window:{location:{hash:'',search:''},Telegram:undefined},
  document:{hidden:false,getElementById:node,querySelectorAll(){return []},addEventListener(){}},
  fetch:async(path)=>({ok:true,headers:{get(){return 'application/json'}},json:async()=>path==='/api/analyses'?await new Promise(resolve=>{finish=resolve}):{...market}})};
vm.createContext(context);
const source=fs.readFileSync('app/static/app.js','utf8').replace(/^boot\(\);\r?$/m,'');
vm.runInContext(source+'\nglobalThis.harness={state,loadMarket,requestAnalysis};loadHistory=async()=>{};',context);
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
})().catch(error=>{console.error(error);process.exitCode=1});
