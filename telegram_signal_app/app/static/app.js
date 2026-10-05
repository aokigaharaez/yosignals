"use strict";
const $ = id => document.getElementById(id);
const state = {session:null, symbol:"EURUSD", expiry:3, category:"forex", chart:"candles", snapshot:null, sequence:0, loading:false, watch:null, offset:0, page:"overview"};
const tg = window.Telegram?.WebApp;
if (tg?.initData) { tg.ready(); tg.expand(); tg.setHeaderColor?.("#f5f7f9"); tg.setBackgroundColor?.("#f5f7f9"); }
const now = () => Date.now()/1000 + state.offset;
const text = (id, value) => { $(id).textContent = value; };
const price = value => value == null ? "—" : new Intl.NumberFormat("en-US", {minimumFractionDigits: value > 100 ? 2 : 5, maximumFractionDigits:value > 100 ? 2 : 5}).format(value);
const clockTime = value => new Date(value*1000).toLocaleTimeString("ru-RU", {timeZone:state.session?.timezone || "Europe/Simferopol",hour:"2-digit",minute:"2-digit",second:"2-digit"});
let toastTimer;
function toast(message) { text("toast",message); $("toast").hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$("toast").hidden=true,6500); }
async function api(path, options={}) {
  const headers={...(options.body ? {"Content-Type":"application/json"} : {}), ...(tg?.initData ? {Authorization:`tma ${tg.initData}`} : {})};
  const controller=new AbortController(); const timeout=setTimeout(()=>controller.abort(),25000);
  try {
    const response=await fetch(path,{...options,headers,signal:controller.signal});
    const data=await response.json();
    if (!response.ok) { const error=new Error(typeof data.detail==="string" ? data.detail : "Не удалось выполнить запрос."); error.code=data.code; error.status=response.status; throw error; }
    return data;
  } catch(error) { if(error.name==="AbortError") throw new Error("Сервер не ответил вовремя. Повторите запрос."); throw error; }
  finally { clearTimeout(timeout); }
}
function page(name) {
  state.page=name;
  for(const key of ["overview","history","connections"]) $(`${key}-page`).hidden=key!==name;
  document.querySelectorAll(".nav-button").forEach(b=>b.classList.toggle("active",b.dataset.page===name));
  text("page-title",{overview:"Обзор рынка",history:"История сигналов",connections:"Подключения"}[name]);
  if(name==="history") loadHistory();
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
  state.expiry=Number(b.dataset.expiry);
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
  text("score-metric","— / 100");text("validation-metric","—");text("validation-detail","Вне обучающей выборки");
  ["trend-value","ema-value","rsi-value","atr-value"].forEach(id=>text(id,"—"));
  $("signal-state").className="signal-state";text("signal-symbol","∿");text("signal-direction","Ожидание данных");
  text("signal-summary","Анализ появится после получения рыночных свечей.");$("signal-timing").hidden=true;
  $("chart-empty").hidden=false;$("chart-retry").hidden=true;
  text("chart-empty-title","Подключаем рынок");text("chart-empty-text","Получаем минутные свечи от поставщика данных.");
  text("chart-source","Ожидание данных");text("chart-update","—");$("live-dot").className="live-dot";
  drawChart();
}
async function loadMarket(reset=false) {
  if(!state.session) return;
  const seq=++state.sequence; state.loading=true;
  if(reset) resetMetrics();
  $("refresh-button").disabled=true;
  try {
    const data=await api(`/api/market?symbol=${encodeURIComponent(state.symbol)}&expiry=${state.expiry}`);
    if(seq!==state.sequence) return;
    state.snapshot=data;state.offset=data.server_time-Date.now()/1000;renderSnapshot();
  } catch(error) {
    if(seq!==state.sequence)return;
    resetMetrics();
    text("data-metric",error.code==="missing_key"?"Нужен API-ключ":"Нет данных");text("data-detail","Сигналы приостановлены");
    text("chart-empty-title",error.code==="missing_key"?"Подключите валютный рынок":"Котировки недоступны");
    text("chart-empty-text",error.message);$("chart-retry").hidden=false;
    text("signal-direction","Нет данных");text("signal-summary","Для анализа нужны реальные свежие котировки. Проверьте подключение.");
  } finally { if(seq===state.sequence){state.loading=false;$("refresh-button").disabled=false;} }
}
function renderSnapshot() {
  const d=state.snapshot;if(!d)return;
  $("chart-empty").hidden=true;
  text("asset-price",price(d.price));text("asset-change",`${d.change_percent>=0?"+":""}${d.change_percent.toFixed(3)}% за 60 мин`);
  $("asset-change").className=d.change_percent>=0?"positive":"negative";
  text("data-metric",d.fresh?"Актуальны":"Устарели");text("data-detail",`${d.sample_count} свечей · обновлено ${clockTime(d.data_as_of)}`);
  text("score-metric",d.score==null?"— / 100":`${d.score.toFixed(1)} / 100`);
  text("validation-metric",d.validation?`${d.validation.accuracy}%`:"—");
  text("validation-detail",d.validation?`${d.validation.samples} примеров · база ${d.validation.baseline}%`:"Недостаточно истории");
  const labels={WAIT:"Ждать",CALL:"CALL · Вверх",PUT:"PUT · Вниз"};
  text("signal-direction",labels[d.direction]);text("signal-symbol",{WAIT:"∿",CALL:"↗",PUT:"↘"}[d.direction]);
  $("signal-state").className=`signal-state ${d.direction.toLowerCase()}`;
  text("signal-summary",d.reasons.join(" "));
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
$("refresh-button").addEventListener("click",()=>loadMarket());$("chart-retry").addEventListener("click",()=>loadMarket(true));
$("analyze-button").addEventListener("click",async()=>{
  if(!state.session)return;
  const button=$("analyze-button");button.disabled=true;
  try {
    await loadMarket();
    if(state.session.preview){if(state.snapshot)toast("Анализ обновлён. Чтобы сохранить его в журнал, откройте Mini App в Telegram.");return;}
    if(!state.snapshot || !state.snapshot.fresh)return;
    const run=await api("/api/analyses",{method:"POST",body:JSON.stringify({symbol:state.symbol,expiry:state.expiry})});
    toast(`Анализ #${run.id} сохранён: ${run.direction==="WAIT"?"Ждать":run.direction}.`);loadHistory();
  }catch(error){toast(error.message);}finally{button.disabled=false;}
});
function renderWatch() {
  $("watch-toggle").setAttribute("aria-checked",String(!!state.watch?.enabled));
  text("watch-description",state.watch?.error || (state.watch?.enabled?`${state.watch.symbol} · ${state.watch.expiry} мин · включён`:"Сигналы в Telegram"));
}
$("watch-toggle").addEventListener("click",async()=>{
  if(!state.session)return;
  if(state.session.preview){toast("Автоанализ включается внутри Telegram. Откройте раздел «Подключения».");return;}
  const button=$("watch-toggle");button.disabled=true;
  try { state.watch=await api("/api/watch",{method:"PUT",body:JSON.stringify({enabled:!state.watch?.enabled,symbol:state.symbol,expiry:state.expiry})});renderWatch();toast(state.watch.enabled?"Автоанализ включён. Новые сигналы придут в Telegram.":"Автоанализ остановлен."); }
  catch(error){toast(error.message);}finally{button.disabled=false;}
});
function node(tag,className,value){const e=document.createElement(tag);if(className)e.className=className;if(value!=null)e.textContent=value;return e;}
async function loadHistory() {
  if(!state.session || state.session.preview){const message="Откройте Mini App в Telegram, чтобы вести личный журнал сигналов.";$("recent-list").replaceChildren(node("div","empty-inline",message));$("history-list").replaceChildren(node("div","empty-inline",message));return;}
  try {
    const data=await api("/api/history");const s=data.stats;
    $("history-stats").replaceChildren(...[["Сигналов",s.signals],["WIN / LOSS",`${s.wins} / ${s.losses}`],["Win rate",s.win_rate==null?"—":`${s.win_rate}%`]].map(([label,val])=>{const e=node("article");e.append(node("small","",label),node("strong","",val));return e;}));
    $("recent-list").replaceChildren(...data.items.slice(0,4).map(r=>{const e=node("div","recent-row");e.append(node("span","row-symbol",r.label),node("span",`direction-pill ${r.direction.toLowerCase()}`,r.direction==="WAIT"?"ЖДАТЬ":r.direction),node("small","row-expiry",`${r.expiry} мин`),node("small","",clockTime(r.created_at)),node("small","row-result",r.result||"—"));return e;}));
    $("history-list").replaceChildren(...data.items.map(r=>{
      const e=node("article","history-record"),head=node("div","history-record-head");
      const date=new Date(r.created_at*1000).toLocaleString("ru-RU",{timeZone:state.session.timezone});
      head.append(node("strong","",r.label),node("span",`direction-pill ${r.direction.toLowerCase()}`,r.direction==="WAIT"?"ЖДАТЬ":r.direction),node("small","",`${date} · ${r.expiry} мин · ${r.provider}`));
      e.append(head,node("p","",r.reasons.join(" ")));
      if(r.result)e.append(node("span","result-label",`${r.result} · отмечено вами`));
      else if(r.direction!=="WAIT" && now()>=r.close_at){const buttons=node("div","result-buttons");for(const value of ["WIN","LOSS","DRAW"]){const b=node("button","",value);b.addEventListener("click",async()=>{b.disabled=true;try{await api(`/api/history/${r.id}/result`,{method:"POST",body:JSON.stringify({result:value})});await loadHistory();}catch(err){toast(err.message);b.disabled=false;}});buttons.append(b);}e.append(buttons);}
      else if(r.direction!=="WAIT")e.append(node("small","result-label",`Результат можно отметить после ${clockTime(r.close_at)}`));
      return e;
    }));
    if(!data.items.length)for(const id of ["history-list","recent-list"])$(id).append(node("div","empty-inline","Сохранённых анализов пока нет. Нажмите «Проанализировать» на странице рынка."));
  }catch(error){toast(error.message);}
}
$("history-refresh").addEventListener("click",loadHistory);
function tick(){
  text("clock",`${clockTime(now())} · ${state.session?.timezone||"UTC+3"}`);
  const d=state.snapshot;if(!d)return;
  const remaining=Math.ceil(d.entry_at-now());text("entry-countdown",remaining>0?`${remaining} сек`:"Окно закрыто");
  if(d.direction!=="WAIT" && remaining<10){text("signal-direction","Ждать");text("signal-summary","Окно входа закрыто. Обновите анализ после новой свечи.");$("signal-state").className="signal-state";text("signal-symbol","∿");}
  if(now()-d.data_as_of>(state.session.max_data_age_seconds||90)){text("data-metric","Устарели");$("live-dot").className="live-dot";text("chart-source",`${d.provider} · данные устарели`);}
}
async function boot(){
  try{
    state.session=await api("/api/session");state.offset=state.session.server_time-Date.now()/1000;
    text("user-name",state.session.user.first_name);text("avatar",state.session.user.first_name.charAt(0).toUpperCase());
    text("session-mode",state.session.preview?"Локальный просмотр":"Приватный терминал");text("environment",state.session.preview?"LOCAL PREVIEW":"TELEGRAM MINI APP");
    $("preview-notice").hidden=!state.session.preview;
    text("forex-status",state.session.forex_ready?"Ключ настроен · доступ проверяется при запросе":"Ожидает API-ключ");$("forex-status").classList.toggle("ready",state.session.forex_ready);
    text("telegram-status",state.session.bot_ready?(state.session.webapp_ready?"Бот и адрес настроены":"Бот подключён · нужен HTTPS-адрес"):(state.session.bot_status==="loading"?"Бот запускается в фоне":"Нужно подключить бота"));$("telegram-status").classList.toggle("ready",state.session.bot_ready&&state.session.webapp_ready);
    if(!state.session.forex_ready){state.category="crypto";state.symbol="BTCUSDT";}
    renderAssets();await loadMarket(true);await loadHistory();
    if(!state.session.preview){state.watch=await api("/api/watch");renderWatch();}
  }catch(error){resetMetrics();text("chart-empty-title","Откройте приложение в Telegram");text("chart-empty-text",error.message);text("signal-direction","Требуется вход");text("data-metric","Нет сессии");toast(error.message);}
}
setInterval(tick,1000);
setInterval(async()=>{if(!document.hidden && state.session){if(state.page==="overview"&&!state.loading)await loadMarket();if(!state.session.preview){try{state.watch=await api("/api/watch");renderWatch();}catch{}if(state.page==="history")await loadHistory();}}},60000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&state.session&&!state.loading)loadMarket();});
boot();
