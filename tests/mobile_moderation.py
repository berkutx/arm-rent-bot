"""Mobile UI against the real FastAPI routes in a temporary database.
Browser requests are intercepted and routed to TestClient; no Telegram/network calls.
"""
import asyncio,base64,json,os,sys,tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
from fastapi.testclient import TestClient
from fastapi.responses import Response
from playwright.sync_api import sync_playwright,expect
import server as s
from test_server import agent_profile, signed,body
O=R/'test-results';errors=[];requests=[]
with tempfile.TemporaryDirectory(prefix='rent-mobile-',ignore_cleanup_errors=True) as tmp:
 s.DATA=Path(tmp);s.DB=s.DATA/'db.sqlite3';s.LIVE=True;s.TOKEN='test-token';s.BOT='test_bot';s.PUBLIC_URL='https://rent.test';s.ADMINS={99};s.CHAT='';s.setup()
 # No TestClient lifespan: the actual Telegram poller and jobs are never started.
 client=TestClient(s.app)
 def create(uid,address,role='owner'):
  payload=body(address=address);payload['listing'].update(phone='+374 (91) 123456',role=role)
  response=client.post('/api/listings',headers=signed(uid),json=payload);assert response.status_code==200,response.text
  return response.json()
 agent_profile(client,44)
 first=create(42,'Мобильная 10');second=create(43,'Мобильная 20');agent=create(44,'Мобильная 30','agent')
 with sync_playwright() as p:
  b=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox'])
  page=b.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
  page.on('pageerror',lambda e:errors.append(str(e)))
  init={'initData':signed()['X-Telegram-Init-Data'],'initDataUnsafe':{},'colorScheme':'light','themeParams':{}}
  page.add_init_script('window.Telegram={WebApp:'+json.dumps(init)+'};Object.assign(window.Telegram.WebApp,{ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}});')
  def route(req):
   url=urlparse(req.request.url);path=url.path+('?' +url.query if url.query else '')
   if url.hostname!='rent.test':return req.fulfill(content_type='application/javascript',body='')
   method=req.request.method;requests.append((method,path))
   headers={k:v for k,v in req.request.headers.items() if k in ('x-telegram-init-data','content-type')}
   response=client.request(method,path,headers=headers,content=req.request.post_data)
   req.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
  page.route('**/*',route);page.goto('https://rent.test/');page.locator('.listing').first.wait_for()
  def actor(uid):
   init_data=signed(uid)['X-Telegram-Init-Data'] if uid else ''
   page.evaluate('async raw=>{tg.initData=raw;state.user=raw?await api("/api/me"):null;await refresh();navigate("feed",false);toast("");}',init_data)
  def open_first():
   page.locator('[data-listing="'+first['id']+'"] .listing-main').click()
   page.locator('.sheet .view-count').wait_for()
  # Phone-only parsing; precise fields still require manual input.
  page.locator('#header [data-action=nav][data-id=add]').click()
  text='С 15 числа до мая, цена 600.000. Телефон +374 (91) 123456.'
  page.evaluate('goStep(3)')
  page.locator('#listing-text').fill(text)
  assert page.evaluate('state.draft.phone')=='+37491123456'
  assert page.evaluate('state.draft.prices[0].amount')==0 and page.evaluate('state.draft.address')==''
  assert page.evaluate('state.draft.available') is None
  assert page.locator('.phone-inferred').is_visible()
  page.locator('[data-action=edit-contact]').click()
  assert page.locator('#phone-contact').input_value()=='+37491123456'
  page.locator('#publisher-role').select_option('agent')
  page.screenshot(animations='disabled',path=str(O/'phone-role-390.png'))
  page.locator('#phone-contact').fill('');page.locator('button[form=contact-form]').click();page.wait_for_timeout(120)
  page.locator('#listing-text').fill(text+' Дополнение.')
  page.evaluate('render()')
  assert page.evaluate('state.draft.phone')=='' and page.evaluate('state.draft.role')=='agent'
  actor(42);open_first()
  page.locator('.trust-box summary').click()
  expect(page.locator('.trust-box')).to_contain_text('Документ можно проверить в e-cadastre по номеру и паролю. Реквизиты запросите у автора.')
  assert not page.locator('[data-action=verify],[data-action=review-doc],#verification-form,#verification-decision').count()
  page.evaluate('tg.openLink=url=>window.officialOpened=url')
  page.locator('.trust-box [data-action=official]').click()
  assert page.evaluate('window.officialOpened')=='https://www.e-cadastre.am/ru/application/docview'
  page.evaluate('(id)=>{const l=item(id);document.querySelector(".trust-box").outerHTML=trustHTML({...l,document_status:"owner_verified",verification:{method:"e-cadastre-manual",checked_on:"2026-09-05"}})}',first['id'])
  page.locator('.trust-box summary').click()
  expect(page.locator('.trust-box')).to_contain_text('Администратор сверил документ через e-cadastre')
  expect(page.locator('.trust-box')).to_contain_text('2026-09-05')
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
  page.locator('#header [data-action=mine]').click();row=page.locator('[data-mine-id="'+first['id']+'"]')
  assert not row.locator('[data-action=verify]').count()
  assert 'Актуально' in row.inner_text();row.locator('[data-action=status]').click()
  page.wait_for_function('document.querySelector(".my-row .listing-status")?.textContent.includes("Сдано")')
  page.locator('.my-row [data-action=status]').click()
  page.wait_for_function('document.querySelector(".my-row .listing-status")?.textContent.includes("Актуально")')
  # Admin bans any existing ad; an empty reason cannot submit.
  actor(99);page.locator('#header [data-action=mine]').click();page.locator('.sheet [data-action=open-admin]').click();page.locator('[data-action=admin-tab][data-id=all]').click()
  page.locator('[data-admin-id="'+first['id']+'"]').wait_for()
  page.evaluate('(id)=>{state.queue.find(l=>l.id===id).document_status="pending";render()}',first['id'])
  assert not page.locator('[data-action=review-doc],#verification-decision').count()
  page.locator('[data-admin-id="'+first['id']+'"] [data-action=ban]').click()
  assert not page.locator('#ban-reason').evaluate('(x)=>x.checkValidity()')
  reason='Скрытая комиссия. Исправьте условия объявления.'
  page.locator('#ban-reason').fill(reason);page.screenshot(animations='disabled',path=str(O/'ban-reason-390.png'))
  page.locator('button[form=ban-form]').click()
  page.locator('[data-admin-id="'+first['id']+'"] .status-banned').wait_for()
  assert client.get('/api/listings/'+first['id']).status_code==404
  actor(42);page.locator('#header [data-action=mine]').click()
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
  actor(99);page.locator('#header [data-action=mine]').click();page.locator('.sheet [data-action=open-admin]').click();page.locator('[data-action=admin-tab][data-id=banned]').click()
  expect(page.locator('[data-admin-id]')).to_have_count(1)
  page.locator('[data-action=unban]').click();page.wait_for_function('!document.querySelector("[data-admin-id]")')
  restored=client.get('/api/listings/'+first['id']).json()
  assert restored['status']=='active' and restored['view_count']==2 and restored['created_at']==first['created_at']
  actor(42);page.locator('[data-listing="'+agent['id']+'"] .listing-main').click()
  assert page.locator('.sheet [data-action=phone-listings]').count()==0
  # A complaint is reviewed before sending; Telegram evidence stays private.
  calls=[]
  async def fake_tg(method,data):calls.append((method,data));return {'message_id':1}
  s.tg=fake_tg
  async def proof_stream(photo,private=False):
   assert private
   return Response(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII='),media_type='image/png')
  s.stream_telegram_photo=proof_stream
  actor(43);open_first();page.locator('[data-action=report]').click();page.locator('#report-form').wait_for()
  assert not page.locator('#report-reason').evaluate('(x)=>x.checkValidity()')
  with s.db() as c:assert c.execute("SELECT count(*) FROM reports WHERE status='pending'").fetchone()[0]==0
  page.locator('#report-reason').fill('Скрытая комиссия')
  page.locator('#report-details').fill('Автор запросил комиссию в личной переписке.')
  page.locator('#report-evidence').fill('https://t.me/example_thread/123')
  page.evaluate('()=>{window.opened=[];tg.openTelegramLink=url=>opened.push(url);}')
  page.locator('[data-action=report-photos]').click()
  page.wait_for_function('opened.length===1')
  rid=page.evaluate('state.report.id')
  assert page.evaluate('opened[0]')=='https://t.me/test_bot?start=proof_'+rid
  from test_reports import incoming,photo
  with ThreadPoolExecutor(max_workers=1) as pool:
   pool.submit(asyncio.run,s.receive(incoming(43,text='/start proof_'+rid))).result()
   pool.submit(asyncio.run,s.receive(incoming(43,photo=photo()))).result()
  assert 'report_'+rid in calls[-1][1]['reply_markup']['inline_keyboard'][0][0]['web_app']['url']
  page.locator('[data-action=refresh-proofs]').click()
  page.locator('.report-proofs img').wait_for()
  assert page.locator('#report-details').input_value()=='Автор запросил комиссию в личной переписке.'
  for width in [320,390,430]:
   page.set_viewport_size({'width':width,'height':844})
   assert page.locator('.sheet-body').evaluate('(x)=>x.scrollWidth<=x.clientWidth')
   page.screenshot(animations='disabled',path=str(O/f'report-form-{width}.png'))
  page.locator('button[form=report-form]').click()
  page.wait_for_function('document.querySelector(".report-content")?.textContent.includes("Ожидает решения")')
  assert page.locator('#report-decision').count()==0
  assert client.get('/api/admin/reports',headers=signed(99)).json()[0]['reporter']['id']==43
  # Launch the exact target carried by the private notification button.
  actor(99)
  page.evaluate('async rid=>{history.replaceState({},"","?start=report_"+rid);startHandled=false;await startRoute();}',rid)
  page.locator('#report-decision').wait_for()
  assert 'ID 43' in page.locator('.report-content').inner_text()
  assert page.locator('.report-proofs img').count()==1
  assert page.locator('.report-content a[href="https://t.me/example_thread/123"]').count()==1
  page.locator('[data-action=report-listing]').click();page.locator('.detail-sheet').wait_for()
  page.locator('[data-action=close]').click()
  page.locator('#header [data-action=mine]').click();page.locator('.sheet [data-action=open-admin]').click();page.locator('[data-action=admin-tab][data-id=reports]').click()
  page.locator('[data-action=open-report]').click();page.locator('#report-decision').wait_for()
  assert not page.locator('#report-outcome').evaluate('(x)=>x.checkValidity()')
  page.locator('#report-outcome').select_option('ban')
  page.locator('#report-resolution').fill('Скрытая комиссия подтверждена перепиской.')
  page.screenshot(animations='disabled',path=str(O/'report-admin-430.png'))
  page.locator('button[form=report-decision]').click()
  page.wait_for_function('document.querySelector(".report-row")?.textContent.includes("Объявление заблокировано")')
  assert client.get('/api/listings/'+first['id']).status_code==404
  actor(42);page.locator('#header [data-action=mine]').click()
  assert 'Скрытая комиссия подтверждена перепиской.' in page.locator('[data-mine-id="'+first['id']+'"]').inner_text()
  assert not any(path.endswith('/verification') or path.endswith('/private') for method,path in requests)
  assert not errors,errors
  report={'result':'passed','mobile_widths':[320,360,390,430],'js_errors':errors,'scenarios':['phone mask only, manual clear persists','explicit realtor role','same phone without realtor','unique views across authenticated users and anonymous display','My listings active and rented','admin required ban reason','owner sees ban and cannot restore','admin unban retains date and views','complaint reason and evidence handoff','private screenshots and direct report launch','admin complaint resolution and owner ban reason'],'telegram_calls':0,'scope':'Chromium UI with intercepted requests to real FastAPI TestClient and temporary SQLite; no live data or Telegram network'}
  (O/'mobile-moderation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
  b.close()
