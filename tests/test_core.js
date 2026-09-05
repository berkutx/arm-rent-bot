const assert=require('node:assert/strict');const C=require('../web/core.js');
assert.equal(C.parse,undefined);
// Same label and date order across device timezones; no use of edited/confirmed times.
const now=Date.parse('2026-09-05T12:00:00+04:00');
const fixture=d=>({created_at:`2026-09-${String(d).padStart(2,'0')}T10:00:00+04:00`});
for (const [day,label] of [[5,'Сегодня'],[4,'1 д. назад'],[3,'2 д. назад'],[2,'3+ д. назад'],[1,'3+ д. назад']]) {
 assert.equal(C.publicationAge(fixture(day),now).label,label);
}
assert.equal(C.publicationAge({},now).label,'Дата не указана');
assert.equal(C.publicationAge({created_at:'invalid'},now).label,'Дата не указана');
assert.equal(C.publicationAge(fixture(7),now).label,'Дата уточняется');
assert.equal(C.publicationAge({...fixture(3),confirmed_at:fixture(5).created_at,edited_at:fixture(5).created_at},now).label,'2 д. назад');
assert.equal(C.createdMs({created_at:'2026-09-04T02:26:25'}),Date.parse('2026-09-03T22:26:25Z'));
const midnight=Date.parse('2026-09-05T00:01:00+04:00');
assert.equal(C.publicationAge({created_at:'2026-09-04T23:59:00+04:00'},midnight).label,'1 д. назад');
assert.deepEqual([fixture(2),{},fixture(5),fixture(4),fixture(3)].sort(C.newestFirst).map(l=>C.publicationAge(l,now).label),['Сегодня','1 д. назад','2 д. назад','3+ д. назад','Дата не указана']);
console.log('Publication-age and sorting assertions: passed');
const seed=require('../seed.json');const conditional=seed.find(l=>l.id==='demo-conditional');
assert(C.matches(conditional,{period:'month',currency:'AMD',max:'420000'}));
assert(!C.matches(conditional,{period:'month',currency:'AMD',max:'420000',residence_registration:true}));
assert(C.matches(conditional,{period:'month',currency:'AMD',max:'450000',residence_registration:true}));
assert.equal(C.offer(conditional,{period:'month',currency:'AMD',residence_registration:true}).amount,450000);
assert.equal(C.DISTRICTS.length,12);assert(C.DISTRICTS.some(d=>d.name==='Нубарашен'));
console.log('Districts, contacts and conditional-registration-price assertions: passed');


for (const city of ['Дилижан','Севан','Цахкадзор','Гюмри','Ванадзор','Абовян','Аштарак','Джермук']) {
  assert(C.CITIES.includes(city));
}
console.log('City catalog assertions: passed');
const phoneCases=JSON.parse(require('fs').readFileSync(require('path').join(__dirname,'phone_cases.json'),'utf8').replace(/^\uFEFF/,''));
for(const [text,expected] of phoneCases)assert.equal(C.phoneFromText(text),expected,text);
console.log('Conservative phone mask assertions: passed');

const paid = {...seed[0], status:'active', role:'agent', commission:80000, commission_type:'fixed'};
assert(C.matches(paid,{market:'paid'}));
assert(!C.matches(paid,{market:'free'}));
assert(!C.matches(paid,{}));
assert(!C.matches({...paid,role:'owner'},{market:'paid'}));
assert(!C.matches({...paid,commission:0},{market:'paid'}));
console.log('Commission market assertions: passed');
