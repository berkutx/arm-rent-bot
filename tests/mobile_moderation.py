"""Mobile UI against the real FastAPI routes in a temporary database.
Browser requests are intercepted and routed to TestClient; no Telegram/network calls.
"""
import json,os,sys,tempfile
from pathlib import Path
from urllib.parse import urlparse
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
import server as s
from test_server import signed,body
O=R/'test-results';errors=[];requests=[]
with tempfile.TemporaryDirectory(prefix='svoi-mobile-',ignore_cleanup_errors=True) as tmp:
 s.DATA=Path(tmp);s.DB=s.DATA/'db.sqlite3';s.LIVE=True;s.TOKEN='test-token';s.BOT='test_bot';s.PUBLIC_URL='https://svoi.test';s.ADMINS={99};s.CHAT='';s.setup()
 # No TestClient lifespan: the actual Telegram poller and jobs are never started.
 client=TestClient(s.app)
 def create(uid,address,role='owner'):
  payload=body(address=address);payload['listing'].update(phone='+374 (91) 123456',role=role)
  response=client.post('/api/listings',headers=signed(uid),json=payload);assert response.status_code==200,response.text
  return response.json()
 first=create(42,'Мобильная 10');second=create(43,'Мобильная 20');agent=create(44,'Мобильная 30','agent')
 with sync_playwright() as p:
  b=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox'])
  page=b.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
  page.on('pageerror',lambda e:errors.append(str(e)))
  init={'initData':signed()['X-Telegram-Init-Data'],'initDataUnsafe':{},'colorScheme':'light','themeParams':{}}
  page.add_init_script('window.Telegram={WebApp:'+json.dumps(init)+'};Object.assign(window.Telegram.WebApp,{ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}});')
  def route(req):
   url=urlparse(req.request.url);path=url.path+('?' +url.query if url.query else '')
   if url.hostname!='svoi.test':return req.fulfill(content_type='application/javascript',body='')
   method=req.request.method;requests.append((method,path))
   headers={k:v for k,v in req.request.headers.items() if k in ('x-telegram-init-data','content-type')}
   response=client.request(method,path,headers=headers,content=req.request.post_data)
   req.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
  page.route('**/*',route);page.goto('https://svoi.test/');page.locator('.listing').first.wait_for()
  def actor(uid):
   init_data=signed(uid)['X-Telegram-Init-Data'] if uid else ''
   page.evaluate('async raw=>{tg.initData=raw;state.user=raw?await api("/api/me"):null;await refresh();navigate("feed",false);toast("");}',init_data)
  def open_first():
   page.locator('[data-listing="'+first['id']+'"] .listing-main').click()
   page.locator('.sheet .view-count').wait_for()
  # Phone-only parsing; precise fields still require manual input.
  page.locator('[data-action=nav][data-id=add]').click()
  text='С 15 числа до мая, цена 600.000. Телефон +374 (91) 123456.'
  page.locator('#listing-text').fill(text);page.locator('#continue').click()
  assert page.evaluate('state.draft.phone')=='+37491123456'
  assert page.evaluate('state.draft.prices[0].amount')==0 and page.evaluate('state.draft.address')==''
  assert page.evaluate('state.draft.available') is None
  assert page.locator('.phone-inferred').is_visible()
  page.locator('[data-action=edit-contact]').click()
  assert page.locator('#phone-contact').input_value()=='+37491123456'
  page.locator('#publisher-role').select_option('agent')
  page.screenshot(animations='disabled',path=str(O/'phone-role-390.png'))
  page.locator('#phone-contact').fill('');page.locator('button[form=contact-form]').click();page.wait_for_timeout(120)
  page.locator('[data-action=edit-text]').click();page.locator('#continue').click()
  assert page.evaluate('state.draft.phone')=='' and page.evaluate('state.draft.role')=='agent'
  actor(42);open_first()
  page.wait_for_function('document.querySelector(".sheet .view-count")?.textContent.trim()==="1"')
  assert page.locator('.community-posts').count()==0
  page.locator('[data-action=phone-listings]').click();page.locator('.phone-listings').wait_for()
  assert page.locator('.phone-listings .author-listing-row').count()==1
  assert 'Мобильная 20' in page.locator('.phone-listings').inner_text()
  page.screenshot(animations='disabled',path=str(O/'same-phone-390.png'))
  page.locator('.sheet-footer [data-action=detail]').click()
  page.wait_for_function('document.querySelector(".sheet .view-count")?.textContent.trim()==="1"')
  actor(43);open_first()
  page.wait_for_function('document.querySelector(".sheet .view-count")?.textContent.trim()==="2"')
  actor(None);open_first()
  assert page.locator('.sheet .view-count').inner_text().strip()=='2'
  assert client.get('/api/listings/'+first['id']).json()['view_count']==2
  actor(42)
  page.locator('[data-action=mine]').click();row=page.locator('[data-mine-id="'+first['id']+'"]')
  assert 'Актуально' in row.inner_text();row.locator('[data-action=status]').click()
  page.wait_for_function('document.querySelector(".my-row .listing-status")?.textContent.includes("Сдано")')
  page.locator('.my-row [data-action=status]').click()
  page.wait_for_function('document.querySelector(".my-row .listing-status")?.textContent.includes("Актуально")')
  # Admin bans any existing ad; an empty reason cannot submit.
  actor(99);page.locator('[data-action=open-admin]').click();page.locator('[data-action=admin-tab][data-id=all]').click()
  page.locator('[data-admin-id="'+first['id']+'"] [data-action=ban]').click()
  assert not page.locator('#ban-reason').evaluate('(x)=>x.checkValidity()')
  reason='Скрытая комиссия. Исправьте условия объявления.'
  page.locator('#ban-reason').fill(reason);page.screenshot(animations='disabled',path=str(O/'ban-reason-390.png'))
  page.locator('button[form=ban-form]').click()
  page.locator('[data-admin-id="'+first['id']+'"] .status-banned').wait_for()
  assert client.get('/api/listings/'+first['id']).status_code==404
  actor(42);page.locator('[data-action=mine]').click()
  row=page.locator('[data-mine-id="'+first['id']+'"]')
  assert reason in row.inner_text() and 'Заблокировано' in row.inner_text()
  assert row.locator('[data-action=status]').count()==0
  for width in [320,360,390,430]:
   page.set_viewport_size({'width':width,'height':844})
   assert page.evaluate('document.documentElement.scrollWidth')==width
   assert page.locator('.sheet-body').evaluate('(x)=>x.scrollWidth<=x.clientWidth')
   page.screenshot(animations='disabled',path=str(O/f'my-ban-{width}.png'))
  page.set_viewport_size({'width':390,'height':844})
  page.evaluate('tg.colorScheme="dark";applyTheme()');page.screenshot(animations='disabled',path=str(O/'my-ban-dark-390.png'))
  actor(99);page.locator('[data-action=open-admin]').click();page.locator('[data-action=admin-tab][data-id=banned]').click()
  expect(page.locator('[data-admin-id]')).to_have_count(1)
  page.locator('[data-action=unban]').click();page.wait_for_function('!document.querySelector("[data-admin-id]")')
  restored=client.get('/api/listings/'+first['id']).json()
  assert restored['status']=='active' and restored['view_count']==2 and restored['created_at']==first['created_at']
  actor(42);page.locator('[data-listing="'+agent['id']+'"] .listing-main').click()
  assert page.locator('.sheet [data-action=phone-listings]').count()==0
  assert not errors,errors
  report={'result':'passed','mobile_widths':[320,360,390,430],'js_errors':errors,'scenarios':['phone mask only, manual clear persists','explicit realtor role','same phone without realtor','unique views across authenticated users and anonymous display','My listings active and rented','admin required ban reason','owner sees ban and cannot restore','admin unban retains date and views'],'telegram_calls':0,'scope':'Chromium UI with intercepted requests to real FastAPI TestClient and temporary SQLite; no live data or Telegram network'}
  (O/'mobile-moderation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
  b.close()
