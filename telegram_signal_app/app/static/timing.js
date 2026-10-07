"use strict";
function zonedEntryTimestamp(value, timezone){
  if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value))throw new Error("Выберите дату и время входа.");
  const parts=value.match(/\d+/g).map(Number);
  const desired=Date.UTC(parts[0],parts[1]-1,parts[2],parts[3],parts[4]);
  const formatter=new Intl.DateTimeFormat("en-GB",{timeZone:timezone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"});
  let timestamp=desired;
  for(let i=0;i<3;i++){
    const fields=Object.fromEntries(formatter.formatToParts(new Date(timestamp)).map(p=>[p.type,p.value]));
    const observed=Date.UTC(+fields.year,+fields.month-1,+fields.day,+fields.hour,+fields.minute,+fields.second);
    const delta=desired-observed;timestamp+=delta;if(!delta)return timestamp/1000;
  }
  throw new Error("Выбранное время не существует в часовом поясе приложения.");
}
if(typeof module!=="undefined")module.exports={zonedEntryTimestamp};
