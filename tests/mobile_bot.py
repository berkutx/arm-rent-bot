"""Bot Mini App routes and nonblocking, deduplicated funnel events on mobile."""
import json,os,sys,tempfile
from pathlib import Path
from urllib.parse import urlparse
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import server as s
from test_server import signed,create
OUT=ROOT/'test-results';OUT.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='mobile-bot-',ignore_cleanup_errors=True) as tmp:
 s.DATA=Path(tmp);s.DB=s.DATA/'rent.sqlite3';s.LIVE=True;s.TOKEN='test-token';s.BOT='test_bot';s.ADMINS={99};s.CHAT=s.PAID_CHAT='';s.setup()
 client=TestClient(s.app);listing=create(client,address='Комитаса 18')
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
  events=[];errors=[];held=[];control={'hold_mine':True,'hold_feed':True,'fail_activity':False,'fail_mine':False}
  page.on('pageerror',lambda error:errors.append(str(error)))
  init={'initData':signed()['X-Telegram-Init-Data'],'initDataUnsafe':{},'colorScheme':'light','themeParams':{}}
  page.add_init_script('window.Telegram={WebApp:'+json.dumps(init)+'};Object.assign(Telegram.WebApp,{ready(){},expand(){},openTelegramLink(url){window.openedTelegram=url},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}});window.EventSource=class{close(){}};window.activityCompleted=0;const activityFetch=window.fetch.bind(window);window.fetch=(input,options)=>activityFetch(input,options).then(response=>{if(input==="/api/activity")window.activityCompleted++;return response;});')
  def fulfill(route):
   url=urlparse(route.request.url);path=url.path+('?' +url.query if url.query else '')
   headers={k:v for k,v in route.request.headers.items() if k in ('x-telegram-init-data','content-type')}
   response=client.request(route.request.method,path,headers=headers,content=route.request.post_data_buffer)
   route.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
  def serve(route):
   url=urlparse(route.request.url)
   if url.hostname!='rent.test':return route.fulfill(content_type='application/javascript',body='')
   if url.path=='/api/activity':
    assert route.request.method=='POST'
    assert route.request.headers.get('x-telegram-init-data')
    events.append(route.request.post_data_json['stage'])
    return route.fulfill(status=503 if control['fail_activity'] else 200,content_type='application/json',body='{"ok":true}')
   if (url.path=='/api/mine' and control['hold_mine']) or (url.path=='/api/feed' and control['hold_feed']):held.append(route);return
   if url.path=='/api/mine' and control['fail_mine']:
    control['fail_mine']=False;return route.fulfill(status=503,content_type='application/json',body='{"detail":"Повторите загрузку"}')
   fulfill(route)
  def wait_events(expected):
   page.wait_for_function('(count)=>activityCompleted===count',arg=len(expected))
   assert events==expected,events
  page.route('**/*',serve)
  page.goto('https://rent.test/?start=mine',wait_until='domcontentloaded')
  page.wait_for_function('state.user && !state.personalReady')
  expect(page.locator('.listing')).to_have_count(0)
  assert not page.locator('.sheet').count()
  assert events==[]
  control['hold_mine']=False
  mine_routes=[route for route in held if urlparse(route.request.url).path=='/api/mine']
  for route in mine_routes:held.remove(route);fulfill(route)
  page.wait_for_function('sheetKind==="mine"')
  assert page.evaluate('state.booted') is False
  expect(page.locator('[data-mine-id="'+listing['id']+'"]')).to_contain_text('Комитаса 18')
  control['hold_feed']=False
  for route in held:fulfill(route)
  held.clear();page.wait_for_function('state.booted')
  assert not page.locator('.sheet [data-action=open-admin]').count()
  page.screenshot(path=str(OUT/'bot-mine-390.png'),animations='disabled')
  page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind')
  assert events==[]
  control['fail_mine']=True
  page.goto('https://rent.test/?start=mine');page.wait_for_function('state.booted')
  assert not page.locator('.sheet').count() and page.evaluate('startHandled') is False
  page.evaluate('refreshVisible()');page.wait_for_function('sheetKind==="mine"')
  expect(page.locator('[data-mine-id="'+listing['id']+'"]')).to_be_visible()
  page.goto('https://rent.test/');page.wait_for_function('state.booted')
  assert events==[]
  page.locator('.listing [data-action=detail]').first.click();page.wait_for_function('sheetKind==="detail"')
  assert events==[]
  page.locator('.sheet-footer [data-action=contact]').click();page.wait_for_function('sheetKind==="contact"')
  page.wait_for_function('activitySeen.has("contact")')
  wait_events(['contact'])
  page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind')
  page.locator('.listing [data-action=contact]').click();page.wait_for_function('sheetKind==="contact"')
  wait_events(['contact'])
  events.clear();control['fail_activity']=True
  page.goto('https://rent.test/?start=add');page.wait_for_function('state.booted && state.screen==="add"')
  expect(page.locator('h1')).to_have_text('Что сдаёте?')
  page.wait_for_function('activitySeen.has("housing")');wait_events(['housing'])
  page.evaluate('render();render()')
  page.locator('[data-action=choose-kind][data-id=house]').click()
  page.locator('#continue').click();expect(page.locator('h1')).to_have_text('Где находится жильё?')
  page.locator('#address-input').fill('Таирова 12')
  page.locator('#continue').click();expect(page.locator('h1')).to_have_text('Сколько стоит аренда?')
  page.locator('#budget-input').fill('300000')
  page.go_back();page.wait_for_function('state.formStep===1')
  page.locator('#continue').click();expect(page.locator('#budget-input')).to_have_value('300000')
  page.locator('#continue').click();expect(page.locator('h1')).to_have_text('Фото и описание')
  page.wait_for_function('activitySeen.has("photos")')
  wait_events(['housing','address','price','photos'])
  expect(page.locator('#publish')).to_be_enabled()
  assert page.locator('#toast').inner_text()==''
  page.locator('#listing-text').fill('Есть мебель и техника.')
  page.locator('[data-action=upload]').click();page.wait_for_function('window.openedTelegram')
  assert page.evaluate('window.openedTelegram')=='https://t.me/test_bot?start=photos'
  events.clear();control['fail_activity']=False
  page.goto('https://rent.test/?start=draft');page.wait_for_function('state.booted && state.screen==="review"')
  expect(page.locator('#listing-text')).to_have_value('Есть мебель и техника.')
  page.wait_for_function('activitySeen.has("photos")');wait_events(['photos'])
  page.locator('#header [data-action=back]').click();page.wait_for_function('state.formStep===2')
  page.wait_for_function('activitySeen.has("price")');wait_events(['photos','price'])
  with page.expect_response(lambda response:urlparse(response.url).path=='/api/activity'):
   page.evaluate('window.originalNow=Date.now;Date.now=()=>originalNow()+3600000;render()')
  page.wait_for_function('activityWindow.endsWith(String(Math.floor(Date.now()/3600000)))')
  wait_events(['photos','price','price'])
  fresh=signed(43)['X-Telegram-Init-Data']
  with page.expect_response(lambda response:urlparse(response.url).path=='/api/activity'):
   page.evaluate('(auth)=>{tg.initData=auth;state.user={...state.user,id:43};render()}',fresh)
  page.wait_for_function('activityWindow.startsWith("43:")')
  wait_events(['photos','price','price','price'])
  count=len(events)
  page.evaluate('state.live=false;Date.now=()=>originalNow()+7200000;render()')
  page.locator('#continue').click();expect(page.locator('h1')).to_have_text('Фото и описание')
  assert len(events)==count
  for width in (320,390,430):
   page.set_viewport_size({'width':width,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==width
  events.clear();page.goto('https://rent.test/');page.wait_for_function('state.booted')
  page.evaluate('window.originalNow=Date.now;Date.now=()=>originalNow()+3600000;render()')
  assert events==[]
  page.mouse.wheel(0,100);wait_events(['app'])
  page.mouse.wheel(0,100);page.locator('#header [data-action=mine]').click();page.wait_for_function('sheetKind==="mine"')
  wait_events(['app'])
  page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind')
  page.evaluate('Date.now=()=>originalNow()+7200000;Object.defineProperty(document,"hidden",{configurable:true,value:true});')
  page.mouse.wheel(0,100);assert events==['app']
  page.evaluate('Object.defineProperty(document,"hidden",{configurable:true,value:false});document.dispatchEvent(new Event("visibilitychange"));')
  wait_events(['app','app'])
  page.evaluate('Date.now=()=>originalNow()+10800000;state.user.is_admin=true;')
  page.mouse.wheel(0,100);assert events==['app','app']
  assert not errors,errors
  report={'result':'passed','mine_waits_for_identity_and_personal_data':True,'mine_opens_before_delayed_feed':True,'mine_retries_after_load_failure':True,'funnel_steps_observed_only':True,'telemetry_failures_do_not_block':True,'hourly_and_user_deduplication':True,'active_return_counted_idle_hidden_admin_skipped':True,'js_errors':errors}
  (OUT/'mobile-bot.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));browser.close()
