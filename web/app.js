'use strict';
const $ = s => document.querySelector(s);
const C = window.RentCore;
const paths = {
  list:'M4 3h16v7H4zM4 14h16v7H4z', grid:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  sliders:'M4 7h7M15 7h5M4 17h2M10 17h10M11 4v6M6 14v6',
  info:'M12 17v-5M12 8h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  home:'M3 10l9-7 9 7v10H15v-7H9v7H3z',
  search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  plus:'M12 5v14M5 12h14',
  bell:'M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4',
  back:'M19 12H5M10 7l-5 5 5 5', close:'M6 6l12 12M18 6L6 18',
  photo:'M4 4h16v16H4zM4 16l5-5 4 4 3-3 4 4M16 8h.01',
  check:'M5 12l4 4L19 6', down:'M6 9l6 6 6-6', right:'M9 5l7 7-7 7',
  edit:'M16 3l5 5-12 12-6 1 1-6zM13 6l5 5',
  trash:'M4 6h16M8 6V3h8v3M6 6l1 15h10l1-15M10 10v7M14 10v7',
  eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  phone:'M5 3h4l2 5-3 2c2 3 3 4 6 6l2-3 5 2v4c0 2-2 3-4 2C9 19 5 15 3 7 2 5 3 3 5 3z',
  chat:'M21 11a9 9 0 0 1-9 9H3l2-5a9 9 0 1 1 16-4M8 10h8M8 14h5',
  author:'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0M4 21v-2a8 8 0 0 1 16 0v2',
  send:'M22 2L9 15M22 2l-7 20-6-7-7-6z',
};
const icon = n => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[n] || paths.home}"/></svg>`;
const esc = v => String(v ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const money = n => new Intl.NumberFormat('ru-RU').format(n);
const sym = c => ({AMD:'֏',USD:'$'})[c] || c;
const unit = p => p === 'month' ? 'мес.' : p === 'day' ? 'сутки' : 'период не указан';
const STORE = 'arm-rent-v7';
let savedLayout='list';try {savedLayout=localStorage.getItem(STORE+'-layout')==='grid'?'grid':'list';} catch {}
const baseFilters = () => ({q:'',period:'month',currency:'AMD',city:'Ереван',district:'',kind:'',rooms:'',max:'',market:'free',owner:false,pets:false,contract:false,residence_registration:false,verified:false});
let stored = {};
try { stored = JSON.parse(localStorage.getItem(STORE) || '{}'); } catch {}
let state = {
  screen:'feed', layout:savedLayout, postMarket:stored.postMarket==='paid'?'paid':'free', adminTab:'review', phoneInferred:false, filters:{...baseFilters(),...(stored.filters || {})}, sort:stored.sort || 'new',
  subs:[], own:[],
  text:'', photos:[], draft:null, formStep:0, phoneTouched:false, telegramPhotos:false,
   channelConfigured:false, paidChannelConfigured:false, related:[], user:null, bot:'', remote:[], queue:[], busy:false, error:'', booted:false,
};
let tg = null, sheetKind = '', sheetArg = '', restoreFocus = null, toastTimer;
let seenDraftPhotos=new Set();
let photoGallery=null, openingGallery=false, galleryBackPending=false, galleryAfter=null;
let startHandled = false;
const uid = () => window.crypto?.randomUUID?.().replaceAll('-','').slice(0,14) || Date.now().toString(36)+Math.random().toString(36).slice(2,8);
function persist() {
  try {localStorage.setItem(STORE,JSON.stringify({filters:state.filters,sort:state.sort,postMarket:state.postMarket}));} catch {}
}
function toast(text) {
  clearTimeout(toastTimer); $('#toast').textContent = text;
  toastTimer = setTimeout(() => { $('#toast').textContent = ''; }, 4000);
}
function all() {return [...new Map([...state.remote,...state.own].map(l=>[l.id,l])).values()];}
const item = id => [...all(),...state.own,...state.remote,...state.related,...state.queue].find(l => l.id === id);
function housing(l) {
  if (l.subtype) return l.subtype;
  if (l.kind === 'aparthotel') return 'Апарт-отель';
  if (l.kind === 'room') return 'Комната';
  if (l.kind === 'house') return l.rooms ? `Дом · ${l.rooms} комнат` : 'Дом';
  return l.rooms === 0 ? 'Студия' : l.rooms ? `${l.rooms} ${l.rooms === 1 ? 'комната' : l.rooms < 5 ? 'комнаты' : 'комнат'}` : 'Квартира';
}
function priceHTML(l, filters=state.filters) {
  const p = C.offer(l,filters) || l.prices?.[0];
  return p ? `${p.amount_max?'от ':''}${money(p.amount)} ${esc(sym(p.currency))} <small>/ ${unit(p.period)}</small>` : 'Укажите цену';
}
const shortDate = d => d ? new Date(d.slice(0,10)+'T12:00:00').toLocaleDateString('ru-RU',{day:'numeric',month:'short'}) : '';
const niceStatus = l => ({active:'Актуально · в ленте',review:'На модерации',rented:'Сдано',rejected:'Не опубликовано',banned:'Заблокировано'})[l.status] || l.status;
const displayStatus = l => l.status;
function exactDate(l) {
  const ms=C.createdMs(l);
  return Number.isFinite(ms)?new Date(ms).toLocaleString('ru-RU',{timeZone:'Asia/Yerevan',day:'numeric',month:'long',year:'numeric',hour:'2-digit',minute:'2-digit'})+' · Ереван':'';
}
function ageHTML(l) {
  const age=C.publicationAge(l,Date.now());
  return `<div class="publication-age" data-age="${age.bucket}"><time ${age.timestamp!==null?`datetime="${new Date(age.timestamp).toISOString()}"`:''} title="${esc(exactDate(l))}" aria-label="${esc(exactDate(l)?'Опубликовано '+exactDate(l):age.label)}">${esc(age.label)}</time></div>`;
}
function safePhoto(p) {
  const url = typeof p === 'string' ? p : p?.url;
  return typeof url === 'string' && (/^data:image\/(jpeg|png|webp);base64,/.test(url) || /^\/media\/[a-f0-9]{32}(?:-thumb)?\.jpg$/.test(url)) ? url : '';
}
function filtered() {
  const records = all().filter(l => displayStatus(l)==='active' && C.matches(l,state.filters));
  return records.sort((a,b) => state.sort === 'price' ? C.offer(a,state.filters).amount-C.offer(b,state.filters).amount || C.newestFirst(a,b) : C.newestFirst(a,b));
}
function filterKey(f) { return JSON.stringify(Object.keys(baseFilters()).map(k => [k, f[k] ?? baseFilters()[k]])); }
function currentSubscription() { return state.subs.find(s => filterKey(s.filters) === filterKey(state.filters)); }
function housingFilterLabel(f) { return f.kind === 'room' ? 'Комната' : f.kind === 'house' ? 'Дом' : f.rooms === '0' ? 'Студия' : f.rooms ? `${f.rooms} комн.` : 'Комнаты'; }
function filterSummary(f) {
  return [f.market==='paid'?'С комиссией':'Без комиссии',f.district || f.city || 'Все районы', housingFilterLabel(f)==='Комнаты' ? '' : housingFilterLabel(f),
    f.max ? `до ${money(f.max)} ${sym(f.currency)}` : sym(f.currency), f.period === 'day' ? 'за сутки' : 'за месяц'].filter(Boolean).join(' · ');
}
function header() {
  if (['add','review','admin','alerts'].includes(state.screen)) {
    const title={admin:'Модерация',alerts:'Уведомления',add:'Новое объявление',review:'Новое объявление'}[state.screen];
    return `<button class="icon-button" data-action="back" aria-label="Назад">${icon('back')}</button><div class="header-title">${title}</div>${state.screen==='add'&&state.own.length?`<button class="text-button" data-action="mine">Мои · ${state.own.length}</button>`:'<span class="header-side"></span>'}`;
  }
  return `<div class="brand"><div class="brand-icon">${icon('home')}</div><div><div class="brand-name">Аренда в Армении</div><div class="brand-caption">${esc(state.filters.city||'Армения')} · ${state.filters.market==='paid'?'агенты':'без комиссии'}</div></div></div><div class="row">${state.user?.is_admin?'<button class="text-button" data-action="open-admin">Админ</button>':''}<button class="icon-button" data-action="nav" data-id="alerts" aria-label="Мои уведомления">${icon('bell')}</button></div>`;
}
function dock() {
  if (state.screen === 'add') return `<div class="dock-action"><button class="button" id="continue" data-action="prepare-listing" ${state.busy||(state.formStep===0&&(!state.draft?.kind||(state.draft.kind==='apartment'&&state.draft.rooms===null)))?'disabled':''}>${state.busy?'Загружаем фото…':'Продолжить'}</button></div>`;
  if (state.screen === 'review') return `<div class="dock-action"><button class="button" data-action="publish" id="publish" ${state.busy?'disabled':''}>${state.busy?'Публикуем…':'Опубликовать'}</button></div>`;
  return `<nav class="nav" aria-label="Основные разделы"><button data-action="nav" data-id="feed" class="${state.screen==='feed'?'active':''}" ${state.screen==='feed'?'aria-current="page"':''}>${icon('search')}<span>Лента</span></button><button data-action="mine" class="mine-nav">${icon('author')}<span>Мои</span></button><button class="add-nav" data-action="nav" data-id="add">${icon('plus')}Сдать</button></nav>`;
}
function render() {
  $('#header').innerHTML = header();
  $('#main').innerHTML = state.screen==='feed' ? feed() : state.screen==='alerts' ? alerts() : state.screen==='review' ? review() : state.screen==='admin' ? admin() : compose();
  $('#dock').innerHTML = dock();
  updateBackButton();
  if($('#city-input'))updateAddressCity();
  if (state.error) $('#main').insertAdjacentHTML('afterbegin',`<div class="error-note">${esc(state.error)}</div>`);
}
function navigate(screen, push=true) {
  if(screen==='add'&&state.draft&&state.formStep===3)screen='review';
  clearSheet(false); state.screen=screen;
  if(screen==='add'&&!state.text.trim()&&!state.draft)state.postMarket=state.filters.market;
  if (push) history.pushState({rent:true,screen,step:state.formStep},'');
  persist(); render(); window.scrollTo(0,0);
}
function feed() {
  const ls=filtered(),f=state.filters,hasMarket=all().some(l=>C.matches(l,{market:f.market})),sub=currentSubscription(),budget=f.max?`До ${money(f.max)} ${sym(f.currency)}`:'Бюджет';
  return `${marketSwitch()}<div class="filters" aria-label="Фильтры поиска"><button class="filter-chip" data-action="budget"><span>${esc(budget)}</span>${icon('down')}</button><button class="filter-chip" data-action="district"><span>${esc(f.district||(f.city==='Ереван'?'Район':f.city||'Город'))}</span>${icon('down')}</button><button class="filter-chip" data-action="rooms"><span>${esc(housingFilterLabel(f))}</span>${icon('down')}</button><button class="filter-chip conditions-chip" data-action="conditions-filter" aria-label="Условия" title="Условия">${icon('sliders')}</button></div>
  <div class="results-heading"><button class="sort-button" data-action="sort">${ls.length} ${plural(ls.length,'вариант','варианта','вариантов')} · ${state.sort==='price'?'дешевле':'новые'} ${icon('down')}</button><div class="catalog-tools"><button class="follow-button ${sub?.active?'on':''}" data-action="follow" aria-label="${sub?.active?'Уведомления включены':'Уведомлять о новых вариантах'}" title="Уведомлять" aria-pressed="${!!sub?.active}">${icon(sub?.active?'check':'bell')}</button>${layoutSwitch()}</div></div>
  <div class="list ${state.layout==='grid'?'compact-grid':'album-feed'}">${ls.map(card).join('')||(hasMarket?empty('Нет подходящих вариантов','Попробуйте другой город или бюджет.','Сбросить фильтры','reset-filters'):empty('Пока нет объявлений',f.market==='paid'?'Добавьте предложение с комиссией или подпишитесь на поиск.':'Добавьте жильё или подпишитесь на поиск.','Сдать жильё','nav','add'))}</div>`;
}
function plural(n,a,b,c) { return n%10===1&&n%100!==11?a:n%10>=2&&n%10<=4&&(n%100<12||n%100>14)?b:c; }
function viewsHTML(l) {
  const known=Number.isInteger(l.view_count);
  const label=known?'Просмотры уникальных пользователей Telegram: '+l.view_count:'Просмотры пока недоступны';
  return `<span class="view-count" data-view-id="${esc(l.id)}" title="${esc(label)}" aria-label="${esc(label)}">${icon('eye')}${known?money(l.view_count):'—'}</span>`;
}
async function recordView(l) {
  if(!state.user)return;
  try {
    const result=await api('/api/listings/'+encodeURIComponent(l.id)+'/view','POST',{});
    for(const x of [...state.remote,...state.own,...state.related])if(x.id===l.id)x.view_count=result.view_count;
    l.view_count=result.view_count;
    document.querySelectorAll('[data-view-id]').forEach(el=>{if(el.dataset.viewId===l.id)el.outerHTML=viewsHTML(l);});
  } catch { /* A failed analytics request must not prevent reading the listing. */ }
}
function marketSwitch(post=false) {
  const market=post?state.postMarket:state.filters.market;
  return `<div class="market-switch" aria-label="${post?'Условия размещения':'Раздел каталога'}">${[['free','Без комиссии'],['paid','С комиссией · агенты']].map(([key,label])=>`<button data-action="${post?'post-market':'market'}" data-id="${key}" aria-pressed="${market===key}">${label}</button>`).join('')}</div>`;
}
function layoutSwitch() {
  return `<div class="layout-switch" aria-label="Вид каталога">${[['list','Лента'],['grid','Сетка']].map(([key,label])=>`<button data-action="layout" data-id="${key}" aria-label="${label}" title="${label}" aria-pressed="${state.layout===key}">${icon(key)}</button>`).join('')}</div>`;
}
function commissionLabel(l) {
  if(l.commission===0)return 'Без комиссии';
  if(!(l.commission>0))return 'Укажите комиссию';
  const fee=l.commission_type==='fixed'?`${money(l.commission)} ${sym(l.commission_currency||'AMD')}`:`${l.commission}% от аренды за ${l.commission_basis==='day'?'сутки':'месяц'}`;
  return 'Агенту '+fee+' · разово';
}
function commissionHTML(l) {return `${l.role==='agent'&&l.agent_affiliation?`<p class="agent-affiliation">${esc(l.agent_affiliation)}</p>`:''}<p class="commission-note ${l.commission>0?'paid-fee':''}">${esc(commissionLabel(l))}</p>`;}
function thumbPhoto(p) {return safePhoto(p?.thumb_url)||safePhoto(p);}
function albumHTML(l,compact=false) {
  const photos=(l.photos||[]).filter(p=>safePhoto(p)).slice(0,10);
  if(!photos.length)return `<button class="album-empty" data-action="detail" data-id="${esc(l.id)}">${icon('home')}<span>Условия и контакты</span></button>`;
  const visible=photos.slice(0,compact?1:3);
  return `<div class="photo-album photos-${visible.length}">${visible.map((p,i)=>`<button class="photo-tile" data-action="gallery" data-id="${esc(l.id)}" data-photo-index="${i}" aria-label="Фото ${i+1} из ${photos.length}: ${esc(l.address)}"><img src="${esc(thumbPhoto(p))}" data-full-src="${esc(safePhoto(p))}" alt="Фото жилья ${i+1}" loading="lazy" decoding="async"><span class="photo-missing">Фото не загрузилось</span>${i===0?`<span class="album-count">${photos.length} фото ${icon('photo')}</span>`:i===2&&photos.length>3?`<span class="album-count">+${photos.length-3}</span>`:''}</button>`).join('')}</div>`;
}
function card(l) {
  const hint=l.residence_registration==='yes'?'Регистрация возможна':l.residence_registration==='ask'?'Регистрация — по условиям':l.pets==='yes'?'Можно с питомцем':'';
  const verified=['owner_verified','representative_verified'].includes(l.document_status);
  return `<article class="listing" data-listing="${esc(l.id)}"><div class="listing-body">
    <button class="listing-main" data-action="detail" data-id="${esc(l.id)}"><div class="price">${priceHTML(l)}</div><div class="listing-title">${esc(housing(l))}${l.area?' · '+esc(l.area)+' м²':''}</div><div class="listing-address">${esc([l.city,l.address].filter(Boolean).join(' · '))}</div></button>
    ${commissionHTML(l)}${albumHTML(l,state.layout==='grid')}
    <div class="card-top"><div class="card-metrics">${ageHTML(l)}${viewsHTML(l)}</div>${verified?'<span class="trust-badge">Сверен по документам</span>':l.role==='owner'?'<span class="claim-badge">От собственника*</span>':l.role==='agent'?'<span class="claim-badge">Агент</span>':''}</div>
    <div class="card-extra">${travelHTML(l)}${availabilityHTML(l)}${hint?`<p class="availability-note">${esc(hint)}</p>`:''}</div>
    <div class="listing-bottom"><button class="listing-photo-link" data-action="detail" data-id="${esc(l.id)}">Условия ${icon('right')}</button><button class="contact-button" data-action="contact" data-id="${esc(l.id)}" >${icon('send')}Связаться</button></div>
  </div></article>`;
}
function empty(title,text,button,action='nav',id='feed') {
  return `<div class="empty">${icon('search')}<h3>${esc(title)}</h3><p>${esc(text)}</p>${button?`<button class="button secondary" data-action="${action}" data-id="${id}">${esc(button)}</button>`:''}</div>`;
}
function alerts() {
  return `<p class="intro">Новый вариант по вашим условиям — сразу в Telegram.</p>
    ${state.subs.length?`<div class="stack">${state.subs.map(s=>`<div class="subscription">${icon('bell')}<button class="grow" style="text-align:left;padding:0" data-action="open-search" data-id="${esc(s.id)}"><p>${esc(s.name)}</p><small>${s.active?('Новые совпадения сразу'):'На паузе'}</small></button><button class="switch" role="switch" aria-checked="${!!s.active}" aria-label="Уведомлять: ${esc(s.name)}" data-action="toggle-sub" data-id="${esc(s.id)}"></button><button class="icon-button" data-action="delete-sub" data-id="${esc(s.id)}" aria-label="Удалить поиск">${icon('trash')}</button></div>`).join('')}</div>`:empty('Уведомления о новых вариантах','Выберите условия в ленте и нажмите «Уведомлять».','К ленте')}`;
}
function formProgress() {
  return `<ol class="form-progress" aria-label="Шаги подачи">${['Жильё','Адрес','Цена','Детали'].map((label,i)=>`<li class="${i===state.formStep?'current':i<state.formStep?'done':''}" ${i===state.formStep?'aria-current="step"':''}><span>${i<state.formStep?icon('check'):i+1}</span>${label}</li>`).join('')}</ol>`;
}
function compose() {
  ensureDraft();const d=state.draft,p=d.prices[0];
  let body='';
  if(state.formStep===0)body=`<h1>Что сдаёте?</h1><div class="housing-choices">${[['apartment','Квартира'],['room','Комната'],['house','Дом'],['aparthotel','Апарт-отель']].map(([key,label])=>`<button class="choice ${d.kind===key?'active':''}" data-action="choose-kind" data-id="${key}" aria-pressed="${d.kind===key}">${label}</button>`).join('')}</div>${d.kind==='apartment'?`<fieldset class="room-options"><legend>Количество комнат</legend><div class="room-choices">${[0,1,2,3,4,5,6].map(n=>`<button class="choice ${d.rooms===n?'active':''}" data-action="choose-rooms" data-id="${n}" aria-pressed="${d.rooms===n}">${n===0?'Студия':n}</button>`).join('')}</div></fieldset>`:''}`;
  if(state.formStep===1)body=`<h1>Где находится жильё?</h1><form id="step-address-form" class="stack">${addressFields(d)}</form>`;
  if(state.formStep===2)body=`<h1>Сколько стоит аренда?</h1><form id="step-price-form" class="stack"><div><label for="budget-input">Стоимость аренды</label><input class="price-input" id="budget-input" name="amount" type="text" inputmode="numeric" pattern="[0-9 ]*" maxlength="11" value="${esc(p.amount||'')}" placeholder="300 000" required></div><div class="field-row"><div><label for="price-currency">Валюта</label><select id="price-currency" name="currency">${selectOptions([['AMD','Драмы · ֏'],['USD','Доллары · $']],p.currency)}</select></div><div><label for="price-period">За период</label><select id="price-period" name="period">${selectOptions([['month','Месяц'],['day','Сутки']],p.period)}</select></div></div></form>${marketSwitch(true)}${state.postMarket==='paid'?`<button class="commission-edit" data-action="edit-commission">${esc(commissionLabel(d))} ${icon('edit')}</button>`:`<div class="publisher-field"><label for="step-role">Кто размещает</label><select id="step-role" data-field="role">${roleOptions(d)}</select></div>`}${agentProfileButton(d)}`;
  return `<section class="submission">${formProgress()}${body}</section>`;
}
function roleOptions(d) {
  return selectOptions(state.postMarket==='paid'?[['agent','Агент / риелтор']]:[['unknown','Не указано'],['owner','Собственник'],['tenant','Съезжающий жилец'],['agent','Агент / риелтор']],d.role);
}
function agentProfileButton(d) {
  return d.role==='agent'?`<button class="contact-edit" data-action="agent-profile">${icon('author')}<span>${state.user?.agent_profile_ready?esc(state.user.agent_affiliation):'Заполнить профиль агента'}</span>${icon('edit')}</button>`:'';
}

function photoPreviews() {
  return state.photos.length?`<div class="photos-row">${state.photos.map((p,i)=>`<div class="photo-preview"><img src="${esc(thumbPhoto(p))}" alt="Фото ${i+1}"><button data-action="remove-photo" data-id="${i}" aria-label="Удалить фото ${i+1}">${icon('close')}</button></div>`).join('')}</div>`:'';
}
function review() {
  ensureDraft();const d=state.draft;
  return `<section class="submission">${formProgress()}<h1>Фото и описание</h1><div class="submission-summary">
    <button data-action="edit-rooms">${esc(housing(d))} ${icon('edit')}</button>
    <button data-action="edit-address">${esc([d.city,d.address].filter(Boolean).join(' · '))} ${icon('edit')}</button>
    <button data-action="edit-price">${priceHTML(d,{})} ${icon('edit')}</button>
    <button data-action="edit-commission">${esc(commissionLabel(d))} ${icon('edit')}</button>
    </div><button class="upload-button" data-action="upload" ${state.busy?'disabled':''}>${icon('photo')}<div><strong>${state.photos.length?'Добавить ещё фото':'Добавить фотографии'}</strong><span>В чате бота · до 10 фото</span></div></button>
    <div id="photo-previews">${photoPreviews()}</div>
    <div class="description-field"><label for="listing-text">Что ещё стоит знать? <small>необязательно</small></label><textarea id="listing-text" placeholder="Мебель, техника, коммунальные платежи, особенности жилья…" maxlength="12000">${esc(state.text)}</textarea></div>
    <details class="rental-dates"><summary>Даты аренды <small>необязательно</small>${icon('down')}</summary>${availabilityFields(d)}</details>
    <details class="optional-block"><summary>Условия и пожелания <small>необязательно</small>${icon('down')}</summary><div class="stack optional-inner">${optionalFields(d)}</div></details>
    <button class="contact-edit" data-action="edit-contact">${icon('send')} Связь: ${esc(state.user?.username?'@'+state.user.username:'в обсуждении')} ${icon('edit')}</button>
    <p class="note phone-inferred" ${state.phoneInferred&&d.phone?'':'hidden'}>Телефон распознан из текста. Проверьте номер в контактах.</p>${agentProfileButton(d)}
    <p class="note">Публикуя, подтверждаете: жильё доступно, размещение согласовано, цена и комиссия указаны верно.</p></section>`;
}

let proofURLs=[];
function clearProofURLs(){for(const url of proofURLs)URL.revokeObjectURL(url);proofURLs=[];}
function showSheet(title, body, footer='', kind='', arg='', className='', push=true) {
  clearProofURLs();
  const replacing=!!sheetKind;
  if (!sheetKind) restoreFocus=document.activeElement;
  sheetKind=kind||'generic'; sheetArg=arg;
  $('#modal-root').innerHTML=`<div class="overlay" data-action="backdrop"><section class="sheet ${className}" role="dialog" aria-modal="true" aria-label="${esc(title)}" tabindex="-1"><header class="sheet-head"><h2>${esc(title)}</h2><button class="icon-button" data-action="close" aria-label="Закрыть">${icon('close')}</button></header><div class="sheet-body">${body}</div><footer class="sheet-footer">${footer}</footer></section></div>`;
  $('#app').inert=true; document.body.style.overflow='hidden';
  $('.sheet').focus({preventScroll:true}); updateBackButton();
  if (push && !replacing) history.pushState({...history.state,rent:true,screen:state.screen,step:state.formStep,sheet:true},'');
}
function clearSheet(focus=true) {
  clearProofURLs();
  $('#modal-root').innerHTML=''; sheetKind=''; sheetArg='';
  $('#app').inert=false; document.body.style.overflow=''; updateBackButton();
  if (focus && restoreFocus?.isConnected) restoreFocus.focus({preventScroll:true});
}
function closeSheet() {
  clearSheet();
  if (history.state?.sheet) history.back();
}
function back() {
  if(photoGallery){closeGallery();return;}
  if (sheetKind) return closeSheet();
  if(['add','review'].includes(state.screen)&&state.formStep>0&&!history.state?.flowPrev)return goStep(state.formStep-1,false);
  if (history.state?.rent && state.screen!=='feed' && history.length>1) { history.back(); return; }
  if (state.screen==='review') return navigate('add',false);
  navigate('feed',false);
}
window.addEventListener('popstate', e=> {
  if(galleryBackPending){galleryBackPending=false;finishGallery();return;}
  if(photoGallery){closeGallery();return;}
  const hadSheet=!!sheetKind;
  clearSheet();
  if (!hadSheet) { state.screen=e.state?.screen||'feed'; state.formStep=e.state?.step||0;if(['add','review'].includes(state.screen)&&!state.draft){state.screen='feed';state.formStep=0;history.replaceState({rent:true,screen:'feed'},'');}render(); }
});
function selectOptions(options,current) {
  return options.map(([v,t])=>`<option value="${esc(v)}" ${v===current?'selected':''}>${esc(t)}</option>`).join('');
}
function budgetSheet(edit=false) {
  const p=edit?state.draft.prices[0]:{amount:state.filters.max,currency:state.filters.currency,period:state.filters.period};
  const presets=p.currency==='USD'?[600,900,1200]:p.period==='day'?[15000,25000,35000]:[250000,350000,450000];
  const body=`<form id="${edit?'edit-price-form':'budget-form'}" class="stack"><div class="field-row"><div><label for="price-period">Цена за</label><select name="period" id="price-period">${selectOptions([['month','Месяц'],['day','Сутки']],p.period)}</select></div><div><label for="price-currency">Валюта</label><select name="currency" id="price-currency">${selectOptions([['AMD','Драмы · ֏'],['USD','Доллары · $']],p.currency)}</select></div></div><div><label for="budget-input">${edit?'Стоимость':'Максимальная цена'}</label><input class="price-input" id="budget-input" name="amount" type="text" inputmode="numeric" pattern="[0-9 ]*" maxlength="11" placeholder="${edit?'Например, 300 000':'Без ограничения'}" value="${esc(p.amount||'')}" ${edit?'required':''}></div>${edit?'':`<div class="choices" id="budget-presets">${presets.map(n=>`<button class="choice" type="button" data-action="preset" data-id="${n}">${money(n)}</button>`).join('')}<button class="choice" type="button" data-action="preset" data-id="">Любая</button></div>`}</form>`;
  showSheet(edit?'Цена аренды':'Бюджет',body,`<button class="button" form="${edit?'edit-price-form':'budget-form'}" type="submit">${edit?'Готово':'Показать варианты'}</button>`,edit?'edit-price':'budget');
}
function districtSheet() {
  const current=state.filters.district,city=state.filters.city;
  const row=(value,label,count)=>`<button class="choice-row district-choice ${!value?'all-districts':''} ${current===value?'active':''}" data-action="select-district" data-id="${esc(value)}"><span class="district-name">${esc(label)}</span><span class="district-count">${count}${current===value?icon('check'):''}</span></button>`;
  const cities=[...new Set([...C.CITIES,...all().map(l=>l.city)])].filter(Boolean);
  const records=all().filter(l=>C.matches(l,{...state.filters,city:'Ереван',district:''}));
  showSheet('Город и район',`<div class="stack"><div><label for="filter-city">Город</label><select id="filter-city">${selectOptions([['','Все города'],...cities.map(c=>[c,c])],city)}</select></div>${city==='Ереван'?`<div class="district-choices">${row('','Все районы Еревана',records.length)}${C.DISTRICTS.map(d=>row(d.name,d.name,records.filter(l=>l.district===d.name).length)).join('')}</div>`:''}</div>`,'','district');
}
const roomChoices=[['','Любое жильё'],['room','Комната'],['0','Студия'],['1','1 комната'],['2','2 комнаты'],['3','3 комнаты'],['4+','4 и больше'],['house','Дом'],['aparthotel','Апарт-отель']];
function roomsSheet(edit=false) {
  const d=edit?state.draft:state.filters, v=['room','house','aparthotel'].includes(d.kind)?d.kind:String(d.rooms??'');
  const choices=edit?roomChoices.filter(([x])=>!['','4+'].includes(x)).concat([['4','4 комнаты'],['5','5 комнат'],['6','6 комнат']]):roomChoices;
  showSheet(edit?'Какое жильё сдаёте?':'Комнаты',`<div class="choice-list">${choices.map(([k,t])=>`<button class="choice-row ${k===v?'active':''}" data-action="${edit?'set-rooms':'select-rooms'}" data-id="${esc(k)}">${esc(t)}${k===v?icon('check'):''}</button>`).join('')}</div>`,'',edit?'edit-rooms':'rooms');
}
function publicationLinksHTML(l) {
  const row=(action,label,note,available)=>`<button class="publication-link" data-action="${action}" data-id="${esc(l.id)}" ${available?'':'disabled'}><span><strong>${label}</strong>${available&&note?`<small>${note}</small>`:''}</span>${icon('right')}</button>`;
  return `<div class="publication-links"><h3>Объявление и автор</h3>${row('listing-post','Пост в канале','Открыть публикацию',!!l.telegram_post_url)}${row('author-listings','Другие объявления автора','Активные предложения',l.author_listings_available)}${l.role!=='agent'?row('phone-listings','Объявления с этим телефоном','Совпадение номера, без риелторов',l.phone_listings_available):''}${row('discussion','Комментарии и ответы','Открыть обсуждение',!!l.telegram_discussion_url)}</div>`;
}
async function authorListings(id,button) {
  const l=item(id);if(!l)return;
  const origin=$('.sheet');button.disabled=true;button.setAttribute('aria-busy','true');
  try {
    const result=await api('/api/listings/'+encodeURIComponent(id)+'/author-listings');
    if(!origin?.isConnected)return;
    if(!result.available)return toast('Автор этого объявления пока не привязан.');
    state.related=result.listings;
    const rows=result.listings.map(x=>`<button class="author-listing-row" data-action="detail" data-id="${esc(x.id)}"><span><strong>${priceHTML(x,{})}</strong><span>${esc(housing(x))} · ${esc(x.city)}</span><small>${esc(x.address)}</small></span>${icon('right')}</button>`).join('');
    showSheet('Другие объявления автора',`<p class="note">Другие активные предложения этого автора.</p>${rows?`<div class="author-listings">${rows}</div>`:'<div class="author-empty"><h3>Других объявлений нет</h3></div>'}`,`<button class="button secondary" data-action="detail" data-id="${esc(id)}">К объявлению</button>`,'author-listings',id);
  } finally { if(button.isConnected){button.disabled=false;button.removeAttribute('aria-busy');} }
}
async function phoneListings(id,button) {
  const l=item(id);if(!l||l.role==='agent')return;
  const origin=$('.sheet');button.disabled=true;
  try {
    const result=await api('/api/listings/'+encodeURIComponent(id)+'/phone-listings');
    if(!origin?.isConnected)return;
    if(!result.available)return toast('Поиск по этому номеру недоступен.');
    state.related=result.listings;
    const rows=result.listings.map(x=>`<button class="author-listing-row" data-action="detail" data-id="${esc(x.id)}"><span><strong>${priceHTML(x,{})}</strong><span>${esc(housing(x))} · ${esc(x.city)}</span><small>${esc(x.address)}</small></span>${icon('right')}</button>`).join('');
    showSheet('С этим телефоном',`<p class="note">Активные объявления с этим телефоном, без риелторов. Личность и право собственности проверяются отдельно.</p>${rows?`<div class="phone-listings">${rows}</div>`:'<div class="author-empty"><h3>Других объявлений не найдено</h3></div>'}`,`<button class="button secondary" data-action="detail" data-id="${esc(id)}">К объявлению</button>`,'phone-listings',id);
  } finally {if(button.isConnected)button.disabled=false;}
}
async function detail(id,cached=false) {
  if(!cached || !state.user?.is_admin) {
    try { const latest=await api('/api/listings/'+encodeURIComponent(id)); state.remote=state.remote.filter(x=>x.id!==id); state.remote.push(latest); }
    catch(e) { return toast(e.message); }
  }
  const l=item(id); if (!l) return toast('Объявление больше недоступно.');
  const media=l.photos?.length?albumHTML(l):'';
  const description=l.description||'';
  showSheet(housing(l),`${media}<div class="card-metrics">${ageHTML(l)}${viewsHTML(l)}</div><div class="price">${priceHTML(l)}</div><p class="meta">${esc([l.city,l.address,l.district].filter(Boolean).join(' · '))}</p>${l.area||l.floor?`<p class="meta">${[l.area?esc(l.area)+' м²':'',l.floor?'Этаж '+esc(l.floor):''].filter(Boolean).join(' · ')}</p>`:''}${commissionHTML(l)}${travelHTML(l)}${conditionsHTML(l)}${publicationLinksHTML(l)}${trustHTML(l)}${description?`<details open><summary>Описание${icon('down')}</summary><div class="detail-description details-text">${esc(description)}</div></details>`:''}${displayStatus(l)!=='active'?`<div class="error-note">${esc(niceStatus(l))}. Объявление скрыто из ленты.</div>`:''}${exactDate(l)?`<p class="note">Опубликовано ${esc(exactDate(l))}</p>`:''}${adminAgentButton(l)}${state.user?.is_admin?`<button class="button secondary danger" data-action="ban" data-id="${esc(l.id)}">Заблокировать с причиной</button>`:''}<button class="text-button" data-action="report" data-id="${esc(l.id)}">Пожаловаться</button>`,
    `<button class="button" data-action="contact" data-id="${esc(l.id)}" ${displayStatus(l)!=='active'?'disabled':''}>${icon('send')}Связаться</button>`,'detail',id,'detail-sheet');
  void recordView(l);
}
function mineSheet() {
  showSheet('Мои объявления',state.own.map(l=>`<article class="my-row" data-mine-id="${esc(l.id)}"><h3>${esc(l.city)} · ${esc(l.address)}</h3><div class="card-metrics">${ageHTML(l)}${viewsHTML(l)}</div><p>${priceHTML(l,{})}</p><p class="listing-status status-${esc(l.status)}">${esc(niceStatus(l))}</p>${l.ban_reason||l.review_reason?`<div class="moderation-reason"><strong>${l.status==='banned'?'Причина блокировки':'Комментарий модератора'}</strong><p>${esc(l.ban_reason||l.review_reason)}</p></div>`:''}${['active','rented'].includes(l.status)?`<button class="text-button" data-action="detail" data-id="${esc(l.id)}">Открыть карточку</button><button class="button secondary" data-action="status" data-id="${esc(l.id)}" data-status="${l.status==='active'?'rented':'active'}">${l.status==='active'?'Отметить «Сдано»':'Снова актуально'}</button>`:''}${['active','review'].includes(l.status)?`<button class="text-button" data-action="verify" data-id="${esc(l.id)}">${l.document_status==='pending'?'Документ на проверке':'Подтвердить собственность'}</button>`:''}</article>`).join('')||'<p class="note">Пока нет объявлений. Нажмите «Сдать», чтобы добавить жильё.</p>','','mine');
}
async function refreshAdmin() {
  state.queue=await api(state.adminTab==='reports'?'/api/admin/reports':state.adminTab==='review'?'/api/admin/queue':'/api/admin/listings?status='+state.adminTab);
}
function admin() {
  if (!state.user?.is_admin) return empty('Доступ только администратору','','К ленте');
  return `<h1>Объявления</h1><div class="admin-tabs">${[['review','Проверка'],['reports','Жалобы'],['all','Все'],['banned','Баны']].map(([k,t])=>`<button class="choice ${state.adminTab===k?'active':''}" data-action="admin-tab" data-id="${k}" aria-pressed="${state.adminTab===k}">${t}</button>`).join('')}</div><div class="stack">${state.adminTab==='reports'?reportsHTML():state.queue.map(l=>`<article class="my-row" data-admin-id="${esc(l.id)}"><h3>${esc(l.city)} · ${esc(l.address)}</h3><p>${priceHTML(l,{})}</p><p class="listing-status status-${esc(l.status)}">${esc(niceStatus(l))}</p>${l.ban_reason||l.review_reason?`<div class="moderation-reason">${esc(l.ban_reason||l.review_reason)}</div>`:''}${l.description?`<details><summary>Описание ${icon('down')}</summary><p class="details-text">${esc(l.description)}</p></details>`:''}${l.document_status==='pending'?`<button class="button secondary" data-action="review-doc" data-id="${esc(l.id)}">Проверить документ</button>`:''}${adminAgentButton(l)}<div class="moderation-actions">${l.status==='banned'?`<button class="button secondary" data-action="unban" data-id="${esc(l.id)}">Снять блокировку</button>`:`${['review','rejected'].includes(l.status)?`<button class="button" data-action="approve" data-id="${esc(l.id)}">Одобрить</button>`:''}<button class="button secondary danger" data-action="ban" data-id="${esc(l.id)}">Заблокировать с причиной</button>`}</div></article>`).join('')||'<p class="note">Объявлений в этом разделе нет.</p>'}</div>`;
}
function reportStatus(r){return r.status==='draft'?'Черновик':r.status==='pending'?'Ожидает решения':r.outcome==='ban'?'Объявление заблокировано':'Жалоба отклонена';}
function reportsHTML(){return state.queue.map(r=>`<button class="my-row report-row" data-action="open-report" data-id="${esc(r.id)}"><strong>${esc(r.listing.address)}</strong><span>${esc(r.reason||'Причина не указана')}</span><small>${esc(r.reporter.name)} · ${esc(r.reporter.username?'@'+r.reporter.username:'ID '+r.reporter.id)}</small><span>${esc(reportStatus(r))}</span></button>`).join('')||'<p class="note">Жалоб пока нет.</p>';}
function proofHTML(r,editable=false){return `<div class="report-proofs">${r.photos.map((p,i)=>`<div><img data-proof-src="${esc(p.url)}" alt="Доказательство ${i+1}">${editable?`<button type="button" class="text-button" data-action="remove-proof" data-id="${esc(p.id)}">Удалить фото ${i+1}</button>`:''}</div>`).join('')}</div>`;}
async function loadProofs(){
  for(const img of document.querySelectorAll('[data-proof-src]')){
    try{
      const response=await fetch(img.dataset.proofSrc,{headers:{'X-Telegram-Init-Data':tg?.initData||''},cache:'no-store'});
      if(!response.ok)throw Error();
      const blob=await response.blob();if(!img.isConnected)continue;
      const url=URL.createObjectURL(blob);proofURLs.push(url);img.src=url;
    }catch{if(img.isConnected)img.alt='Фото не загрузилось. Обновите доказательства.';}
  }
}
async function reportSheet(id,isReport=false){
  if(!requireUser())return;
  const r=await api(isReport?'/api/reports/'+id:'/api/listings/'+id+'/report-draft',isReport?'GET':'POST',isReport?undefined:{});
  state.report=r;
  if(r.status!=='draft')return reportReview(r);
  showSheet('Жалоба на объявление',`<form id="report-form" data-listing="${esc(r.listing.id)}" class="stack"><p>${esc(r.listing.address)}</p><div><label for="report-reason">Причина</label><input id="report-reason" name="reason" minlength="3" maxlength="200" required placeholder="Например: скрытая комиссия" value="${esc(r.reason)}"></div><div><label for="report-details">Что произошло</label><textarea id="report-details" name="details" minlength="10" maxlength="2000" required placeholder="Что в объявлении не соответствует действительности?">${esc(r.details)}</textarea></div><div><label for="report-evidence">Доказательства <small>если есть</small></label><textarea id="report-evidence" name="evidence" maxlength="2000" placeholder="Ссылки на сообщения, публикации или другие подтверждения">${esc(r.evidence)}</textarea></div><button class="button secondary" type="button" data-action="report-photos">Добавить скриншоты через Telegram</button><button class="text-button" type="button" data-action="refresh-proofs">Обновить фото · ${r.photos.length}/5</button>${proofHTML(r,true)}<p class="note">Жалобу, ваш Telegram и доказательства увидят только администраторы.</p></form>`,`<button class="button" type="submit" form="report-form">Отправить жалобу</button>`,'report',r.id);
  void loadProofs();
}
async function saveReportForm(){
  const form=$('#report-form');if(!form)return;
  const x=Object.fromEntries(new FormData(form));
  state.report=await api('/api/reports/'+state.report.id,'PUT',x);
}
function evidenceHTML(text){
  return text.split(/(https?:\/\/[^\s<>]+)/g).map(part=>/^https?:\/\//.test(part)?`<a href="${esc(part)}" target="_blank" rel="noopener noreferrer">${esc(part)}</a>`:esc(part)).join('');
}
function reportReview(r){
  const isAdmin=state.user?.is_admin,p=r.reporter,l=r.listing;
  state.remote=state.remote.filter(x=>x.id!==l.id);state.remote.push(l);
  const who=`${esc(p.name)}${p.username?` · <a href="https://t.me/${encodeURIComponent(p.username)}" target="_blank" rel="noopener noreferrer">@${esc(p.username)}</a>`:''} · ID ${esc(p.id)}`;
  const form=isAdmin&&r.status==='pending'?`<form id="report-decision" class="stack"><div><label for="report-outcome">Решение</label><select id="report-outcome" name="outcome" required><option value="">Выберите</option><option value="dismiss">Отклонить жалобу</option><option value="ban">Заблокировать объявление</option></select></div><div><label for="report-resolution">Причина решения</label><textarea id="report-resolution" name="reason" required minlength="3" maxlength="500"></textarea></div><p class="note">При блокировке автор увидит причину решения.</p></form>`:'';
  showSheet('Жалоба',`<div class="stack report-content"><strong>${esc(l.city)} · ${esc(l.address)}</strong><p class="note">${esc(reportStatus(r))} · ${esc(new Date(r.created_at).toLocaleString('ru-RU'))}</p><p>От: ${who}</p><h3>${esc(r.reason||'Причина не указана')}</h3>${r.details?`<p class="details-text">${esc(r.details)}</p>`:''}${r.evidence?`<div class="details-text">${evidenceHTML(r.evidence)}</div>`:''}${proofHTML(r)}${isAdmin?`<button class="button secondary" data-action="report-listing" data-id="${esc(l.id)}">Открыть объявление</button>`:''}${r.resolution?`<p class="moderation-reason">${esc(r.resolution)}</p>`:''}${form}</div>`,form?'<button class="button" type="submit" form="report-decision">Сохранить решение</button>':'','report-review',r.id);
  void loadProofs();
}
async function submitReport(form,x){
  const button=$(`button[form="${form.id}"]`);button.disabled=true;
  try{
    if(form.id==='report-form'){
      await api('/api/listings/'+form.dataset.listing+'/report','POST',{...x,report_id:state.report.id});
      await reportSheet(state.report.id,true);toast('Жалоба отправлена администратору.');
    }else{
      await api('/api/admin/reports/'+state.report.id+'/resolve','POST',x);
      closeSheet();await refresh();state.adminTab='reports';await refreshAdmin();navigate('admin');toast('Решение сохранено.');
    }
  }finally{if(button.isConnected)button.disabled=false;}
}
function banSheet(id) {
  if(!state.user?.is_admin)return;
  const l=item(id);if(!l)return;
  showSheet('Блокировка объявления',`<form id="ban-form" data-listing="${esc(id)}" class="stack"><p>${esc(l.city)} · ${esc(l.address)}</p><div><label for="ban-reason">Причина — её увидит автор</label><textarea id="ban-reason" name="reason" minlength="3" maxlength="500" required placeholder="Например: в объявлении указана комиссия"></textarea></div><p class="note">Объявление исчезнет из ленты. Автор увидит его в «Моих» со статусом и причиной блокировки.</p></form>`,`<button class="button" type="submit" form="ban-form">Заблокировать</button>`,'ban',id);
}
async function api(path,method='GET',body) {
  const headers={}; if (tg?.initData) headers['X-Telegram-Init-Data']=tg.initData;
  if (body && !(body instanceof FormData)) headers['Content-Type']='application/json';
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),20000);
  try {
    const r=await fetch(path,{method,headers,body:body?(body instanceof FormData?body:JSON.stringify(body)):undefined,signal:controller.signal});
    const data=await r.json(); if (!r.ok) throw Error(typeof data.detail==='string'?data.detail:'Проверьте данные и повторите.');
    return data;
  } catch(e) { if(e.name==='AbortError') throw Error('Нет ответа сервера. Попробуйте ещё раз.'); throw e; }
  finally { clearTimeout(timer); }
}
function requireUser() {
  if (state.user) return true;
  toast('Откройте приложение через своего Telegram-бота.'); return false;
}
async function refresh() {
  const [ls, own, subscriptions]=await Promise.all([
    api('/api/listings'), state.user?api('/api/mine'):Promise.resolve([]),
    state.user?api('/api/subscriptions'):Promise.resolve([])
  ]);
  state.remote=ls; state.own=own; state.subs=subscriptions;
}

async function follow() {
  if (!requireUser()) return;
  const existing=currentSubscription();
  if (existing) { await toggleSub(existing.id); render(); return; }
  if (tg?.requestWriteAccess && !tg.initDataUnsafe?.user?.allows_write_to_pm) {
    const allowed=await new Promise(resolve=>tg.requestWriteAccess(resolve));
    if (!allowed) return toast('Без разрешения бот не сможет присылать объявления.');
  }
  const s={id:'q'+uid(),name:filterSummary(state.filters),frequency:'instant',active:true,filters:{...state.filters}};
  s.id=(await api('/api/subscriptions','POST',s)).id;
  state.subs.push(s); persist(); render();
  toast('Будем присылать новые совпадения.');
}
async function toggleSub(id) {
  const s=state.subs.find(s=>s.id===id); if (!s) return;
  const active=!s.active;
  await api('/api/subscriptions/'+id,'PATCH',{active});
  s.active=active; persist(); render();
}
function ensureDraft() {
  if(state.draft)return;
  state.formStep=0;state.phoneTouched=false;
  state.draft={address:'',city:state.filters.city||'Ереван',district:'',kind:'',rooms:null,
    prices:[{amount:0,currency:'AMD',period:'month'}],available:null,available_until:null,
    commission:state.postMarket==='paid'?null:0,commission_type:'percent',commission_currency:'AMD',commission_basis:'month',contact:'',phone:C.phoneFromText(state.text),role:state.postMarket==='paid'?'agent':'unknown',pets:'unknown',deposit:null,
    contract:'unknown',residence_registration:'unknown',lease_registration:'unknown',wishes:''};
  state.phoneInferred=!!state.draft.phone;
}
function captureStep() {
  const d=state.draft;if(!d)return;
  const address=$('#step-address-form');
  if(address){const x=Object.fromEntries(new FormData(address)),city=(x.city==='other'?x.custom_city:x.city).trim();Object.assign(d,{address:x.address,city,district:city==='Ереван'?x.district:'',metro_walk_minutes:city==='Ереван'&&x.metro_walk_minutes?Number(x.metro_walk_minutes):null,center_drive_minutes:x.center_drive_minutes?Number(x.center_drive_minutes):null});}
  const price=$('#step-price-form');
  if(price){const x=Object.fromEntries(new FormData(price));d.prices[0]={amount:Number(x.amount.replace(/\s/g,'')),currency:x.currency,period:x.period};}
}
function goStep(step,push=true) {
  captureStep();const fromForm=['add','review'].includes(state.screen);
  state.formStep=step;navigate(step===3?'review':'add',false);
  history[push?'pushState':'replaceState']({rent:true,screen:state.screen,step,flowPrev:push&&fromForm},'');
}
async function prepareListing() {
  if(state.busy)return;
  ensureDraft();captureStep();const d=state.draft;
  if(state.formStep===0&&(!d.kind||(d.kind==='apartment'&&d.rooms===null)))return;
  if(state.formStep===1){
    if(!$('#step-address-form').reportValidity())return;
    if(d.kind!=='house'&&!/\d/.test(d.address))return toast('Добавьте номер дома. Номер квартиры не нужен.');
  }
  if(state.formStep===2){
    if(!$('#step-price-form').reportValidity())return;
    if(!Number.isFinite(d.prices[0].amount)||d.prices[0].amount<=0||d.prices[0].amount>1e9)return toast('Укажите корректную цену.');
    if(state.postMarket==='paid'&&!(d.commission>0))return commissionSheet();
    if(d.role==='agent'&&!state.user?.agent_profile_ready)return agentProfileSheet();
  }
  goStep(state.formStep+1);
}

async function publish() {
  if (state.busy || !requireUser()) return;
  const d=state.draft; if (!d) return;

  if(d.role==='agent'&&!state.user?.agent_profile_ready){await agentProfileSheet();return;}
  if(state.postMarket==='paid'&&(!(d.commission>0)||d.role!=='agent')){commissionSheet();return;}
  if (!d.prices[0].amount) { budgetSheet(true); return; }
  if (!d.address || d.address.trim().length<3) { editAddress(); return; }
  if(d.kind!=='house'&&!/\d/.test(d.address)){editAddress();toast('Добавьте номер дома. Номер квартиры не нужен.');return;}
  if(!d.kind||(d.kind==='apartment'&&d.rooms==null)){roomsSheet(true);return;}
  const dateError=availabilityError(d);
  if(dateError){toast(dateError);$('.rental-dates').open=true;$('#available-until').focus();return;}
  state.busy=true; $('#dock').innerHTML=dock();
  try {
    const payload={...d,description:state.text,photos:state.photos.map(p=>({id:p.id})),contact:d.contact||(state.user?.username?'@'+state.user.username:''),phone:d.phone||'',photo_count:state.photos.length};
    const l=await api('/api/listings','POST',{listing:payload,private:{},consent:true});await refresh();
    state.text=''; state.photos=[]; state.draft=null; state.formStep=0;state.phoneTouched=false; state.busy=false;state.telegramPhotos=false;seenDraftPhotos.clear();
    try {localStorage.removeItem(STORE+'-draft-'+state.user.id);} catch {}
    state.filters={...baseFilters(),currency:l.prices[0].currency,period:l.prices[0].period,city:l.city,market:l.commission>0?'paid':'free'};
    persist(); navigate('feed');
    showSheet(l.status==='review'?'Нужна проверка':'Объявление добавлено',`<div class="stack"><p>${esc(l.address)}</p><p class="price">${priceHTML(l)}</p><p class="muted">${l.status==='review'?'Нашлось похожее объявление. '+'Админ получит задачу.':'Объявление добавлено.'}</p></div>`,`<div class="stack"><button class="button secondary" data-action="verify" data-id="${esc(l.id)}">Подтвердить собственность</button><button class="button" data-action="close">Готово</button></div>`,'success');
  } finally { state.busy=false; $('#dock').innerHTML=dock(); }
}
function editAddress() {
  showSheet('Адрес',`<form id="address-form" class="stack">${addressFields(state.draft)}</form>`,`<button class="button" type="submit" form="address-form">Готово</button>`,'edit-address');
  updateAddressCity();
}
function addressFields(d) {
  const known=C.CITIES.includes(d.city||'Ереван');
  return `<div><label for="city-input">Город</label><select id="city-input" name="city">${selectOptions([...C.CITIES.map(c=>[c,c]),['other','Другой город / населённый пункт']],known?d.city||'Ереван':'other')}</select></div><div id="custom-city-row" ${known?'hidden':''}><label for="custom-city">Название населённого пункта</label><input id="custom-city" name="custom_city" value="${known?'':esc(d.city)}" maxlength="80" ${known?'':'required'}></div><div><label for="address-input">Улица и номер дома</label><input id="address-input" name="address" value="${esc(d.address)}" autocomplete="street-address" maxlength="180" minlength="3" required></div><details class="travel-fields"><summary>Район и время в пути <small>необязательно</small>${icon('down')}</summary><div class="stack optional-inner"><div id="district-row"><label for="district-input">Район Еревана</label><select id="district-input" name="district">${selectOptions([['','Уточняется'],...C.DISTRICTS.map(x=>[x.name,x.name])],d.district||'')}</select></div><div id="metro-row"><label for="metro-minutes">До метро пешком, мин</label><input id="metro-minutes" name="metro_walk_minutes" type="number" inputmode="numeric" min="1" max="180" step="1" placeholder="Например, 10" value="${esc(d.metro_walk_minutes??'')}"></div><div><label for="center-minutes">До центра этого города на машине, мин</label><input id="center-minutes" name="center_drive_minutes" type="number" inputmode="numeric" min="1" max="360" step="1" placeholder="Например, 15" value="${esc(d.center_drive_minutes??'')}"></div><p class="note">По вашей оценке, с учётом обычного трафика.</p></div></details><p class="note">Укажите улицу и номер дома, без квартиры. Для частного дома номер необязателен.</p>`;
}
function updateAddressCity(clear=false) {
  const city=$('#city-input').value, yerevan=city==='Ереван', other=city==='other';
  if(clear){$('#district-input').value='';$('#metro-minutes').value='';$('#center-minutes').value='';}
  $('#custom-city-row').hidden=!other; $('#custom-city').required=other;
  $('#district-row').hidden=!yerevan; $('#district-input').disabled=!yerevan;
  $('#metro-row').hidden=!yerevan; $('#metro-minutes').disabled=!yerevan;
}
function travelHTML(l) {
  const parts=[];
  if(l.city==='Ереван'&&l.metro_walk_minutes)parts.push(`≈ ${l.metro_walk_minutes} мин пешком до метро`);
  if(l.center_drive_minutes)parts.push(`≈ ${l.center_drive_minutes} мин на машине до центра города`);
  return parts.length?`<p class="travel-note">${parts.map(esc).join('<br>')}<small>Оценка автора · на машине зависит от пробок</small></p>`:'';
}
async function addPhotos() {
  captureStep();
  if(state.busy||!requireUser())return;
  if(!state.bot)return toast('Чат бота пока недоступен.');
  if(state.photos.length>=10)return toast('Можно добавить до 10 фотографий.');
  try {localStorage.setItem(STORE+'-draft-'+state.user.id,JSON.stringify({draft:state.draft,postMarket:state.postMarket,formStep:state.formStep,phoneTouched:state.phoneTouched,at:Date.now()}));}
  catch {return toast('Не удалось сохранить форму. Разрешите хранилище приложения.');}
  state.busy=true;
  try {
    await api('/api/draft','POST',{text:state.text,photos:state.photos.map(p=>p.id)});
    state.telegramPhotos=true;seenDraftPhotos=new Set(state.photos.map(p=>p.id));safeOpen('https://t.me/'+state.bot+'?start=photos');
  } finally {state.busy=false;}
}
async function syncDraft(replaceText=false) {
  const d=await api('/api/draft'),photos=d.photos||[];
  if(replaceText){state.text=d.text||'';state.photos=photos;seenDraftPhotos=new Set();if(state.draft&&!state.phoneTouched){state.draft.phone=C.phoneFromText(state.text);state.phoneInferred=!!state.draft.phone;}}
  else state.photos=[...state.photos,...photos.filter(p=>!seenDraftPhotos.has(p.id))].slice(0,10);
  for(const p of photos)seenDraftPhotos.add(p.id);
  state.telegramPhotos=true;
  try {localStorage.removeItem(STORE+'-draft-'+state.user.id);} catch {}
}
function restoreDraft() {
  if(!state.user)return;
  try {
    const key=STORE+'-draft-'+state.user.id,saved=JSON.parse(localStorage.getItem(key)||'null');
    if(saved&&Date.now()-saved.at<7*86400000){state.draft=saved.draft;state.postMarket=saved.postMarket;state.formStep=saved.formStep||0;state.phoneTouched=!!saved.phoneTouched;}
  } catch {}
}
function safeOpen(url) {
  try {
    const u=new URL(url); if (!['http:','https:'].includes(u.protocol)) return;
    if (u.hostname==='t.me' && tg?.openTelegramLink) tg.openTelegramLink(u.href);
    else if (tg?.openLink) tg.openLink(u.href);
    else window.open(u.href,'_blank','noopener,noreferrer');
  } catch { toast('Ссылка недоступна.'); }
}
async function action(a,id,el) {
  captureStep();
  if(a==='choose-kind'){if(state.draft.kind===id)return;state.draft.kind=id;state.draft.rooms=null;render();return;}
  if(a==='choose-rooms'){state.draft.rooms=Number(id);render();return;}
  if(a==='agent-profile')return agentProfileSheet();
  if(a==='admin-agent')return adminAgentProfile(id);
  if(a==='gallery')return openGallery(id,Number(el.dataset.photoIndex)||0,el);
  if(a==='layout'){const anchor=[...document.querySelectorAll('.listing')].find(x=>x.getBoundingClientRect().bottom>100),offset=anchor?.getBoundingClientRect().top;state.layout=id==='grid'?'grid':'list';try{localStorage.setItem(STORE+'-layout',state.layout);}catch{}render();if(anchor){const next=[...document.querySelectorAll('.listing')].find(x=>x.dataset.listing===anchor.dataset.listing);if(next)window.scrollBy(0,next.getBoundingClientRect().top-offset);}return;}
  if(a==='market'){state.filters.market=id==='paid'?'paid':'free';state.filters.owner=false;persist();render();window.scrollTo(0,0);return;}
  if(a==='post-market'){if(id===state.postMarket)return;state.postMarket=id==='paid'?'paid':'free';if(state.draft){state.draft.commission=state.postMarket==='paid'?null:0;if(state.postMarket==='paid')state.draft.role='agent';}if(sheetKind==='commission')commissionSheet();persist();render();return;}
  if(a==='edit-commission')return commissionSheet();
  if (a==='conditions-filter') return filterConditions();
  if (a==='edit-contact') return editContact();
  if (a==='verify') return verificationSheet(id);
  if (a==='review-doc') return reviewDocument(id);
  if (a==='open-admin'){if(!state.user?.is_admin)return;await refreshAdmin();navigate('admin');return;}
  if (a==='official') {safeOpen('https://www.e-cadastre.am/ru/application/docview');return;}
  if (a==='telegram-contact'){const l=item(id);safeOpen('https://t.me/'+l.contact.slice(1));return;}
  if (a==='nav') { navigate(id); return; }
  if (a==='back') { back(); return; }
  if (a==='close'||a==='backdrop') { closeSheet(); return; }
  if (a==='budget') return budgetSheet();
  if (a==='district') return districtSheet();
  if (a==='rooms') return roomsSheet();
  if (a==='preset') { $('#budget-input').value=id; return; }
  if (a==='select-city') {state.filters.city=id;state.filters.district='';closeSheet();persist();render();return;}
  if (a==='select-district') { state.filters.city='Ереван';state.filters.district=id; closeSheet(); persist(); render(); return; }
  if (a==='select-rooms'||a==='set-rooms') {
    const d=a==='set-rooms'?state.draft:state.filters;
    d.kind=['room','house','aparthotel'].includes(id)?id:a==='set-rooms'?'apartment':'';
    d.rooms=['room','house','aparthotel',''].includes(id)?a==='set-rooms'?null:'':a==='set-rooms'?Number(id):id;
    closeSheet(); persist(); render(); return;
  }
  if (a==='sort') return showSheet('Порядок объявлений',`<div class="choice-list">${[['new','Сначала новые'],['price','Сначала дешевле']].map(([v,t])=>`<button class="choice-row ${state.sort===v?'active':''}" data-action="select-sort" data-id="${v}">${t}${state.sort===v?icon('check'):''}</button>`).join('')}</div>`,'','sort');
  if (a==='select-sort') { state.sort=id; closeSheet(); persist(); render(); return; }
  if (a==='reset-filters') { state.filters={...baseFilters(),market:state.filters.market}; persist(); render(); return; }
  if (a==='detail') return detail(id);
  if (a==='phone-listings') return phoneListings(id,el);
  if (a==='ban') return banSheet(id);
  if (a==='admin-tab'){state.adminTab=id;await refreshAdmin();render();return;}
  if (a==='unban'){el.disabled=true;try{await api('/api/admin/'+id+'/unban','POST',{});await refreshAdmin();await refresh();render();toast('Блокировка снята.');}finally{if(el.isConnected)el.disabled=false;}return;}
  if (a==='author-listings') return authorListings(id,el);
  if (a==='listing-post') {const l=item(id),url=l?.telegram_post_url;if(url)safeOpen(url);return;}
  if (a==='follow') return follow();
  if (a==='toggle-sub') return toggleSub(id);
  if (a==='delete-sub') {
    await api('/api/subscriptions/'+id,'DELETE');
    state.subs=state.subs.filter(s=>s.id!==id); persist(); render(); return;
  }
  if (a==='open-search') { state.filters={...baseFilters(),...state.subs.find(s=>s.id===id).filters}; navigate('feed'); return; }
  if (a==='prepare-listing') return prepareListing();
  if (a==='edit-price') return budgetSheet(true);
  if (a==='edit-address') return editAddress();
  if (a==='edit-rooms') return roomsSheet(true);
  if (a==='upload') return addPhotos();
  if (a==='remove-photo') { state.photos.splice(Number(id),1); persist(); if($('#photo-previews')) $('#photo-previews').innerHTML=photoPreviews(); else render(); return; }
  if (a==='publish') return publish();
  if (a==='mine'){if(!requireUser())return;await refresh();return mineSheet();}
  if (a==='status') {
    const l=state.own.find(x=>x.id===id); if(!l)return;
    const status=el.dataset.status==='active'?'active':'rented';
    await api('/api/listings/'+id+'/status','POST',{status}); await refresh();
    persist(); render(); mineSheet(); return;
  }
  if(a==='discussion'){const l=item(id);if(l?.telegram_discussion_url)safeOpen(l.telegram_discussion_url);return;}
  if (a==='contact') return contactSheet(id);
  if(a==='report')return reportSheet(id);
  if(a==='open-report')return reportSheet(id,true);
  if(a==='report-listing')return detail(id,true);
  if(a==='report-photos'){
    el.disabled=true;try{await saveReportForm();safeOpen('https://t.me/'+state.bot+'?start=proof_'+state.report.id);}finally{if(el.isConnected)el.disabled=false;}return;
  }
  if(a==='refresh-proofs'||a==='remove-proof'){
    el.disabled=true;try{await saveReportForm();if(a==='remove-proof')await api('/api/reports/'+state.report.id+'/photos/'+id,'DELETE');await reportSheet(state.report.id,true);}finally{if(el.isConnected)el.disabled=false;}return;
  }
  if (a==='approve'||a==='reject') { if(!state.user?.is_admin)return; await api('/api/admin/'+id+'/decision','POST',{decision:a==='approve'?'approve':'reject'}); await refreshAdmin(); await refresh(); render(); }
}
document.addEventListener('click',e=>{
  const b=e.target.closest('[data-action]'); if(!b || b.disabled)return;
  if (b.dataset.action==='backdrop' && e.target!==b) return;
  e.preventDefault(); Promise.resolve(action(b.dataset.action,b.dataset.id||'',b)).catch(e=>toast(e.message||'Не удалось выполнить действие.'));
});
document.addEventListener('input',e=>{
  if(e.target.dataset.field&&state.draft)state.draft[e.target.dataset.field]=e.target.type==='date'?(e.target.value||null):e.target.value;
  if(['available-from','available-until'].includes(e.target.id))updateAvailability();
  if(e.target.id==='listing-text') {state.text=e.target.value;if(!state.phoneTouched){state.draft.phone=C.phoneFromText(state.text);state.phoneInferred=!!state.draft.phone;const note=$('.phone-inferred');if(note)note.hidden=!state.phoneInferred;}persist();}
  captureStep();
});
document.addEventListener('change',e=>{
  if(e.target.id==='city-input')updateAddressCity(true);
  if(e.target.id==='agent-work'){const independent=e.target.value==='independent';$('#agency-row').hidden=independent;$('#agent-agency').required=!independent;}
  captureStep();
  if(e.target.id==='step-role') {state.draft.role=e.target.value;render();}
  if(e.target.id==='filter-city'){state.filters.city=e.target.value;state.filters.district='';closeSheet();persist();render();}
  if(e.target.dataset.field&&state.draft)state.draft[e.target.dataset.field]=e.target.type==='date'?(e.target.value||null):e.target.value;
  if(['available-from','available-until'].includes(e.target.id))updateAvailability();
  if(['price-currency','price-period'].includes(e.target.id)&&$('#budget-presets')) {
    const usd=$('#price-currency').value==='USD', day=$('#price-period').value==='day';
    const choices=usd?(day?[40,70,100]:[600,900,1200]):day?[15000,25000,35000]:[250000,350000,450000];
    $('#budget-input').value='';
    $('#budget-presets').innerHTML=choices.map(n=>`<button class="choice" type="button" data-action="preset" data-id="${n}">${money(n)}</button>`).join('')+'<button class="choice" type="button" data-action="preset" data-id="">Любая</button>';
  }
});
document.addEventListener('invalid',e=>{for(let el=e.target.parentElement;el;el=el.parentElement)if(el.tagName==='DETAILS')el.open=true;},true);
document.addEventListener('submit',e=>{
  e.preventDefault(); const x=Object.fromEntries(new FormData(e.target));
  if(['report-form','report-decision'].includes(e.target.id)){submitReport(e.target,x).catch(err=>toast(err.message));return;}
  if(['step-address-form','step-price-form'].includes(e.target.id)){prepareListing().catch(err=>toast(err.message));return;}
  if(['verification-form','verification-decision','contact-form','filter-conditions','ban-form','commission-form','agent-profile-form'].includes(e.target.id)){handleExtraForm(e.target,x).catch(err=>toast(err.message));return;}
  if(e.target.id==='budget-form'||e.target.id==='edit-price-form') {
    const amount=Number(String(x.amount).replace(/\s/g,''));
    if (!Number.isFinite(amount)||amount<0||amount>1e9) return toast('Укажите корректную сумму.');
    if(e.target.id==='budget-form') Object.assign(state.filters,{max:amount?String(amount):'',period:x.period,currency:x.currency});
    else {
      if(!amount)return toast('Цена должна быть больше нуля.');
      state.draft.prices[0]={amount,period:x.period,currency:x.currency};
    }
    closeSheet(); persist(); render();
  }
  if(e.target.id==='address-form') {
    const next=x.address.trim();
    const city=(x.city==='other'?x.custom_city:x.city).trim();
    if(!city)return toast('Укажите город или населённый пункт.');
    const metro=x.metro_walk_minutes?Number(x.metro_walk_minutes):null, center=x.center_drive_minutes?Number(x.center_drive_minutes):null;
    if((metro!==null&&(!Number.isInteger(metro)||metro<1||metro>180))||(center!==null&&(!Number.isInteger(center)||center<1||center>360)))return toast('Укажите время в целых минутах.');
    Object.assign(state.draft,{address:next,city,district:city==='Ереван'?x.district:'',metro_walk_minutes:city==='Ереван'?metro:null,center_drive_minutes:center});
    persist(); closeSheet(); render();
  }
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape') { e.preventDefault(); back(); }
  if(e.key==='Tab'&&sheetKind&&!photoGallery) {
    const items=[...$('.sheet').querySelectorAll('button,input,select,textarea,a[href]')].filter(x=>!x.disabled&&x.offsetParent!==null);
    if(!items.length)return;
    if(e.shiftKey&&document.activeElement===items[0]) { e.preventDefault(); items.at(-1).focus(); }
    else if(!e.shiftKey&&document.activeElement===items.at(-1)) { e.preventDefault(); items[0].focus(); }
  }
});
function updateBackButton() {
  try { if(photoGallery||sheetKind||!['feed'].includes(state.screen)) tg?.BackButton?.show(); else tg?.BackButton?.hide(); } catch {}
}
function applyTheme() {
  const dark=tg?.initData?tg.colorScheme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme=dark?'dark':'light';
  const params=tg?.themeParams;
  if(tg?.initData&&params) for(const [css,key] of [['--bg','secondary_bg_color'],['--surface','bg_color'],['--ink','text_color'],['--muted','hint_color'],['--accent','button_color'],['--on-accent','button_text_color']]) {
    if(/^#[0-9a-f]{6}$/i.test(params[key]||'')) document.documentElement.style.setProperty(css,params[key]);
  }
  try { const bg=getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(); tg?.setHeaderColor(bg); tg?.setBackgroundColor(bg); } catch {}
}
function initTelegram() {
  const app=window.Telegram?.WebApp; if(!app || tg===app)return;
  tg=app;
  try { tg.ready(); tg.expand(); tg.BackButton.onClick(back); tg.onEvent('themeChanged',applyTheme); } catch {}
  applyTheme(); updateBackButton();
  if(state.booted&&tg.initData) connect().catch(e=>{state.error=e.message;render();});
}
window.addEventListener('tg-ready',initTelegram);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',applyTheme);
let normalHeight=window.innerHeight, viewportWidth=innerWidth;
function syncKeyboard() {
  const v=window.visualViewport; if(!v)return;
  if(viewportWidth!==innerWidth) { normalHeight=innerHeight; viewportWidth=innerWidth; }
  normalHeight=Math.max(normalHeight,innerHeight);
  const keyboard=normalHeight-v.height>160 && /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
  document.documentElement.style.setProperty('--keyboard',Math.max(0,innerHeight-v.height-v.offsetTop)+'px');
  document.documentElement.dataset.keyboard=String(keyboard);
}
window.visualViewport?.addEventListener('resize',syncKeyboard);
window.visualViewport?.addEventListener('scroll',syncKeyboard);
document.addEventListener('focusout',()=>setTimeout(syncKeyboard,100));
async function startRoute() {
  if(startHandled)return;
  const p=tg?.initDataUnsafe?.start_param||new URLSearchParams(location.search).get('start')||new URLSearchParams(location.search).get('tgWebAppStartParam')||'';
  if(!p)return;
  if(!state.user && (['draft','admin'].includes(p)||p.startsWith('report_')||p.startsWith('review_')))return;
  startHandled=true;
  if(p.startsWith('report_')){await reportSheet(p.slice(7),true);return;}
  if(p.startsWith('review_')&&state.user?.is_admin){
    const id=p.slice(7);state.adminTab='review';await refreshAdmin();
    if(!state.queue.some(l=>l.id===id)){state.adminTab='all';await refreshAdmin();}
    navigate('admin',false);const row=$(`[data-admin-id="${CSS.escape(id)}"]`);
    if(row){row.setAttribute('tabindex','-1');row.scrollIntoView({block:'start'});row.focus({preventScroll:true});}return;
  }
  if(p.startsWith('l_')) { detail(p.slice(2)); return; }
  if(['add','draft'].includes(p)) {
    if(state.user&&p==='draft') {restoreDraft();await syncDraft(true);}
    goStep(state.draft?state.formStep:0,false);
  }
  if(p==='admin' && state.user?.is_admin) { state.queue=await api('/api/admin/queue'); navigate('admin',false); }
}
let connecting=false;
async function connect() {
  if(connecting)return;connecting=true;
  try {
    const config=await api('/api/config');state.bot=config.bot_username;
    state.channelConfigured=!!config.channel_configured;state.paidChannelConfigured=!!config.paid_channel_configured;
    if(tg?.initData)state.user=await api('/api/me');
    await refresh();render();await startRoute();
  } finally {connecting=false;}
}
(async()=>{
  history.replaceState({rent:true,screen:'feed'},''); initTelegram(); applyTheme(); render();
  if(['http:','https:'].includes(location.protocol)) {
    try { await connect(); }
    catch(e) { state.error='Не удалось подключиться к серверу. '+e.message; render(); }
  }
  state.booted=true; await startRoute();
})();

let refreshPending=false;
async function refreshVisible() {
  if(document.hidden || !state.booted || refreshPending) return;
  refreshPending=true;
  try {
    await refresh();state.error='';
    if(state.user&&state.telegramPhotos&&['add','review'].includes(state.screen)){
      const before=state.photos.length;await syncDraft();
      if(state.photos.length!==before&&$('#photo-previews'))$('#photo-previews').innerHTML=photoPreviews();
    }
  }
  catch { state.error='Нет связи. Новые объявления пока не загружаются.'; }
  finally {
    if(state.screen==='feed' && !sheetKind) {
      const y=window.scrollY; render(); window.scrollTo(0,y);
    }
    if(sheetKind==='mine')mineSheet();
    if(sheetKind==='detail') {
      const l=item(sheetArg);
      const count=document.querySelector('.sheet .view-count');if(l&&count)count.outerHTML=viewsHTML(l);
      const age=document.querySelector('.sheet .publication-age');
      if(l && age) age.outerHTML=ageHTML(l);
      const action=document.querySelector('.sheet-footer [data-action="contact"]');
      if(l && displayStatus(l)!=='active' && action) { action.disabled=true; action.textContent=niceStatus(l); }
    }
    refreshPending=false;
  }
}
setInterval(refreshVisible,30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshVisible();});

function rentalDate(value) {
  return new Date(value+'T12:00:00').toLocaleDateString('ru-RU',{day:'numeric',month:'short',year:'numeric'});
}
function availabilityHTML(l) {
  const text=[l.available?'с '+rentalDate(l.available):'',l.available_until?'до '+rentalDate(l.available_until):''].filter(Boolean).join(' · ');
  return text?`<p class="availability-note">Сдаётся ${esc(text)}</p>`:'';
}
function availabilityError(d) {
  for(const date of [d.available,d.available_until])if(date){
    const stamp=Date.parse(date+'T12:00:00Z');
    if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||!Number.isFinite(stamp)||new Date(stamp).toISOString().slice(0,10)!==date)return 'Укажите полную дату: день, месяц и год.';
  }
  return d.available&&d.available_until&&d.available_until<d.available?'Дата окончания должна быть не раньше начала.':'';
}
function availabilityFields(d) {
  return `<section class="availability-fields" aria-label="Даты аренды"><div class="label-row"><strong>Когда сдаётся</strong><span class="muted">необязательно</span></div><div class="field-row"><div><label for="available-from">С какого числа</label><input id="available-from" type="date" data-field="available" value="${esc(d.available||'')}" max="9999-12-31"></div><div><label for="available-until">До какого числа</label><input id="available-until" type="date" data-field="available_until" value="${esc(d.available_until||'')}" min="${esc(d.available||'')}" max="9999-12-31"></div></div><p class="note">Заполните даты, если срок известен.</p><p id="availability-error" class="error-note" role="alert" ${availabilityError(d)?'':'hidden'}>${esc(availabilityError(d))}</p></section>`;
}
function updateAvailability() {
  const error=availabilityError(state.draft);
  $('#available-until').min=state.draft.available||'';
  $('#availability-error').textContent=error;$('#availability-error').hidden=!error;
}
function optionalFields(d) {
  const opts=[['unknown','Не указано'],['yes','Да'],['ask','По договорённости'],['no','Нет']];
  return `<div><label for="opt-contract">Готовы подписать письменный договор?</label><select id="opt-contract" data-field="contract">${selectOptions(opts,d.contract||'unknown')}</select></div><div><label for="opt-registration">Поможете с регистрацией по месту проживания?</label><select id="opt-registration" data-field="residence_registration">${selectOptions(opts,d.residence_registration||'unknown')}</select></div><details><summary>Регистрация права аренды / договора ${icon('down')}</summary><label for="opt-lease">Готовы к оформлению через кадастр?</label><select id="opt-lease" data-field="lease_registration">${selectOptions(opts,d.lease_registration||'unknown')}</select><p class="note">Регистрация права аренды через кадастр.</p></details><div><label for="wishes">Важные условия и пожелания</label><textarea class="short-text" id="wishes" data-field="wishes" maxlength="1200" placeholder="Например, с котом можно; тишина после 23:00. Можно оставить в основном тексте.">${esc(d.wishes||'')}</textarea></div>`;
}
function conditionsHTML(l) {
  const rows=[];
  if(l.deposit!=null)rows.push(['Депозит',l.deposit===0?'Без депозита':money(l.deposit)+' '+sym(l.prices?.[0]?.currency||'AMD')]);
  if(l.term)rows.push(['Срок',l.term]);
  if(l.available)rows.push(['Сдаётся с',rentalDate(l.available)]);
  if(l.available_until)rows.push(['Сдаётся до',rentalDate(l.available_until)]);
  if(l.utilities)rows.push(['Коммунальные',l.utilities]);
  if(l.contract==='yes')rows.push(['Договор','Автор готов подписать']);
  if(['yes','ask'].includes(l.residence_registration))rows.push(['Регистрация проживания',l.residence_registration==='yes'?'Автор согласен':'Условия нужно обсудить']);
  if(['yes','ask'].includes(l.lease_registration))rows.push(['Оформление через кадастр',l.lease_registration==='yes'?'Автор согласен':'Нужно обсудить']);
  const extras=(l.prices||[]).length>1||(l.prices||[]).some(p=>p.amount_max||p.condition)?`<div class="rate-options">${(l.prices||[]).map(p=>`<p><strong>${money(p.amount)}${p.amount_max?'–'+money(p.amount_max):''} ${esc(sym(p.currency))} / ${unit(p.period)}</strong>${p.condition?'<br><span>'+esc(p.condition)+'</span>':''}</p>`).join('')}</div>`:'';
  return `${extras}${rows.length?`<dl class="facts">${rows.map(([k,v])=>`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>`:''}${l.wishes?`<p class="details-text">${esc(l.wishes)}</p>`:''}`;
}
function trustHTML(l) {
  const verified=['owner_verified','representative_verified'].includes(l.document_status);
  const titles={owner_verified:'Собственник сверён',representative_verified:'Представитель сверён',document_checked:'Документ проверен',pending:'Документ на проверке',not_confirmed:'Проверка не подтверждена'};
  const title=titles[l.document_status]||(l.role==='owner'?'Собственник — со слов автора':'Проверка собственности');
  const detail=verified?'Администратор сверил документ через e-cadastre, объект, личность и актуальность права.':l.document_status==='document_checked'?'Сверены документ через e-cadastre и сведения об объекте.':'Администратор сверяет документ через e-cadastre, отдельно личность и право на размещение.';
  return `<details class="trust-box"><summary>${icon(verified||l.document_status==='document_checked'?'check':'info')}<span>${title}</span>${icon('down')}</summary><p class="note">${detail}</p>${l.verification?.checked_on?`<p class="note">Дата сверки: ${esc(l.verification.checked_on)}</p>`:''}<button class="text-button" data-action="official">Официальный способ проверки ↗</button></details>`;
}

function filterConditions() {
  const f=state.filters;
  showSheet('Только когда это важно',`<form id="filter-conditions" class="stack">${[['contract','Готовы подписать договор'],['residence_registration','Возможна регистрация проживания'],['verified','Личность и право сверены']].map(([k,t])=>`<label class="check-row"><input type="checkbox" name="${k}" ${f[k]?'checked':''}><span>${t}</span></label>`).join('')}<p class="note">Поиск учитывает согласие и регистрацию по договорённости. Условия и цена — в карточке.</p></form>`,`<button type="submit" form="filter-conditions" class="button">Показать варианты</button>`,'conditions-filter');
}
async function agentProfileSheet() {
  if(!requireUser())return;
  const x=await api('/api/agent-profile');
  showSheet('Профиль агента',`<form id="agent-profile-form" class="stack"><p class="note">Заполняется один раз. Имя и телефон доступны только вам и администратору. В объявлении видно агентство или «Частный агент».</p><div><label for="agent-name">Имя и фамилия</label><input id="agent-name" name="full_name" value="${esc(x.full_name||[state.user.first_name,state.user.last_name].filter(Boolean).join(' '))}" autocomplete="name" minlength="3" maxlength="120" required></div><div><label for="agent-phone">Телефон для проверки</label><input id="agent-phone" name="phone" type="tel" autocomplete="tel" value="${esc(x.phone||state.draft?.phone||'')}" maxlength="40" placeholder="+374…" required></div><div><label for="agent-work">Как работаете</label><select id="agent-work" name="work">${selectOptions([['agency','В агентстве'],['independent','Частный агент']],x.independent?'independent':'agency')}</select></div><div id="agency-row" ${x.independent?'hidden':''}><label for="agent-agency">Название агентства</label><input id="agent-agency" name="agency" value="${esc(x.agency||'')}" minlength="2" maxlength="120" ${x.independent?'':'required'}></div></form>`,`<button class="button" form="agent-profile-form" type="submit">Сохранить профиль</button>`,'agent-profile');
}
function adminAgentButton(l) {
  return state.user?.is_admin&&l.role==='agent'&&l.agent_affiliation?`<button class="text-button" data-action="admin-agent" data-id="${esc(l.id)}">Данные агента</button>`:'';
}
async function adminAgentProfile(id) {
  if(!state.user?.is_admin)return;
  const x=await api('/api/admin/'+encodeURIComponent(id)+'/agent-profile');
  showSheet('Данные агента',`<div class="stack"><p>${esc(x.full_name)}</p><p>${esc(x.phone)}</p><p>${esc(x.independent?'Частный агент':x.agency)}</p><p class="note">Данные со слов агента. Личность проверяется отдельно.</p></div>`,'','admin-agent',id);
}

function editContact() {
  const d=state.draft;
  showSheet('Контакт автора',`<form id="contact-form" class="stack"><div><label for="publisher-role">Кто размещает</label><select id="publisher-role" name="role">${roleOptions(d)}</select></div><p>${state.user?.username?'Telegram: @'+esc(state.user.username):'Без username связь доступна только в обсуждении публикации.'}</p><div><label for="phone-contact">Телефон — необязательно</label><input id="phone-contact" type="tel" name="phone" value="${esc(d.phone||'')}" maxlength="40"></div></form>`,`<button class="button" type="submit" form="contact-form">Готово</button>`,'contact-edit');
}
async function contactSheet(id) {
  const l=await api('/api/listings/'+encodeURIComponent(id));if(l.status!=='active')return toast('Объявление снято.');
  Object.assign(item(id)||{},l);
  const contact=/^@[A-Za-z0-9_]{5,32}$/.test(l.contact||'')?l.contact:'';
  showSheet('Связаться с автором',`<div class="stack">${contact?`<button class="button" data-action="telegram-contact" data-id="${esc(id)}">Написать ${esc(contact)}</button>`:`<button class="button" data-action="discussion" data-id="${esc(id)}" ${l.telegram_discussion_url?'':'disabled'}>Открыть обсуждение</button>${l.telegram_discussion_url?'':'<p class="note">Обсуждение недоступно.</p>'}`}${contact&&/^\+[1-9]\d{7,14}$/.test(l.phone||'')?`<a class="button secondary" href="tel:${esc(l.phone)}">Позвонить ${esc(l.phone)}</a>`:''}</div>`,'','contact',id);
}
function verificationSheet(id) {
  const l=item(id);if(!l?.is_mine)return toast('Проверку запрашивает автор своего объявления.');
  if(l.document_status==='pending')return toast('Документ уже ожидает администратора.');
  showSheet('Подтвердить собственность',`<form id="verification-form" data-listing="${esc(id)}" class="stack" autocomplete="off"><p>Администратор сверит документ через <strong>e-cadastre</strong>, отдельно личность и право на размещение.</p><div><label for="applicant-name">Ваше имя как в документе</label><input id="applicant-name" name="applicant_name" minlength="3" maxlength="140" required autocomplete="off"></div><div><label for="document-number">Номер кадастрового документа</label><input id="document-number" name="document_number" minlength="4" maxlength="80" required autocomplete="off"></div><div><label for="document-password">Пароль этого документа</label><input id="document-password" name="document_password" type="password" minlength="4" maxlength="100" required autocomplete="new-password"></div><p class="note">Пароль просмотра, указанный в кадастровом документе.</p><label class="check-row"><input name="consent" type="checkbox" required><span>Разрешаю администратору использовать реквизиты для проверки этого объявления.</span></label><p class="note">Реквизиты доступны администратору, хранятся зашифрованными и удаляются после решения или через 7 дней.</p></form>`,`<button class="button" type="submit" form="verification-form">Отправить на проверку</button>`,'verification',id);
}
async function reviewDocument(id) {
  if(!state.user?.is_admin)return;
  const x=await api('/api/admin/'+id+'/private');
  if(!x.document_number)return toast('Реквизиты отсутствуют или удалены.');
  const today=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Yerevan'});
  showSheet('Проверка администратором',`<form id="verification-decision" data-listing="${esc(id)}" class="stack"><p class="note">Откройте e-cadastre, введите реквизиты и сверьте результат.</p><div><label>Имя заявителя</label><input readonly value="${esc(x.applicant_name||'Не указано')}"></div><div><label>Номер документа</label><input readonly value="${esc(x.document_number)}"></div><div><label>Пароль просмотра</label><input readonly value="${esc(x.document_password)}" autocomplete="off"></div><button type="button" class="button secondary" data-action="official">Открыть официальный e-cadastre ↗</button>${[['document_valid','На официальном сайте документ подлинный и действительный'],['object_matches','Адрес и объект совпадают с объявлением'],['identity_matches','Личность заявителя сверена отдельно'],['rights_current','Сведения о праве актуальны на дату проверки'],['authority_checked','Для представителя: полномочия отдельно сверены']].map(([k,t])=>`<label class="check-row"><input name="${k}" type="checkbox"><span>${t}</span></label>`).join('')}<div><label for="checked-date">Дата сверки сведений</label><input id="checked-date" type="date" name="checked_on" value="${today}" required></div><div><label for="verification-result">Результат</label><select name="result" id="verification-result">${selectOptions([['document_checked','Документ и объект проверены'],['owner_verified','Собственник: документ + личность + право'],['representative_verified','Представитель: дополнительно полномочия'],['not_confirmed','Не подтверждено']],'document_checked')}</select></div><p class="note">Положительное решение удалит реквизиты. Публично останутся метод, дата и перечень выполненных проверок.</p></form>`,`<button class="button" type="submit" form="verification-decision">Сохранить результат</button>`,'review-doc',id);
}
async function handleExtraForm(form,x) {
  if(form.id==='agent-profile-form'){
    const button=$('button[form="agent-profile-form"]');button.disabled=true;
    try {const result=await api('/api/agent-profile','PUT',{full_name:x.full_name,phone:x.phone,independent:x.work==='independent',agency:x.agency});state.user.agent_profile_ready=result.ready;state.user.agent_affiliation=result.affiliation;closeSheet();render();}
    finally {if(button.isConnected)button.disabled=false;}return;
  }
  if(form.id==='commission-form'){const amount=Number(x.amount),fixed=['AMD','USD'].includes(x.fee_unit);if(!Number.isInteger(amount)||amount<=0||amount>(fixed?1000000000:100))return toast('Укажите корректную комиссию.');Object.assign(state.draft,{commission:amount,commission_type:fixed?'fixed':'percent',commission_currency:fixed?x.fee_unit:'AMD',commission_basis:x.fee_unit==='percent_day'?'day':'month',role:'agent'});state.postMarket='paid';closeSheet();render();return;}

  if(form.id==='filter-conditions'){
    for(const k of ['contract','residence_registration','verified'])state.filters[k]=x[k]==='on';closeSheet();persist();render();return;
  }
  if(form.id==='contact-form'){
    x.phone=x.phone.replace(/[ ()\-\u00a0]/g,'');
    if(x.phone&&!/^\+[1-9]\d{7,14}$/.test(x.phone))return toast('Введите телефон с кодом страны: +374…');
    Object.assign(state.draft,x);state.phoneInferred=false;state.phoneTouched=true;closeSheet();render();return;
  }
  const lid=form.dataset.listing,button=$(`button[form="${form.id}"]`);if(button)button.disabled=true;
  try {
    if(form.id==='ban-form'){
      await api('/api/admin/'+lid+'/ban','POST',{reason:x.reason.trim()});form.reset();closeSheet();await refresh();await refreshAdmin();navigate('admin');toast('Объявление заблокировано. Причина видна автору.');
    } else if(form.id==='verification-form'){
      await api('/api/listings/'+lid+'/verification','POST',{...x,consent:x.consent==='on'});form.reset();await refresh();closeSheet();toast('Реквизиты отправлены администратору.');
    } else if(form.id==='verification-decision'){
      for(const k of ['document_valid','object_matches','identity_matches','rights_current','authority_checked'])x[k]=x[k]==='on';
      await api('/api/admin/'+lid+'/verification','POST',x);form.reset();closeSheet();await refreshAdmin();await refresh();render();toast('Результат сохранён. Реквизиты удалены.');
    }
  } finally {if(button?.isConnected)button.disabled=false;}
}

function commissionSheet() {
  const d=state.draft||{},paid=state.postMarket==='paid';
  const value=d.commission_type==='fixed'?d.commission_currency||'AMD':d.commission_basis==='day'?'percent_day':'percent_month';
  const body=marketSwitch(true)+(paid?`<form id="commission-form" class="stack"><p class="note">Платёж агенту отдельно от аренды, один раз.</p><div><label for="commission-amount">Размер комиссии</label><input id="commission-amount" name="amount" type="number" inputmode="numeric" min="1" max="1000000000" step="1" value="${d.commission>0?d.commission:''}" placeholder="50" required></div><div><label for="commission-unit">Как рассчитывается</label><select id="commission-unit" name="fee_unit">${selectOptions([['percent_month','% от аренды за месяц'],['percent_day','% от аренды за сутки'],['AMD','Фиксированно · ֏'],['USD','Фиксированно · $']],value)}</select></div><p class="note">Размещение в разделе «С комиссией» — от имени агента.</p></form>`:'<p class="note">Комиссия для арендатора — 0.</p>');
  showSheet('Комиссия',body,paid?'<button class="button" type="submit" form="commission-form">Готово</button>':'<button class="button" data-action="close">Готово</button>','commission');
}
function closeGallery() {
  const viewer=photoGallery;if(!viewer)return;
  if(viewer.opener.isOpen)viewer.close();else viewer.on('openingAnimationEnd',()=>viewer.close());
}
function finishGallery() {
  if(photoGallery||galleryBackPending)return;
  const next=galleryAfter;galleryAfter=null;if(next)next();
}
async function galleryPhoto(p) {
  const src=safePhoto(p);
  if(p.width>0&&p.height>0)return {src,width:p.width,height:p.height,msrc:thumbPhoto(p)};
  return new Promise(resolve=>{
    const im=new Image(),timer=setTimeout(()=>done(),8000);let finished=false;
    function done(){if(finished)return;finished=true;clearTimeout(timer);resolve({src,width:im.naturalWidth||1600,height:im.naturalHeight||1200,msrc:thumbPhoto(p)});}
    im.onload=done;im.onerror=done;im.src=src;
  });
}
async function openGallery(id,index=0,trigger=null) {
  if(photoGallery||openingGallery)return;
  openingGallery=true;trigger?.setAttribute('aria-busy','true');
  try {
    let l=item(id);if(!l)return;
    {const current=await api('/api/listings/'+encodeURIComponent(id));Object.assign(l,current);}
    const photos=(l.photos||[]).filter(p=>safePhoto(p)).slice(0,10);
    if(!photos.length)return detail(id);
    const data=await Promise.all(photos.map(galleryPhoto));
    const originalOverflow=document.body.style.overflow,verticalSwipes=tg?.isVerticalSwipesEnabled!==false;
    let footer=null;
    const viewer=new PhotoSwipe({dataSource:data.map((p,i)=>({...p,alt:`Фото ${i+1}: ${l.address}`})),index:Math.max(0,Math.min(index,photos.length-1)),loop:false,preload:[1,1],mainClass:'rent-gallery',bgOpacity:1,showHideAnimationType:matchMedia('(prefers-reduced-motion: reduce)').matches?'none':'fade',showAnimationDuration:160,hideAnimationDuration:160,closeTitle:'Закрыть фотографии',zoomTitle:'Увеличить',arrowPrevTitle:'Предыдущее фото',arrowNextTitle:'Следующее фото',errorMsg:'Фото не загрузилось. Перейдите к следующему снимку.',paddingFn:()=>({top:Math.max(60,document.querySelector('.pswp__top-bar')?.offsetHeight||60),bottom:footer?.offsetHeight||180,left:0,right:0})});
    photoGallery=viewer;
    viewer.on('uiRegister',()=>viewer.ui.registerElement({name:'rental-footer',order:9,appendTo:'root',html:`<div class="gallery-thumbs">${photos.map((p,i)=>`<button data-gallery-index="${i}" aria-label="Фото ${i+1}"><img src="${esc(thumbPhoto(p))}" alt="" loading="lazy"></button>`).join('')}</div><div class="gallery-summary"><strong>${priceHTML(l)}</strong><span>${esc(l.city+' · '+l.address)}</span><span class="gallery-fee">${esc(commissionLabel(l))}</span></div><div class="gallery-actions"><button class="button secondary" data-gallery-action="details">Условия</button><button class="button" data-gallery-action="contact" >Связаться</button></div>`,onInit:el=>{
      footer=el;
      el.addEventListener('click',e=>{
        const button=e.target.closest('button');if(!button)return;
        if(button.dataset.galleryIndex!==undefined){viewer.goTo(Number(button.dataset.galleryIndex));return;}
        if(button.dataset.galleryAction){galleryAfter=()=>button.dataset.galleryAction==='details'?detail(id):contactSheet(id);closeGallery();}
      });
    }}));
    viewer.on('change',()=>{
      footer?.querySelectorAll('[data-gallery-index]').forEach((el,i)=>{el.setAttribute('aria-pressed',String(i===viewer.currIndex));if(i===viewer.currIndex)el.scrollIntoView({block:'nearest',inline:'nearest'});});
    });
    viewer.on('close',()=>{if(history.state?.gallery){galleryBackPending=true;history.back();}});
    viewer.on('destroy',()=>{
      photoGallery=null;document.body.style.overflow=originalOverflow;$('#app').inert=!!sheetKind;$('#modal-root').inert=false;
      if(verticalSwipes)tg?.enableVerticalSwipes?.();updateBackButton();finishGallery();
    });
    document.body.style.overflow='hidden';$('#app').inert=true;$('#modal-root').inert=true;tg?.disableVerticalSwipes?.();
    history.pushState({...history.state,rent:true,gallery:true},'');viewer.init();viewer.updateSize(true);updateBackButton();void recordView(l);
  } finally {openingGallery=false;trigger?.removeAttribute('aria-busy');}
}
document.addEventListener('error',e=>{
  const img=e.target;if(!(img instanceof HTMLImageElement)||!img.closest('.photo-tile'))return;
  if(img.dataset.fullSrc&&img.src!==new URL(img.dataset.fullSrc,location.href).href){img.src=img.dataset.fullSrc;delete img.dataset.fullSrc;return;}
  img.closest('.photo-tile').classList.add('photo-broken');
},true);
