"""Paged mobile feed: first paint, delayed responses, retries and deletion refresh."""
import base64,json,os,sys,tempfile
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
import server as s
from test_server import signed
from test_feed import insert_rows
OUT=R/'test-results';OUT.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='mobile-feed-',ignore_cleanup_errors=True) as tmp:
 s.DATA=Path(tmp);s.DB=s.DATA/'rent.sqlite3';s.LIVE=True;s.TOKEN='test-token';s.BOT='test_bot';s.ADMINS={99};s.CHAT=s.PAID_CHAT='';s.setup()
 os.environ['TELEGRAM_SYNC_ENABLED']='0';os.environ['TELEGRAM_DELETION_CHECK_INTERVAL']='900'
 specs=[]
 for i in range(68):
  specs.append({'id':f'page{i:03d}','rooms':2,'district':'Кентрон' if i%2 else 'Арабкир','commission':0 if i<60 else 50,'role':'owner' if i<60 else 'agent','contact':'@author_fixture','description':'Полное описание '+('жильё '*100),'photos':[{'id':f'{i*10+j:032x}','url':f'/media/{i*10+j:032x}.jpg','thumb_url':f'/media/{i*10+j:032x}-thumb.jpg','width':1200,'height':800} for j in range(5)]})
 insert_rows(specs);client=TestClient(s.app)
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True);errors=[];requests=[];held=[];control={'hold_user':True,'hold_feed':False,'fail_feed':False}
  page.on('pageerror',lambda error:errors.append(str(error)))
  init={'initData':signed(99)['X-Telegram-Init-Data'],'initDataUnsafe':{},'colorScheme':'light','themeParams':{}}
  page.add_init_script('window.streams=[];window.EventSource=class{constructor(){streams.push(this)}close(){}};window.Telegram={WebApp:'+json.dumps(init)+'};Object.assign(Telegram.WebApp,{ready(){},expand(){},openTelegramLink(url){window.openedTelegram=url},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}});')
  pixel=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5ioAAAAASUVORK5CYII=')
  def serve(route):
   url=urlparse(route.request.url);path=url.path+('?' +url.query if url.query else '');requests.append(path)
   if url.hostname!='rent.test':return route.fulfill(content_type='application/javascript',body='')
   if url.path.startswith('/media/'):return route.fulfill(content_type='image/png',body=pixel)
   if (url.path=='/api/me' and control['hold_user']) or (url.path=='/api/feed' and control['hold_feed']):
    held.append(route);return
   if url.path=='/api/feed' and control['fail_feed']:
    control['fail_feed']=False;return route.fulfill(status=503,content_type='application/json',body='{"detail":"Повторите загрузку"}')
   fulfill(route)
  def fulfill(route):
   url=urlparse(route.request.url);path=url.path+('?' +url.query if url.query else '')
   headers={k:v for k,v in route.request.headers.items() if k in ('x-telegram-init-data','content-type')}
   response=client.request(route.request.method,path,headers=headers,content=route.request.post_data_buffer)
   route.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
  page.route('**/*',serve);page.goto('https://rent.test/',wait_until='domcontentloaded')
  expect(page.locator('.listing')).to_have_count(12)
  assert page.evaluate('state.user') is None
  page.wait_for_function('state.catalogEventsEnabled&&streams.length>0');assert page.evaluate('state.sourceEnabled') is False
  assert len([r for r in requests if r.startswith('/api/feed?')])==1 and '/api/listings' not in requests
  expect(page.locator('[data-id=free] .market-count')).to_have_text('60')
  expect(page.locator('[data-id=paid] .market-count')).to_have_text('8')
  expect(page.locator('.listing').first.locator('.album-count').first).to_contain_text('5 фото')
  assert page.evaluate('state.remote.every(x=>!x.description&&x.photos.length===3)')
  control['hold_user']=False;fulfill(held.pop());page.wait_for_function('state.booted&&state.user')
  page.evaluate('window.firstCard=document.querySelector(".listing")')
  page.locator('#feed-more').scroll_into_view_if_needed();expect(page.locator('.listing')).to_have_count(24)
  assert page.evaluate('window.firstCard===document.querySelector(".listing")')
  control['fail_feed']=True;page.locator('#feed-more').scroll_into_view_if_needed()
  expect(page.locator('#feed-more')).to_contain_text('Повторите загрузку');expect(page.locator('.listing')).to_have_count(24)
  page.locator('[data-action=more-feed]').click();expect(page.locator('.listing')).to_have_count(36)
  # A slow old filter must not overwrite a newer section or its full-catalog facets.
  control['hold_feed']=True;page.evaluate('state.filters.rooms="3";render()');page.wait_for_function('feedState.loading')
  page.locator('[data-action=district]').click();expect(page.locator('.district-count').first).to_have_text('…')
  control['hold_feed']=False
  for route in held:fulfill(route)
  held.clear();expect(page.locator('.district-count').first).to_have_text('0')
  page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind')
  control['hold_feed']=True;page.evaluate('state.filters.rooms="2";render()');page.wait_for_function('feedState.loading')
  page.locator('[data-id=paid][data-action=market]').click();control['hold_feed']=False
  page.evaluate('state.filters.rooms="";render()');expect(page.locator('.listing')).to_have_count(8)
  for route in held:fulfill(route)
  held.clear();expect(page.locator('.listing')).to_have_count(8)
  assert page.evaluate('filtered().every(x=>x.commission>0)')
  # Open contact stays usable after the compact feed refreshes.
  page.locator('.listing').first.locator('[data-action=contact]').click()
  page.evaluate('async()=>{await refreshVisible()}')
  page.locator('[data-action=telegram-contact]').click();assert page.evaluate('window.openedTelegram')=='https://t.me/author_fixture'
  page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind')
  page.locator('[data-id=free][data-action=market]').click();expect(page.locator('.listing')).to_have_count(12)
  removed=page.locator('.listing').first.get_attribute('data-listing')
  with s.db() as c:c.execute("UPDATE listings SET status='source_deleted' WHERE id=?",(removed,))
  page.locator('#feed-more').scroll_into_view_if_needed()
  page.wait_for_function('(id)=>feedState.ready&&!feedState.loading&&feedState.total===59&&!feedState.ids.includes(id)',arg=removed)
  assert page.evaluate('new Set(feedState.ids).size===feedState.ids.length')
  for width in [320,390,430]:
   page.set_viewport_size({'width':width,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==width
  page.evaluate('window.scrollTo(0,0)');page.screenshot(path=str(OUT/'paged-feed-430.png'),animations='disabled')
  assert not errors,errors
  report={'result':'passed','initial_cards':12,'catalog_cards':68,'first_paint_before_identity':True,'append_preserves_nodes':True,'retry_preserves_cards':True,'stale_response_ignored':True,'deleted_cursor_refreshed':True,'js_errors':errors}
  (OUT/'mobile-feed.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));browser.close()
