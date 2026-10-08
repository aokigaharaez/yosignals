"use strict";
const $ = id => document.getElementById(id);
const state = {session:null, engine:"ml", symbol:"EURUSD", expiry:3, category:"forex", chart:"candles", snapshot:null, analysisStarted:0, analysisError:null, lastGPT:0, sequence:0, loading:false, analyzing:false, watch:null, offset:0, page:"overview", history:[], historyLoaded:false, seenId:0, events:new Set(), historyLoading:false};
const tg = window.Telegram?.WebApp;
if (tg?.initData) { tg.ready(); tg.expand(); tg.setHeaderColor?.("#f5f7f9"); tg.setBackgroundColor?.("#f5f7f9"); }
function telegramInitData() {
  const direct = tg?.initData?.trim();
  if (direct) return direct;
  // Telegram can expose the raw value in the launch URL before the SDK finishes booting.
  const hash = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : window.location.hash;
  const query = new URLSearchParams(`${window.location.search.slice(1)}&${hash}`);
  return query.get("tgWebAppData") || "";
}
const now = () => Date.now()/1000 + state.offset;
const text = (id, value) => { $(id).textContent = value; };
const price = value => value == null ? "—" : new Intl.NumberFormat("en-US", {minimumFractionDigits: value > 100 ? 2 : 5, maximumFractionDigits:value > 100 ? 2 : 5}).format(value);
const clockTime = value => new Date(value*1000).toLocaleTimeString("ru-RU", {timeZone:state.session?.timezone || "Europe/Simferopol",hour:"2-digit",minute:"2-digit",second:"2-digit"});
let toastTimer;
function toast(message) { text("toast",message); $("toast").hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$("toast").hidden=true,6500); }
async function api(path, options={}) {
  const initData = telegramInitData();
  const headers={...(options.body ? {"Content-Type":"application/json"} : {}), ...(initData ? {Authorization:`tma ${initData}`} : {})};
  const controller=new AbortController(); const timeout=setTimeout(()=>controller.abort(),(path==="/api/gpt/review"||path==="/api/analyses")?95000:25000);
  try {
    const response=await fetch(path,{...options,headers,signal:controller.signal});
    const contentType=response.headers.get("content-type")||"";
    const data=contentType.includes("application/json") ? await response.json() : {detail:await response.text()};
    if (!response.ok) { const error=new Error(typeof data.detail==="string" ? data.detail : "Не удалось выполнить запрос."); error.code=data.code; error.status=response.status; throw error; }
    return data;
  } catch(error) { if(error.name==="AbortError") throw new Error("Сервер не ответил вовремя. Повторите запрос."); throw error; }
  finally { clearTimeout(timeout); }
}
function page(name) {
  state.page=name;
  for(const key of ["overview","history","connections","access"]) $(`${key}-page`).hidden=key!==name;
  document.querySelectorAll(".nav-button").forEach(b=>b.classList.toggle("active",b.dataset.page===name));
  text("page-title",{overview:"Обзор рынка",history:"Сигналы и история",connections:"Подключения",access:"Доступ"}[name]);
  if(name==="history") loadHistory();
  if(name==="access") loadOwners();
  if(name==="overview") requestAnimationFrame(drawChart);
  window.scrollTo({top:0,behavior:"instant"});
}
document.querySelectorAll("[data-page]").forEach(b=>b.addEventListener("click",()=>page(b.dataset.page)));
function renderAssets() {
  const instruments=state.session.instruments.filter(i=>i.category===state.category);
  if(!instruments.some(i=>i.symbol===state.symbol)) state.symbol=instruments[0].symbol;
  $("asset-select").replaceChildren(...instruments.map(i=>{const o=document.createElement("option");o.value=i.symbol;o.textContent=i.label;return o;}));
  $("asset-select").value=state.symbol;
  document.querySelectorAll("[data-category]").forEach(b=>b.classList.toggle("active",b.dataset.category===state.category));
  const asset=state.session.instruments.find(i=>i.symbol===state.symbol);
  text("asset-title",asset.label);text("asset-subtitle",asset.name);text("source-metric",asset.provider);
  text("source-detail",asset.category==="crypto" ? "Spot · котировка к USDT" : "Валютный рынок · 1 мин");
  text("asset-icon",asset.symbol.startsWith("BTC")?"₿":asset.symbol.startsWith("ETH")?"Ξ":asset.symbol.startsWith("EUR")?"€":asset.symbol.startsWith("GBP")?"£":"$");
}
document.querySelectorAll("[data-category]").forEach(b=>b.addEventListener("click",()=>{
  if(!state.session) return;
  if(b.dataset.category==="otc") { toast("OTC использует отдельные котировки Pocket Option. Внешние данные для этого режима не подходят."); return; }
  state.category=b.dataset.category;renderAssets();loadMarket(true);
}));
$("asset-select").addEventListener("change",()=>{state.symbol=$("asset-select").value;renderAssets();loadMarket(true);});
document.querySelectorAll("[data-expiry]").forEach(b=>b.addEventListener("click",()=>{
  state.expiry=Number(b.dataset.expiry);$("custom-expiry").value=state.expiry;state.lastGPT=0;
  document.querySelectorAll("[data-expiry]").forEach(x=>x.classList.toggle("active",x===b));
  loadMarket(true);
}));
document.querySelectorAll("[data-chart]").forEach(b=>b.addEventListener("click",()=>{
  state.chart=b.dataset.chart; document.querySelectorAll("[data-chart]").forEach(x=>x.classList.toggle("active",x===b));drawChart();
}));
function resetMetrics() {
  state.snapshot=null;
  text("asset-price","—");text("asset-change","Изменение за 60 мин");
  $("asset-change").className="";
  text("data-metric","Подключение");text("data-detail","Проверяем закрытые свечи");
  text("score-metric","—");text("probability-detail","Оценка по внешнему рынку");text("validation-metric","—");text("validation-detail","Вне обучающей выборки");
  $("forecast-quality").hidden=true;
  ["trend-value","ema-value","rsi-value","atr-value"].forEach(id=>text(id,"—"));
  $("signal-state").className="signal-state";text("signal-symbol","∿");text("signal-direction","—");text("signal-action","Ожидание прогноза");
  text("signal-summary","Анализ появится после получения рыночных свечей.");$("signal-timing").hidden=true;
  $("chart-empty").hidden=false;$("chart-retry").hidden=true;
  text("chart-empty-title","Подключаем рынок");text("chart-empty-text","Получаем минутные свечи от поставщика данных.");
  text("chart-source","Ожидание данных");text("chart-update","—");$("live-dot").className="live-dot";
  drawChart();
}
async function loadMarket(reset=false) {
  if(!state.session) return;

  const seq=reset||!state.analyzing?++state.sequence:state.sequence; state.marketSequence=seq;state.loading=true;
  if(reset&&(!state.snapshot||state.snapshot.symbol!==state.symbol||state.snapshot.expiry!==state.expiry))resetMetrics();
  $("refresh-button").disabled=true;
  try {
    const entry=selectedEntry(false);
    const expired=(entry!=null&&entry<now()+10)||($("entry-mode").value==="custom"&&entry==null);
    const paused=!$("live-analysis").checked&&!!state.snapshot;
    const data=await api(`/api/market?symbol=${encodeURIComponent(state.symbol)}&expiry=${state.expiry}&engine=${expired||paused?"gpt":state.engine}${entry&&!expired?`&entry_at=${entry}`:""}`);
    if(seq!==state.sequence) return;
    if(state.engine==="gpt"){data.engine="gpt";data.direction="WAIT";data.probability=null;data.validation=null;data.quality="unavailable";data.model="GPT";data.forecast_pending=true;data.reasons=["Нажмите «Получить сигнал»: выбранная GPT-модель самостоятельно проанализирует рынок."];}
    if((state.engine==="gpt"||expired||paused||!data.fresh)&&state.snapshot?.symbol===data.symbol&&state.snapshot?.expiry===data.expiry&&state.snapshot.selection_entry_at===entry&&(state.snapshot.engine||"ml")===state.engine){
      const previous=state.snapshot;
      state.snapshot={...previous,candles:data.candles,price:data.price,change_percent:data.change_percent,data_as_of:data.data_as_of,data_age_seconds:data.data_age_seconds,fresh:data.fresh,delayed:data.delayed,max_data_age_seconds:data.max_data_age_seconds,sample_count:data.sample_count,indicators:data.indicators};
    }else{state.snapshot=data;state.snapshot.forecast_at=data.server_time;state.snapshot.forecast_data_as_of=data.data_as_of;state.snapshot.selection_entry_at=entry;}
    state.offset=data.server_time-Date.now()/1000;renderSnapshot();
    if(state.engine==="gpt"&&$("live-analysis").checked&&!state.analyzing&&!expired)requestAnalysis(true);
  } catch(error) {
    if(seq!==state.sequence)return;
    if(state.snapshot){text("data-metric","Задержка источника");text("data-detail",error.message);$("chart-retry").hidden=false;return;}
    resetMetrics();
    text("data-metric",error.code==="missing_key"?"Нужен API-ключ":"Нет данных");text("data-detail","Сигналы приостановлены");
    text("chart-empty-title",error.code==="missing_key"?"Подключите валютный рынок":"Котировки недоступны");
    text("chart-empty-text",error.message);$("chart-retry").hidden=false;
    text("signal-direction","Нет данных");text("signal-summary","Для анализа нужны реальные свежие котировки. Проверьте подключение.");
  } finally { if(seq===state.marketSequence){state.loading=false;$("refresh-button").disabled=false;} }
}
function renderSnapshot() {
  let d=state.snapshot;if(!d)return;
  if(state.engine==="ml"&&$("strict-ml").checked&&!d.signal_eligible){d={...d,raw_direction:d.raw_direction||d.direction,direction:"WAIT",status:"filtered"};}
  $("chart-empty").hidden=true;
  text("signal-updated",`${d.engine==="gpt"?d.model:({legacy_linear:"ML · линейная",expanded_linear:"ML · расширенная",boosting:"ML · бустинг",extra_trees:"ML · деревья",regularized_linear:"ML · регуляризация",ensemble:"ML · ансамбль",strong_linear:"ML · сильная регуляризация",shallow_boosting:"ML · простой бустинг",deep_boosting:"ML · расширенный бустинг",shallow_trees:"ML · простые деревья"}[d.validation?.model_selection?.selected]||"ML Model")} · анализ ${clockTime(d.forecast_at||d.server_time)} · вход ${clockTime(d.entry_at)} · экспирация ${d.expiry} мин`);
  text("asset-price",price(d.price));text("asset-change",`${d.change_percent>=0?"+":""}${d.change_percent.toFixed(3)}% за 60 мин`);
  $("asset-change").className=d.change_percent>=0?"positive":"negative";
  text("data-metric",!d.fresh?"Устарели":d.delayed?"Задержка источника":"Актуальны");text("data-detail",`${d.sample_count} свечей · возраст ${Math.round(now()-d.data_as_of)} сек · ${clockTime(d.data_as_of)}`);
  text("score-metric",chance(d));
  text("probability-detail",d.probability?(d.probability.method==="historical_bin"?`${d.label} · ${d.expiry} мин · ${d.probability.samples} похожих прогнозов`:"Предварительная оценка · без калибровки"):"Недостаточно данных для оценки");
  text("validation-metric",d.validation?`${d.validation.accuracy}%`:"—");
  text("validation-detail",d.validation?`${d.label} · ${d.validation.samples} примеров · база ${d.validation.baseline}%`:"Недостаточно истории");
  text("ml-training-status",d.training?`Обучение ${d.label}: ${d.training.history_candles} свечей · ${d.validation?.feature_count||0} признаков · обновление модели раз в ${Math.round((state.session?.model_retrain_seconds||600)/60)} мин`:"Ожидаем обучение модели");
  const selective=d.validation?.selective;
  text("ml-selective-status",selective?`Калибровка: ${selective.calibration_accuracy==null?"нет отбора":`${selective.calibration_accuracy}%`} → строгая проверка: ${selective.accuracy==null?"нет подходящих сигналов":`${selective.accuracy}% успеха`} · ${selective.samples} примеров · отобрано ${selective.coverage}% · ${selective.passed?"проверка пройдена":"цель не подтверждена"}`:"Для строгой проверки пока нет данных");
  const labels={WAIT:"—",CALL:"CALL",PUT:"PUT"};
  const waiting=d.engine==="gpt"&&d.forecast_pending&&d.fresh;
  text("signal-action",{CALL:"ВВЕРХ ↑",PUT:"ВНИЗ ↓",WAIT:d.status==="filtered"?"НЕТ ПОДТВЕРЖДЁННОГО СИГНАЛА":state.analysisError?(state.engine==="gpt"?"ОШИБКА GPT":"ОШИБКА ML"):d.forecast_state==="stale_data"?"ДАННЫЕ УСТАРЕЛИ":d.forecast_state==="expired_entry"?"ВРЕМЯ ВХОДА ПРОШЛО":waiting?(state.analyzing?"ПОЛУЧАЕМ ПРОГНОЗ GPT":state.session?.gpt_ready?"ЗАПРОСИТЕ GPT-ПРОГНОЗ":"НУЖЕН OPENAI API-КЛЮЧ"):d.status==="loading"?"Подготовка модели":"НЕТ НАПРАВЛЕНИЯ"}[d.direction]);
  text("signal-direction",labels[d.direction]);text("signal-symbol",{WAIT:"∿",CALL:"↗",PUT:"↘"}[d.direction]);
  $("signal-state").className=`signal-state ${d.direction.toLowerCase()}`;
  text("signal-summary",state.analysisError||(d.status==="filtered"?`Предварительное направление: ${d.raw_direction||"—"}. Целевая точность на независимой истории не подтверждена для этого прогноза. `:"")+d.reasons.join(" "));$("signal-explanation").open=d.direction==="WAIT"&&(!waiting||!!state.analysisError);
  $("forecast-quality").hidden=d.direction==="WAIT"&&d.status!=="filtered";
  text("quality-badge",d.engine==="gpt"?"GPT · точность не проверена":d.quality==="qualified"?"Фильтры качества пройдены":"Слабый сигнал · высокий риск");
  $("quality-badge").className=`quality-badge ${d.quality||"weak"}`;
  text("chance-value",chance(d));text("chance-note",d.probability?`Оценка только для ${d.label}, экспирация ${d.expiry} мин, по котировкам ${d.provider}. ${d.probability.note}`:(d.engine==="gpt"?"Для GPT вероятность выигрыша не откалибрована.":"Оценка недоступна"));
  text("chance-interval",d.probability?.interval?`Исторический диапазон 95%: ${d.probability.interval[0]}–${d.probability.interval[1]}%. Это не гарантия для текущей сделки.`:"Для исторического диапазона пока мало данных.");
  $("signal-timing").hidden=d.direction==="WAIT";
  text("close-time",clockTime(d.close_at));
  const i=d.indicators;
  text("trend-value",i.trend?(i.trend==="up"?"Восходящий":"Нисходящий"):"—");
  text("ema-value",i.ema9?`${price(i.ema9)} / ${price(i.ema21)}`:"Недостаточно данных");
  text("rsi-value",i.rsi==null?"—":i.rsi.toFixed(1));$("rsi-marker").style.left=`${i.rsi??50}%`;
  text("atr-value",price(i.atr));text("chart-source",`${d.provider} · ${d.fresh?"актуальные данные":"данные устарели"}`);
  $("live-dot").className=`live-dot ${d.fresh?"live":""}`;text("chart-update",`${clockTime(d.data_as_of)} · ${state.session.timezone}`);
  drawChart();tick();
}
function drawChart() {
  const canvas=$("market-chart"),rect=canvas.getBoundingClientRect();if(!rect.width)return;
  const ratio=window.devicePixelRatio||1;canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;
  const ctx=canvas.getContext("2d");ctx.scale(ratio,ratio);
  const w=rect.width,h=rect.height,left=10,right=64,top=16,bottom=28,plotW=w-left-right,plotH=h-top-bottom;
  ctx.clearRect(0,0,w,h);ctx.font="9px Segoe UI, sans-serif";
  const data=state.snapshot?.candles?.slice(-75)||[];
  const low=data.length?Math.min(...data.map(c=>c.low)):0,high=data.length?Math.max(...data.map(c=>c.high)):1;
  const pad=Math.max((high-low)*.12,high*.00001),min=low-pad,max=high+pad;
  const y=v=>top+(max-v)/(max-min)*plotH;
  for(let i=0;i<=4;i++) { const yy=top+i*plotH/4;ctx.strokeStyle="#edf1e9";ctx.lineWidth=1;ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(w-right+5,yy);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle="#aab3a2";if(data.length)ctx.fillText(price(max-i*(max-min)/4),w-right+11,yy+3); }
  if(!data.length)return;
  const step=plotW/data.length,x=i=>left+(i+.5)*step;
  if(state.chart==="line"){
    const gradient=ctx.createLinearGradient(0,top,0,top+plotH);gradient.addColorStop(0,"#a4c99340");gradient.addColorStop(1,"#a4c99300");
    ctx.beginPath();ctx.moveTo(x(0),y(data[0].close));data.forEach((c,i)=>ctx.lineTo(x(i),y(c.close)));ctx.lineTo(x(data.length-1),top+plotH);ctx.lineTo(x(0),top+plotH);ctx.closePath();ctx.fillStyle=gradient;ctx.fill();
    ctx.beginPath();data.forEach((c,i)=>i?ctx.lineTo(x(i),y(c.close)):ctx.moveTo(x(i),y(c.close)));ctx.strokeStyle="#639a73";ctx.lineWidth=1.8;ctx.stroke();
  }else{
    data.forEach((c,i)=>{const color=c.close>=c.open?"#79a886":"#d99a93";ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x(i),y(c.high));ctx.lineTo(x(i),y(c.low));ctx.stroke();ctx.fillRect(x(i)-Math.max(2,step*.56)/2,Math.min(y(c.open),y(c.close)),Math.max(2,step*.56),Math.max(1,Math.abs(y(c.open)-y(c.close))));});
  }
  const last=data[data.length-1];ctx.setLineDash([4,3]);ctx.strokeStyle="#95b390";ctx.beginPath();ctx.moveTo(left,y(last.close));ctx.lineTo(w-right+5,y(last.close));ctx.stroke();ctx.setLineDash([]);
  ctx.fillStyle="#eff5e9";ctx.fillRect(w-right+5,y(last.close)-9,59,18);ctx.fillStyle="#6f9362";ctx.fillText(price(last.close),w-right+10,y(last.close)+3);
  for(let i=0;i<5;i++){const idx=Math.round(i*(data.length-1)/4);ctx.fillStyle="#a8b29e";ctx.textAlign="center";ctx.fillText(clockTime(data[idx].time).slice(0,5),x(idx),h-8);}ctx.textAlign="left";
}
new ResizeObserver(drawChart).observe($("market-chart"));
$("refresh-button").addEventListener("click",()=>loadMarket(true));
$("chart-retry").addEventListener("click",()=>state.session?loadMarket(true):window.location.reload());
async function requestAnalysis(auto=false){
  if(!state.session||state.analyzing)return;
  if(auto&&(state.session.preview||!state.session.gpt_ready||now()-state.lastGPT<65))return;
  let entry;try{entry=selectedEntry(true);}catch(error){if(!auto)toast(error.message);return;}
  if(state.engine==="gpt")state.lastGPT=now();
  if(state.engine==="gpt"&&(!state.session.gpt_ready||state.session.preview)){toast("Для GPT нужен OPENAI_API_KEY и вход владельца через Telegram.");return;}
  const button=$("analyze-button"),symbol=state.symbol,expiry=state.expiry;button.disabled=true;state.analyzing=true;state.analysisStarted=now();state.analysisError=null;$("analysis-progress").hidden=false;
  const seq=++state.sequence;
  button.querySelector("span").textContent="Анализируем рынок…";
  try {
    const run=state.session.preview?await api(`/api/market?symbol=${encodeURIComponent(symbol)}&expiry=${expiry}${entry?`&entry_at=${entry}`:""}`):await api("/api/analyses",{method:"POST",body:JSON.stringify({symbol,expiry,entry_at:entry,engine:state.engine,strict:state.engine==="ml"&&$("strict-ml").checked,model:$("gpt-model").value||"gpt-6-luna"})});
    if(seq===state.sequence){state.snapshot=run;state.snapshot.forecast_at=run.server_time;state.analysisError=null;state.snapshot.forecast_data_as_of=run.data_as_of;state.snapshot.selection_entry_at=entry;state.offset=run.server_time-Date.now()/1000;renderSnapshot();}
    if(state.session.preview)toast("Прогноз готов. Для сохранения и автоанализа откройте Mini App в Telegram.");
    else {state.seenId=Math.max(state.seenId,run.id);toast(`Анализ #${run.id} · ${run.label}: ${run.direction==="WAIT"?"нет прогноза":run.direction}.`);await loadHistory();}
  }catch(error){if(error.code==="model_training"){state.analysisError=null;if(!auto)toast(error.message);text("signal-action","ОБУЧЕНИЕ ML В ФОНЕ");text("signal-summary",error.message);$("signal-explanation").open=true;return;}state.analysisError=error.message;if(!auto)toast(error.message);text("signal-updated",`Обновление анализа не удалось: ${error.message}`);if(!state.snapshot||state.snapshot.direction==="WAIT"){text("signal-direction","—");text("signal-action",state.engine==="gpt"?"ОШИБКА GPT":"ОШИБКА ML");text("signal-summary",error.message);$("signal-explanation").open=true;}}finally{button.disabled=false;state.analyzing=false;$("analysis-progress").hidden=true;button.querySelector("span").textContent="Получить сигнал";}
}
$("strict-ml").addEventListener("change",async()=>{
  if(state.snapshot)renderSnapshot();
  if(state.historyLoaded)renderFeed();
  try{localStorage.setItem("strictML",String($("strict-ml").checked));}catch{}
  if(state.watch?.enabled){try{state.watch=await api("/api/watch",{method:"PUT",body:JSON.stringify({enabled:true,symbol:state.watch.symbol,expiry:state.watch.expiry,strict:$("strict-ml").checked})});renderWatch();}catch(error){toast(error.message);}}
});
$("analyze-button").addEventListener("click",()=>requestAnalysis());
function renderWatch() {
  $("watch-toggle").setAttribute("aria-checked",String(!!state.watch?.enabled));
  text("watch-description",state.watch?.error || (state.watch?.enabled?`${state.watch.symbol} · ${state.watch.expiry} мин · в Mini App`:"Автоанализ ML Model"));
  renderEngine();
}
$("watch-toggle").addEventListener("click",async()=>{
  if(!state.session)return;
  if(state.engine==="gpt"){toast("Автоанализ работает на ML Model. GPT запускается кнопкой «Получить сигнал».");return;}
  if(state.session.preview){toast("Автоанализ включается внутри Telegram. Откройте раздел «Подключения».");return;}
  const button=$("watch-toggle");button.disabled=true;
  try { state.watch=await api("/api/watch",{method:"PUT",body:JSON.stringify({enabled:!state.watch?.enabled,symbol:state.symbol,expiry:state.expiry,strict:$("strict-ml").checked})});renderWatch();toast(state.watch.enabled?"Автоанализ включён. Сигналы появятся в ленте Mini App.":"Автоанализ остановлен."); }
  catch(error){toast(error.message);}finally{button.disabled=false;}
});
function node(tag,className,value){const e=document.createElement(tag);if(className)e.className=className;if(value!=null)e.textContent=value;return e;}
function chance(run) { return run.probability?.value==null ? "—" : `≈${Math.round(run.probability.value)}%`; }
function phase(run) {
  if(run.result)return run.result;
  if(run.direction==="WAIT")return "Нет прогноза";
  const entry=Math.ceil(run.entry_at-now()),close=Math.ceil(run.close_at-now());
  if(entry>0)return `Вход через ${entry} сек · ${clockTime(run.entry_at)}`;
  if(close>0)return `До закрытия ${close} сек`;
  return "Закрыт · отметьте результат";
}
function resultButtons(run) {
  const buttons=node("div","result-buttons");
  for(const value of ["WIN","LOSS","DRAW"]){
    const b=node("button","",value);
    b.addEventListener("click",async()=>{
      for(const sibling of buttons.children)sibling.disabled=true;
      try{await api(`/api/history/${run.id}/result`,{method:"POST",body:JSON.stringify({result:value})});await loadHistory();}
      catch(error){toast(error.message);for(const sibling of buttons.children)sibling.disabled=false;}
    });buttons.append(b);
  }
  return buttons;
}
function renderFeed() {
  const visible=state.history.filter(r=>!$("strict-ml").checked||r.engine==="gpt"||r.signal_eligible||r.direction==="WAIT");
  $("recent-list").replaceChildren(...visible.slice(0,6).map(r=>{
    const e=node("article",`signal-feed-card ${r.quality||""}`),head=node("div","signal-feed-title");
    head.append(node("strong","",r.label),node("span",`direction-pill ${r.direction.toLowerCase()}`,r.direction==="WAIT"?"Нет прогноза":r.direction));
    if(r.direction!=="WAIT")head.append(node("span",`quality-badge ${r.quality||""}`,r.engine==="gpt"?r.model:r.quality==="qualified"?"Фильтры пройдены":"Слабый"));
    const probability=node("div","signal-feed-probability",chance(r));probability.append(node("small","probability-caption",r.probability?.method==="historical_bin"?"по похожим прогнозам":"предварительно"));
    const timer=node("div","signal-feed-clock",phase(r));timer.dataset.runClock=r.id;
    e.append(head,probability,node("div","signal-feed-meta",`${r.expiry} мин · ${r.provider} · анализ #${r.id}`),timer);
    if(!r.result && r.direction!=="WAIT" && now()>=r.close_at)e.append(resultButtons(r));
    return e;
  }));
  if(!visible.length)$("recent-list").append(node("div","empty-inline",$("strict-ml").checked?"Сигналов, прошедших строгую проверку, пока нет. Все анализы сохранены в истории.":"Нажмите «Получить сигнал». Результат появится здесь; чат бота останется свободным от сигналов."));
}
async function loadHistory() {
  if(!state.session || state.session.preview){const message="Лента сигналов и история доступны внутри Telegram Mini App. Анализ рынка можно посмотреть здесь.";$("recent-list").replaceChildren(node("div","empty-inline",message));$("history-list").replaceChildren(node("div","empty-inline",message));return;}
  if(state.historyLoading)return;
  state.historyLoading=true;
  try {
    const data=await api("/api/history");const s=data.stats;
    state.offset=data.server_time-Date.now()/1000;
    const fresh=data.items.filter(r=>r.id>state.seenId && r.direction!=="WAIT" && (!$("strict-ml").checked||r.engine==="gpt"||r.signal_eligible));
    if(state.historyLoaded && fresh.length){const r=fresh[0];toast(`Новый сигнал в Mini App: ${r.label} ${r.direction} · шанс по модели ${chance(r)}${r.quality==="weak"?" · слабый сигнал":""}.`);tg?.HapticFeedback?.notificationOccurred?.("success");}
    state.seenId=Math.max(state.seenId,...data.items.map(r=>r.id));state.historyLoaded=true;state.history=data.items;
    renderFeed();
    $("history-stats").replaceChildren(...[["Сигналов",s.signals],["WIN / LOSS",`${s.wins} / ${s.losses}`],["Win rate",s.win_rate==null?"—":`${s.win_rate}%`]].map(([label,val])=>{const e=node("article");e.append(node("small","",label),node("strong","",val));return e;}));
    $("history-list").replaceChildren(...data.items.map(r=>{
      const e=node("article","history-record"),head=node("div","history-record-head");
      const date=new Date(r.created_at*1000).toLocaleString("ru-RU",{timeZone:state.session.timezone});
      head.append(node("strong","",r.label),node("span",`direction-pill ${r.direction.toLowerCase()}`,r.direction==="WAIT"?"Нет прогноза":r.direction),node("small","",`${date} · ${r.expiry} мин · ${r.provider} · ${r.engine==="gpt"?r.model:"ML Model"}`));
      e.append(head);
      if(r.probability){
        const detail=node("details"),summary=node("summary","",`Шанс по модели ${chance(r)} · ${r.quality==="qualified"?"фильтры пройдены":"слабый сигнал"}`);
        detail.append(summary,node("p","",r.probability.note));
        if(r.probability.interval)detail.append(node("p","",`Исторический диапазон 95%: ${r.probability.interval.join("–")}%.`));
        detail.append(node("p","","Оценка по внешним котировкам; реальный шанс выигрыша у брокера не проверен."));e.append(detail);
      }
      e.append(node("p","",r.reasons.join(" ")));
      const status=node("small","result-label",phase(r));status.dataset.runClock=r.id;e.append(status);
      if(!r.result && r.direction!=="WAIT" && now()>=r.close_at)e.append(resultButtons(r));
      return e;
    }));
    if(!data.items.length)$("history-list").append(node("div","empty-inline","Сохранённых анализов пока нет. Нажмите «Получить сигнал» на странице рынка."));
  }catch(error){toast(error.message);}finally{state.historyLoading=false;}
}
$("history-refresh").addEventListener("click",loadHistory);
let ownersLoading=false;
async function loadOwners() {
  if(!state.session)return;
  const preview=state.session.preview;
  $("owner-form").hidden=preview;$("access-preview").hidden=!preview;
  text("current-user-id",preview?"Откройте Telegram":state.session.user.id);
  if(preview){$("owners-list").replaceChildren(node("p","","Список владельцев доступен в Telegram Mini App."));return;}
  if(ownersLoading)return;ownersLoading=true;
  try {
    const data=await api("/api/owners");
    $("owners-list").replaceChildren(...data.items.map(owner=>{
      const row=node("article","owner-row"),info=node("div");
      info.append(node("strong","",String(owner.telegram_id)+(owner.telegram_id===data.current_user_id?" · вы":"")));
      info.append(node("small","",owner.source==="environment"?"Закреплён в настройках сервера":"Добавлен через Mini App"));
      row.append(info);
      if(owner.can_remove){
        const button=node("button","text-button remove-owner","Отозвать доступ");
        button.addEventListener("click",async()=>{
          button.disabled=true;
          try{await api(`/api/owners/${owner.telegram_id}`,{method:"DELETE"});toast(`Доступ для ${owner.telegram_id} отозван.`);await loadOwners();}
          catch(error){toast(error.message);button.disabled=false;}
        });row.append(button);
      }
      return row;
    }));
  }catch(error){$("owners-list").replaceChildren(node("p","",error.message));}finally{ownersLoading=false;}
}
$("owners-refresh").addEventListener("click",loadOwners);
$("owner-form").addEventListener("submit",async(event)=>{
  event.preventDefault();
  if(!state.session||state.session.preview)return;
  const raw=$("owner-id").value.trim(),telegram_id=Number(raw);
  if(!/^[1-9][0-9]*$/.test(raw)||!Number.isSafeInteger(telegram_id)||telegram_id>=2**52){toast("Введите корректный числовой Telegram ID.");return;}
  const button=$("owner-add");if(button.disabled)return;button.disabled=true;
  try{await api("/api/owners",{method:"POST",body:JSON.stringify({telegram_id})});$("owner-form").reset();toast(`Владелец ${telegram_id} добавлен. Теперь он может открыть Mini App через бота.`);await loadOwners();}
  catch(error){toast(error.message);}finally{button.disabled=false;}
});
function tick(){
  text("clock",`${clockTime(now())} · ${state.session?.timezone||"UTC+3"}`);
  for(const r of state.history){
    document.querySelectorAll(`[data-run-clock="${r.id}"]`).forEach(el=>{el.textContent=phase(r);el.classList.toggle("live",!r.result&&r.entry_at<=now()&&r.close_at>now());});
    if(document.hidden || r.result || r.direction==="WAIT" || ($("strict-ml").checked&&r.engine!=="gpt"&&!r.signal_eligible))continue;
    const remaining=r.entry_at-now();
    const event=remaining>0&&remaining<=10?"soon":remaining<=0&&remaining>=-5?"entry":now()>=r.close_at&&now()<r.close_at+5?"close":null;
    const key=`${r.id}:${event}`;
    if(event&&!state.events.has(key)){
      state.events.add(key);
      toast(`${r.label} ${r.direction} · ${event==="soon"?"до входа менее 10 секунд":event==="entry"?"наступило время входа":"экспирация завершена, отметьте результат в истории"}${r.quality==="weak"?" · слабый сигнал":""}.`);
      if(event==="close")loadHistory();
    }
  }
  if(state.snapshot?.direction==="WAIT"&&state.engine==="gpt"&&state.analyzing)text("signal-action","ПОЛУЧАЕМ ПРОГНОЗ GPT");
  if(state.analyzing){const elapsed=Math.max(0,Math.floor(now()-state.analysisStarted));text("analysis-progress",`${state.engine==="gpt"?"Получаем прогноз GPT":"Рассчитываем ML-прогноз"} · ${elapsed} сек`);}
  const d=state.snapshot;if(!d)return;
  const forecastOld=now()-(d.forecast_data_as_of||d.data_as_of)>(d.max_data_age_seconds||state.session.max_data_age_seconds||90);
  text("signal-updated",`${d.engine==="gpt"?d.model:({legacy_linear:"ML · линейная",expanded_linear:"ML · расширенная",boosting:"ML · бустинг",extra_trees:"ML · деревья",regularized_linear:"ML · регуляризация",ensemble:"ML · ансамбль",strong_linear:"ML · сильная регуляризация",shallow_boosting:"ML · простой бустинг",deep_boosting:"ML · расширенный бустинг",shallow_trees:"ML · простые деревья"}[d.validation?.model_selection?.selected]||"ML Model")} · анализ ${clockTime(d.forecast_at||d.server_time)} · вход ${clockTime(d.entry_at)} · ${d.expiry} мин${state.analysisError?` · ${state.analysisError}`:""}${forecastOld?" · прогноз устарел":now()>=d.entry_at?" · время входа прошло":""}`);
  const remaining=Math.ceil(d.entry_at-now());text("entry-countdown",remaining>0?`${remaining} сек`:"Вход завершён");
  if(d.direction!=="WAIT"&&remaining<=0)text("entry-countdown",d.close_at>now()?"Сделка в процессе":"Экспирация завершена");
  if(now()-d.data_as_of>(d.max_data_age_seconds||state.session.max_data_age_seconds||90)){text("data-metric","Устарели");$("live-dot").className="live-dot";text("chart-source",`${d.provider} · данные устарели`);text("data-detail","Последний анализ сохранён. Ожидаем свежие котировки от источника.");}
}
async function boot(){
  try{
    state.session=await api("/api/session");state.offset=state.session.server_time-Date.now()/1000;
    text("user-name",state.session.user.first_name);text("avatar",state.session.user.first_name.charAt(0).toUpperCase());
    text("session-mode",state.session.preview?"Локальный просмотр":"Терминал владельцев");text("environment",(state.session.preview?"PREVIEW":"MINI APP")+" · v"+state.session.version);
    $("preview-notice").hidden=!state.session.preview;
    text("strict-ml-label",`Только сигналы с исторической точностью от ${state.session.model_target_win_rate||70}%`);
    setupGPT();
    try{$("strict-ml").checked=localStorage.getItem("strictML")==="true";}catch{}
    text("forex-status",state.session.forex_ready?"Ключ настроен · доступ проверяется при запросе":"Ожидает API-ключ");$("forex-status").classList.toggle("ready",state.session.forex_ready);
    text("telegram-status",state.session.bot_ready?(state.session.webapp_ready?"Бот и адрес настроены":"Бот подключён · нужен HTTPS-адрес"):(state.session.bot_status==="loading"?"Бот запускается в фоне":"Нужно подключить бота"));$("telegram-status").classList.toggle("ready",state.session.bot_ready&&state.session.webapp_ready);
    renderAssets();await loadMarket(true);await loadHistory();
    if(!state.session.preview){state.watch=await api("/api/watch");renderWatch();}
  }catch(error){
    state.session=null;resetMetrics();$("chart-retry").hidden=false;
    const authError=error.status===401 || error.status===403 || !telegramInitData();
    text("chart-empty-title",authError?"Сессия Telegram не подтверждена":"Сервис временно недоступен");
    text("chart-empty-text",authError?"Закройте Mini App, снова откройте его через кнопку бота и не используйте обычную ссылку браузера.":error.message);
    text("signal-direction",authError?"Нужен вход через Telegram":"Ошибка сервера");text("data-metric",authError?"Нет сессии":"Ошибка");
    toast(authError?"Откройте Mini App заново через Telegram.":error.message);
  }
}
setInterval(tick,1000);
setInterval(async()=>{
  if(document.hidden||!state.session)return;
  if(state.page==="overview"&&!state.loading)await loadMarket();
  if(!state.session.preview){try{state.watch=await api("/api/watch");renderWatch();}catch{}await loadHistory();}
},15000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&state.session&&!state.loading){loadMarket();loadHistory();}});
boot();

function setupGPT(){
  const models=state.session.gpt_models||[];
  $("gpt-model").replaceChildren(...models.map(m=>{const option=document.createElement("option");option.value=m.id;option.textContent=m.label;return option;}));
  try{const saved=localStorage.getItem("signalLab.gptModel");if(models.some(m=>m.id===saved))$("gpt-model").value=saved;state.engine=localStorage.getItem("signalLab.engine")==="gpt"?"gpt":"ml";}catch{}
  $("analysis-engine").value=state.engine;text("entry-timezone",`Время входа: ${state.session.timezone} · точность до минуты · в пределах 24 часов`);renderEngine();
}
function renderEngine(){
  $("ml-controls").hidden=state.engine!=="ml";
  $("gpt-controls").hidden=state.engine!=="gpt";
  text("engine-badge",state.engine==="gpt"?"GPT":"ML MODEL");
  text("gpt-status",state.session?.gpt_ready?"API-ключ настроен":"Добавьте OPENAI_API_KEY в Railway Variables");
  $("watch-toggle").disabled=state.engine==="gpt";
  text("watch-description",state.engine==="gpt"?(state.watch?.enabled?"В фоне активен автоанализ ML Model":"GPT-анализ по кнопке"):"Автоанализ ML Model");
}
$("analysis-engine").addEventListener("change",()=>{state.engine=$("analysis-engine").value;try{localStorage.setItem("signalLab.engine",state.engine);}catch{}renderEngine();loadMarket(true);});
$("gpt-model").addEventListener("change",()=>{try{localStorage.setItem("signalLab.gptModel",$("gpt-model").value);}catch{}if(state.engine==="gpt")loadMarket(true);});

function selectedEntry(validate=true){
  if($("entry-mode").value!=="custom")return null;
  if(!$("custom-entry").value){if(validate)throw new Error("Укажите дату и время входа.");return null;}
  const value=zonedEntryTimestamp($("custom-entry").value,state.session?.timezone||"Europe/Simferopol");
  if(validate&&(value<now()+10||value>now()+86400))throw new Error("Вход должен быть минимум через 10 секунд и в пределах 24 часов.");
  return value;
}
$("entry-mode").addEventListener("change",()=>{$("custom-entry").hidden=$("entry-mode").value!=="custom";state.lastGPT=0;if($("entry-mode").value==="auto"||$("custom-entry").value)loadMarket(true);});
$("custom-entry").addEventListener("change",()=>{try{selectedEntry();state.lastGPT=0;loadMarket(true);}catch(error){toast(error.message);}});
$("custom-expiry").addEventListener("change",()=>{const value=Number($("custom-expiry").value);if(!Number.isInteger(value)||value<1||value>60){toast("Экспирация: от 1 до 60 минут.");return;}state.expiry=value;state.lastGPT=0;document.querySelectorAll("[data-expiry]").forEach(b=>b.classList.toggle("active",Number(b.dataset.expiry)===value));loadMarket(true);});
