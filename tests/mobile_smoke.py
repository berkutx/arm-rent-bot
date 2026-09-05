"""Production UI: empty catalog, Telegram photo handoff, manual fields and discussion-only contact."""
import asyncio,io,json,os,sys,tempfile
import pytest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from PIL import Image
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import server as s
from test_commission_photos import sizes,mock_telegram_files
from test_server import signed
OUT=ROOT/'test-results';OUT.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='rent-form-',ignore_cleanup_errors=True) as tmp:
 s.DATA=Path(tmp);s.DB=s.DATA/'test.sqlite3';s.LIVE=True;s.TOKEN='test-token';s.BOT='test_bot';s.ADMINS={99};s.CHAT=s.PAID_CHAT='';s.setup()
 client=TestClient(s.app);errors=[];requests=[]
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
  page.on('pageerror',lambda error:errors.append(str(error)))
  init={'initData':signed(username='')['X-Telegram-Init-Data'],'initDataUnsafe':{},'colorScheme':'light','themeParams':{}}
  page.add_init_script('window.Telegram={WebApp:'+json.dumps(init)+'};Object.assign(window.Telegram.WebApp,{ready(){},expand(){},openTelegramLink(url){window.openedTelegram=url;},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}});')
  def route(req):
   url=urlparse(req.request.url)
   if url.hostname!='rent.test':return req.fulfill(content_type='application/javascript',body='')
   path=url.path+('?' +url.query if url.query else '')
   headers={k:v for k,v in req.request.headers.items() if k in ('x-telegram-init-data','content-type')}
   response=client.request(req.request.method,path,headers=headers,content=req.request.post_data_buffer)
   requests.append((req.request.method,path))
   req.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
  page.context.route('**/*',route);page.goto('https://rent.test/');page.wait_for_function('state.booted && state.user')
  assert 'Пока нет объявлений' in page.locator('#main').inner_text()
  assert not page.locator('[data-action=show-examples],.demo-label,.catalog-switch').count()
  page.locator('[data-action=conditions-filter]').click();assert page.locator('#filter-conditions input').count()==3
  page.locator('button[form=filter-conditions]').click();page.wait_for_function('!sheetKind')
  page.locator('#dock [data-action=nav][data-id=add]').click()
  description='С 15 числа до мая, 600.000 + ком услуги, дом на Таирова. +374 (91) 123456'
  page.locator('#listing-text').fill(description)
  assert not page.locator('input[type=file]').count()
  page.locator('#continue').click()
  assert page.evaluate('state.draft.address')=='' and page.evaluate('state.draft.available') is None
  assert page.evaluate('state.draft.phone')=='+37491123456'
  page.locator('[data-action=edit-price]').click();page.locator('#budget-input').fill('600000');page.locator('button[form=edit-price-form]').click();page.wait_for_function('!sheetKind')
  page.locator('[data-action=edit-address]').click();page.locator('#address-input').fill('Таирова')
  page.locator('#city-input').select_option('Дилижан');assert not page.locator('#district-row').is_visible();assert page.locator('#metro-minutes').is_disabled()
  page.locator('.travel-fields summary').click();page.locator('#center-minutes').fill('15')
  page.locator('button[form=address-form]').click();page.wait_for_function('!sheetKind')
  page.locator('[data-action=edit-rooms]').click();page.locator('[data-action=set-rooms][data-id=house]').click();page.wait_for_function('!sheetKind')
  page.locator('#available-from').fill('2026-09-15');page.locator('#available-until').fill('2026-09-01');assert page.locator('#availability-error').is_visible()
  page.locator('#available-until').fill('2027-05-01');assert not page.locator('#availability-error').is_visible()
  page.locator('[data-action=edit-contact]').click();assert 'Без username' in page.locator('.sheet').inner_text();assert not page.locator('#tg-contact,#contact-mode').count()
  page.locator('button[form=contact-form]').click();page.wait_for_function('!sheetKind')
  page.locator('[data-action=edit-text]').click();page.locator('[data-action=upload]').click()
  page.wait_for_function('window.openedTelegram')
  assert page.evaluate('window.openedTelegram')=='https://t.me/test_bot?start=photos'
  assert client.get('/api/draft',headers=signed(username='')).json()['text']==description
  async def telegram(*args):return {}
  file_lookup=s.tg;s.tg=telegram
  with ThreadPoolExecutor(max_workers=1) as executor:
   for i in range(1):executor.submit(asyncio.run,s.receive({'message':{'chat':{'type':'private'},'from':{'id':42},'photo':sizes(str(i))}})).result()
  raw=io.BytesIO();Image.new('RGB',(800,600),'#4d857b').save(raw,'JPEG')
  patch=pytest.MonkeyPatch();mock_telegram_files(patch,{prefix+name:raw.getvalue() for prefix in ['0','1'] for name in ['preview','full']})
  page.goto('https://rent.test/?start=draft');page.wait_for_function('state.booted && state.screen==="review" && state.photos.length===1')
  assert page.evaluate('state.text')==description
  assert page.evaluate('state.draft.address')=='Таирова' and page.evaluate('state.draft.available_until')=='2027-05-01'
  file_lookup=s.tg;s.tg=telegram
  with ThreadPoolExecutor(max_workers=1) as executor:
   executor.submit(asyncio.run,s.receive({'message':{'chat':{'type':'private'},'from':{'id':42},'photo':sizes('1')}})).result()
  s.tg=file_lookup
  page.evaluate('refreshVisible()');assert page.evaluate('state.photos.length')==2
  page.locator('[data-action=remove-photo][data-id="1"]').click();page.evaluate('refreshVisible()')
  assert page.evaluate('state.photos.length')==1
  assert page.evaluate('state.draft.available_until')=='2027-05-01'
  assert not any(method=='POST' and path=='/api/photos' for method,path in requests)
  for width in [320,360,390,430]:
   page.set_viewport_size({'width':width,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==width
  page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(OUT/'manual-production-390.png'))
  page.locator('[data-action=publish]').click();page.wait_for_function('state.screen==="feed" && !state.busy')
  row=client.get('/api/listings').json()[0]
  assert row['description']==description and row['contact']=='' and row['phone']=='+37491123456'
  assert row['available_until']=='2027-05-01' and row['center_drive_minutes']==15
  page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind')
  page.locator('.contact-button').click();expect(page.locator('.sheet [data-action=discussion]')).to_be_disabled()
  assert not page.locator('[data-action=relay-contact]').count()
  assert 'Обсуждение публикации ещё не подключено.' in page.locator('.sheet').inner_text()
  page.wait_for_timeout(300);page.screenshot(path=str(OUT/'discussion-contact-390.png'))
  assert not errors,errors
  print(json.dumps({'result':'passed','telegram_album_photos':2,'form_restored':True,'image_uploads_through_server':0,'js_errors':errors,'manual_dates_and_city':True,'no_username_submission':True,'contact':'discussion only'},ensure_ascii=False))
  browser.close()
