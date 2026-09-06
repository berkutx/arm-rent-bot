"""Mobile import review and push refresh against an isolated database."""
import json,os,re,sys,tempfile,time
from pathlib import Path
from urllib.parse import urlparse
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
import server as s
from test_server import signed
from test_source_sync import message,fields
O=R/'test-results';O.mkdir(exist_ok=True);errors=[]
with tempfile.TemporaryDirectory(prefix='source-mobile-',ignore_cleanup_errors=True) as tmp:
 s.DATA=Path(tmp);s.DB=s.DATA/'rent.sqlite3';s.LIVE=True;s.TOKEN='test-token';s.BOT='test_bot';s.PUBLIC_URL='https://rent.test';s.ADMINS={99};s.CHAT='';s.PAID_CHAT='';s.setup()
 os.environ['TELEGRAM_SYNC_ENABLED']='1'
 store=s.SOURCE_STORE;catalog=s.SOURCE_CATALOG;store.begin(-1000000000123,{55:'free'});store.set('source_username','source_fixture');store.set('activated',True);store.set('history_complete',True);store.set('connection',{'state':'connected'});store.set('history_checked',time.time())
 original=message();store.ingest(original);lid=catalog.apply(store.snapshot('-1000000000123:m1'),fields())
 pending=message(2,text='Квартира в Ереване, Улица 20, 2 комнаты, 350000 AMD в месяц, комиссия 50%.',market='paid');store.ingest(pending);catalog.hold(store.snapshot('-1000000000123:m2'))
 client=TestClient(s.app)
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
  page.on('pageerror',lambda error:errors.append(str(error)))
  init={'initData':signed(99)['X-Telegram-Init-Data'],'initDataUnsafe':{},'colorScheme':'light','themeParams':{}}
  page.add_init_script('window.Telegram={WebApp:'+json.dumps(init)+'};Object.assign(Telegram.WebApp,{ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}});window.streams=[];window.EventSource=class{constructor(url){this.url=url;streams.push(this);}close(){}};')
  def route(req):
   url=urlparse(req.request.url);path=url.path+('?' +url.query if url.query else '')
   if url.hostname!='rent.test':return req.fulfill(content_type='application/javascript',body='')
   headers={k:v for k,v in req.request.headers.items() if k in ('x-telegram-init-data','content-type')}
   response=client.request(req.request.method,path,headers=headers,content=req.request.post_data)
   req.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
  page.route('**/*',route);page.goto('https://rent.test/');page.locator('.listing').first.wait_for()
  page.evaluate('streams[0].onmessage({data:JSON.stringify({revision:"initial"})})')
  page.locator('#header [data-action=mine]').click();page.locator('.sheet [data-action=open-admin]').click();page.locator('[data-action=admin-tab][data-id=import]').click()
  page.locator('[data-action=source-post]').click();page.locator('#source-form').wait_for()
  assert not page.locator('#source-city').evaluate('(x)=>x.checkValidity()')
  page.locator('#source-city').fill('Ереван');page.locator('#source-address').fill('Улица 20');page.locator('#source-district').select_option('Кентрон');page.locator('#source-kind').select_option('apartment');page.locator('#source-rooms').fill('2');page.locator('#source-price').fill('350000');page.locator('select[name=currency]').select_option('AMD');page.locator('select[name=period]').select_option('month');page.locator('#source-fee').fill('50')
  page.screenshot(animations='disabled',path=str(O/'source-review-390.png'))
  page.locator('button[form=source-form]').click();page.wait_for_function('!document.querySelector("#source-form")')
  assert len(client.get('/api/listings').json())==2
  page.evaluate('state.filters.district="Нор-Норк";navigate("feed",false)')
  page.locator('[data-action=market][data-id=paid]').click()
  expect(page.locator('[data-action=market][data-id=paid] .market-count')).to_have_text('1')
  expect(page.locator('.empty')).to_contain_text('Нор-Норк')
  expect(page.locator('#header [data-action=district]')).to_have_class(re.compile(r'\bselected\b'))
  for width in [320,390]:
   page.set_viewport_size({'width':width,'height':844})
   assert page.evaluate('document.documentElement.scrollWidth')==width
   page.screenshot(path=str(O/f'paid-filter-{width}.png'),animations='disabled')
  page.locator('[data-action=reset-filters]').click()
  expect(page.locator('.listing')).to_have_count(1)
  assert page.evaluate('state.filters.market')=='paid' and page.evaluate('state.filters.district')==''
  page.reload();page.locator('.listing').wait_for()
  page.evaluate('streams[0].onmessage({data:JSON.stringify({revision:"after-reload"})})')
  assert page.evaluate('state.filters.market')=='paid' and page.locator('.listing').count()==1
  page.locator('[data-action=market][data-id=free]').click()
  page.locator('[data-listing="'+lid+'"] .listing-main').click();page.locator('.detail-sheet').wait_for()
  changed={**original,'text':'Исправленный адрес и цена','edited':time.time()};store.ingest(changed);catalog.apply(store.snapshot('-1000000000123:m1'),fields(250000,'Улица 11'))
  page.evaluate('streams[0].onmessage({data:JSON.stringify({revision:"changed"})})')
  page.wait_for_function('document.querySelector(".sheet .meta")?.textContent.includes("Улица 11")')
  page.locator('.listing-history summary').click()
  assert '300' in page.locator('.listing-history').inner_text() and '250' in page.locator('.listing-history').inner_text()
  for width in [320,390,430]:
   page.set_viewport_size({'width':width,'height':844})
   assert page.locator('.sheet-body').evaluate('(x)=>x.scrollWidth<=x.clientWidth')
   page.screenshot(animations='disabled',path=str(O/f'source-history-{width}.png'))
  store.delete(original['channel'],[original['id']]);catalog.hold(store.snapshot('-1000000000123:m1'),'deleted')
  page.evaluate('streams[0].onmessage({data:JSON.stringify({revision:"deleted"})})')
  page.wait_for_function('!document.querySelector(".detail-sheet")')
  assert page.locator('[data-listing="'+lid+'"]').count()==0
  page.evaluate('async raw=>{tg.initData=raw;state.user=await api("/api/me");await refresh();navigate("feed",false);}',signed(42)['X-Telegram-Init-Data'])
  page.locator('#header [data-action=mine]').click()
  assert 'Удалено в Telegram' in page.locator('[data-mine-id="'+lid+'"]').inner_text()
  assert not errors,errors
  result={'result':'passed','scenarios':['admin reviews imported paid offer','price and address history','push refresh of open card','deleted post removed without reload'],'js_errors':errors,'telegram_network_calls':0}
  (O/'mobile-source.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');print(json.dumps(result,ensure_ascii=False));browser.close()
