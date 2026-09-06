import base64,json,os,sys,tempfile
from pathlib import Path
from urllib.parse import urlparse
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
import server as s
from test_server import signed,create
O=R/'test-results';O.mkdir(exist_ok=True);errors=[];requests=[]
with tempfile.TemporaryDirectory(prefix='map-mobile-',ignore_cleanup_errors=True) as tmp:
 s.DATA=Path(tmp);s.DB=s.DATA/'rent.sqlite3';s.LIVE=True;s.TOKEN='test-token';s.BOT='test_bot';s.ADMINS={99};s.CHAT=s.PAID_CHAT='';s.GEOCODER_URL='https://geocoder.test';s.setup()
 client=TestClient(s.app);ad=create(client,address='Улица 12');out=create(client,address='Другая 12')
 with s.db() as c:
  d=json.loads(s.getrow(out['id'])['payload']);d['city']='Севан';c.execute('UPDATE listings SET payload=? WHERE id=?',(s.dumps(d),out['id']))
 points=[{'lat':40.19,'lon':44.51,'label':'Улица 12, Ереван','precision':'building'}]
 async def geocode(address):return points
 s.geocode_address=geocode
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox'])
  page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
  page.on('pageerror',lambda e:errors.append(str(e)))
  init={'initData':signed()['X-Telegram-Init-Data'],'colorScheme':'light','themeParams':{}}
  page.add_init_script('window.Telegram={WebApp:'+json.dumps(init)+'};Object.assign(Telegram.WebApp,{ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}});')
  def route(req):
   url=urlparse(req.request.url);requests.append(req.request.url)
   if url.hostname=='tile.openstreetmap.org':return req.fulfill(content_type='image/png',body=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=='))
   if url.hostname!='rent.test':return req.fulfill(content_type='application/javascript',body='')
   headers={k:v for k,v in req.request.headers.items() if k in ('x-telegram-init-data','content-type')}
   response=client.request(req.request.method,url.path,headers=headers,content=req.request.post_data)
   req.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
  page.route('**/*',route);page.goto('https://rent.test/');page.locator('.listing').wait_for()
  assert not any('/map' in x or '/assets/' in x or 'tile.openstreetmap.org' in x for x in requests)
  page.evaluate('state.filters.city="";render()');assert page.locator('[data-listing="'+out['id']+'"] [data-action=map]').count()==0
  page.locator('[data-listing="'+ad['id']+'"] [data-action=map]').click();page.locator('.leaflet-container').wait_for()
  expect(page.locator('#map-status')).to_contain_text('Примерное расположение')
  expect(page.locator('.leaflet-control-attribution')).to_contain_text('OpenStreetMap')
  assert page.locator('.leaflet-overlay-pane svg').evaluate('(x)=>x.getBoundingClientRect().width>200')
  zoom=page.evaluate('rentalMap.getZoom()');page.locator('.leaflet-control-zoom-in').click();page.wait_for_timeout(350);assert page.evaluate('rentalMap.getZoom()')==zoom+1
  for width in [320,390,430]:
   page.set_viewport_size({'width':width,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==width
   page.screenshot(path=str(O/f'map-{width}.png'),animations='disabled')
  page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind');assert page.evaluate('rentalMap===null')
  with s.db() as c:c.execute('DELETE FROM listing_locations')
  points.append({'lat':40.18,'lon':44.52,'label':'Переулок','precision':'building'})
  page.locator('[data-listing="'+ad['id']+'"] [data-action=map]').click();page.locator('.leaflet-container').wait_for()
  expect(page.locator('#map-status')).to_contain_text('Примерное расположение')
  assert not page.locator('[data-action=map-point],[data-action=save-map]').count()
  page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind')
  with s.db() as c:c.execute('DELETE FROM listing_locations')
  points[:]=[{'lat':40.2,'lon':44.51,'label':'Улица','precision':'street'}]
  page.locator('[data-listing="'+ad['id']+'"] [data-action=map]').click();expect(page.locator('#map-status')).to_contain_text('расположение улицы')
  page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind')
  points.clear()
  with s.db() as c:c.execute('DELETE FROM listing_locations')
  page.locator('[data-listing="'+ad['id']+'"] [data-action=map]').click();expect(page.locator('#map-status')).to_contain_text('Адрес не найден')
  assert not errors,errors
  print(json.dumps({'result':'passed','external_requests':0,'scenarios':['lazy assets and geocoding','Yerevan only','zoom and cleanup','stored approximate point without selection','street precision','no result'],'js_errors':errors}))
  browser.close()
