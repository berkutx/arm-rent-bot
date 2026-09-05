/* Catalog filters, dates and explicit phone masks. */
(function(root){
'use strict';
const DISTRICTS=[
 {"name":"Кентрон","aliases":["центр","kentron","կենտրոն"]},
 {"name":"Арабкир","aliases":["арабкир","arabkir","արաբկիր","комитас","komitas"]},
 {"name":"Давташен","aliases":["давташен","давтанеш","davtashen","դավթաշեն"]},
 {"name":"Ачапняк","aliases":["ачапняк","аджапняк","աջափնյակ","ajapnyak"]},
 {"name":"Канакер-Зейтун","aliases":["канакер-зейтун","канакер зейтун","зейтун","քանաքեռ-զեյթուն"]},
 {"name":"Шенгавит","aliases":["шенгавит","шенгавид","շենգավիթ","shengavit","чарбах"]},
 {"name":"Малатия-Себастия","aliases":["малатия-себастия","малатия себастия","малатсия себастия","малатия","մալաթիա-սեբաստիա"]},
 {"name":"Нор-Норк","aliases":["нор-норк","нор норк","nor nork","նոր նորք"]},
 {"name":"Аван","aliases":["аван","avan","ավան"]},
 {"name":"Эребуни","aliases":["эребуни","erebuni","էրեբունի","нор ареш"]},
 {"name":"Норк-Мараш","aliases":["норк-мараш","норк мараш","nork marash","նորք-մարաշ"]},
 {"name":"Нубарашен","aliases":["нубарашен","nubarashen","նուբարաշեն"]}
];
const CITIES=['Ереван','Дилижан','Севан','Цахкадзор','Гюмри','Ванадзор','Абовян','Аштарак','Джермук','Прошян'];
const norm=s=>String(s||'').toLowerCase().replace(/ё/g,'е').replace(/\s+/g,' ').trim();
function offer(l,f={}){return(l.prices||[]).find(p=>(!f.period||p.period===f.period)&&(!f.currency||p.currency===f.currency)&&(!f.residence_registration||p.registration!=='no'))||null;}
function matches(l,f={}){
 if(l.status!=='active')return false;
 if(f.city&&l.city!==f.city)return false;if(f.kind&&l.kind!==f.kind)return false;
 if(f.rooms!==''&&f.rooms!=null&&(f.rooms==='4+'?Number(l.rooms)<4:String(l.rooms)!==String(f.rooms)))return false;
 if(f.district&&l.district!==f.district)return false;
 if((f.market||'free')==='free'&&l.commission!==0)return false;
 if(f.market==='paid'&&!(l.role==='agent'&&l.commission>0))return false;
 if(f.owner&&l.role!=='owner')return false;
 if(f.pets&&!['yes','ask'].includes(l.pets))return false;
 if(f.contract&&l.contract!=='yes')return false;
 if(f.residence_registration&&!['yes','ask'].includes(l.residence_registration))return false;
 if(f.verified&&!['owner_verified','representative_verified'].includes(l.document_status))return false;
 const p=offer(l,f);if(!p)return false;if(f.max&&p.amount>Number(f.max))return false;
 if(f.q&&!norm([l.address,l.city,l.district,l.description].join(' ')).includes(norm(f.q)))return false;
 return true;
}
// Age is informational only. Calendar days use the rental market's timezone.
const DAY_MS=86400000, YEREVAN_OFFSET_MS=4*3600000;
function createdMs(l){
 const raw=String(l.created_at||'');
 if(!raw)return NaN;
 const iso=/^\d{4}-\d{2}-\d{2}$/.test(raw)?raw+'T00:00:00+04:00':/T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(raw)?raw+'+04:00':raw;
 return Date.parse(iso);
}
function publicationAge(l,now=Date.now()){
 const ms=createdMs(l);
 if(!Number.isFinite(ms))return {days:null,bucket:'unknown',label:'Дата не указана',timestamp:null};
 if(ms>now+60000)return {days:null,bucket:'unknown',label:'Дата уточняется',timestamp:ms};
 const days=Math.max(0,Math.floor((now+YEREVAN_OFFSET_MS)/DAY_MS)-Math.floor((ms+YEREVAN_OFFSET_MS)/DAY_MS));
 return {days,bucket:String(Math.min(days,3)),label:days===0?'Сегодня':days===1?'1 д. назад':days===2?'2 д. назад':'3+ д. назад',timestamp:ms};
}
function newestFirst(a,b){
 const x=createdMs(a),y=createdMs(b);
 if(!Number.isFinite(x))return Number.isFinite(y)?1:0;
 if(!Number.isFinite(y))return -1;
 return y-x;
}
function normalizePhone(value) {
 const phone=String(value||'').replace(/[ ()\-\u00a0]/g,'');
 return /^\+[1-9][0-9]{7,14}$/.test(phone)?phone:'';
}
function phoneFromText(value) {
 const text=String(value||'').slice(0,12000),found=new Set();
 for(const m of text.matchAll(/(?<![\p{L}\p{N}_+])\+[0-9](?:[0-9 ()\-\u00a0]*[0-9])?/gu)) {
  let raw=m[0];const tail=text.slice(m.index+raw.length);
  if(/^[\p{L}\p{N}_]/u.test(tail)||/^\s*(?:доб\.?|ext\.?|#)\s*[0-9]/i.test(tail))continue;
  if((raw.match(/\(/g)||[]).length>(raw.match(/\)/g)||[]).length&&tail.startsWith(')'))raw+=')';
  const opens=(raw.match(/\(/g)||[]).length,closes=(raw.match(/\)/g)||[]).length;
  if(opens!==closes||opens>1||(opens&&raw.indexOf('(')>raw.indexOf(')')))continue;
  const number=normalizePhone(raw);if(number)found.add(number);
 }
 const number=found.size===1?[...found][0]:'';
 return /^(?:\+374[1-9][0-9]{7}|\+7[0-9]{10})$/.test(number)?number:'';
}
const API={phoneFromText,normalizePhone,offer,matches,norm,createdMs,publicationAge,newestFirst,DISTRICTS,CITIES};
if(typeof module!=='undefined')module.exports=API;else root.RentCore=API;
})(typeof window==='undefined'?globalThis:window);
