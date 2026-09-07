import json,os,sys,tempfile
from pathlib import Path
from urllib.parse import urlparse
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright,expect
import server as s
from test_server import signed,create
O=R/'test-results';O.mkdir(exist_ok=True);errors=[];requests=[];style_failure=False
features=[{'type':'Feature','geometry':{'type':'Point','coordinates':xy},'properties':props} for xy,props in [
 ([44.5098,40.19065],{'name':'Տեղական','name:ru':'Русское название','name:latin':'English name','ref':'H1','housenumber':'12'}),
 ([44.5103,40.18935],{'name':'Տեղական','name:latin':'Latin fallback','ref':'H2','housenumber':'14'}),
 ([44.5096,40.19005],{'name':'Տեղական','name:en':'English fallback','ref':'H3','housenumber':'16'}),
]]
style={'version':8,'sources':{'places':{'type':'geojson','attribution':'© OpenMapTiles · © OpenStreetMap','data':{'type':'FeatureCollection','features':features}}},'layers':[
 {'id':'background','type':'background','paint':{'background-color':'#e8efe6'}},
 {'id':'labels','type':'symbol','source':'places','layout':{'text-font':['Arial'],'text-field':['case',['has','name:nonlatin'],['concat',['get','name:latin'],'\n',['get','name:nonlatin']],['coalesce',['get','name_en'],['get','name']]],'text-size':14,'text-allow-overlap':True},'paint':{'text-color':'#253b32'}},
 {'id':'refs','type':'symbol','source':'places','layout':{'visibility':'none','text-font':['Arial'],'text-field':['get','ref']}},
 {'id':'houses','type':'symbol','source':'places','layout':{'visibility':'none','text-font':['Arial'],'text-field':'{housenumber}'}},
]}
with tempfile.TemporaryDirectory(prefix='map-mobile-',ignore_cleanup_errors=True) as tmp:
 s.DATA=Path(tmp);s.DB=s.DATA/'rent.sqlite3';s.LIVE=True;s.TOKEN='test-token';s.BOT='test_bot';s.ADMINS={99};s.CHAT=s.PAID_CHAT='';s.GEOCODER_URL='https://geocoder.test';s.setup()
 client=TestClient(s.app);ad=create(client,address='Улица 12');out=create(client,address='Другая 12')
 with s.db() as c:
  d=json.loads(s.getrow(out['id'])['payload']);d['city']='Севан';c.execute('UPDATE listings SET payload=? WHERE id=?',(s.dumps(d),out['id']))
 points=[{'lat':40.19,'lon':44.51,'label':'Улица 12, Ереван','precision':'building'}]
 async def geocode(address):return points
 s.geocode_address=geocode
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,executable_path=os.getenv('CHROMIUM_PATH') or None,args=['--no-sandbox','--enable-unsafe-swiftshader'])
  context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
  page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
  init={'initData':signed()['X-Telegram-Init-Data'],'colorScheme':'light','themeParams':{}}
  page.add_init_script('window.Telegram={WebApp:'+json.dumps(init)+'};Object.assign(Telegram.WebApp,{ready(){},expand(){},onEvent(){},BackButton:{show(){},hide(){},onClick(){}}});')
  def route(req):
   url=urlparse(req.request.url);requests.append(req.request.url)
   if url.hostname=='tiles.openfreemap.org':return req.fulfill(status=503 if style_failure else 200,content_type='application/json',body=json.dumps(style))
   if url.hostname!='rent.test':return req.fulfill(content_type='application/javascript',body='')
   headers={k:v for k,v in req.request.headers.items() if k in ('x-telegram-init-data','content-type')}
   response=client.request(req.request.method,url.path+('?' +url.query if url.query else ''),headers=headers,content=req.request.post_data)
   req.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
  context.route('**/*',route);page.goto('https://rent.test/');page.locator('.listing').wait_for()
  assert not any('/map' in x or '/assets/' in x or 'openfreemap.org' in x for x in requests)
  page.evaluate('state.filters.city="";render()');assert page.locator('[data-listing="'+out['id']+'"] [data-action=map]').count()==0
  def open_map():page.locator('[data-listing="'+ad['id']+'"] [data-action=map]').click()
  def close_map():page.locator('.sheet-head [data-action=close]').click();page.wait_for_function('!sheetKind');assert page.evaluate('rentalMap===null')
  open_map();page.wait_for_function('rentalMap?.loaded()')
  expect(page.locator('#map-status')).to_contain_text('Примерное расположение')
  expect(page.locator('.maplibregl-ctrl-attrib')).to_contain_text('OpenStreetMap')
  expect(page.locator('.maplibregl-ctrl-attrib')).to_contain_text('OpenMapTiles')
  assert page.locator('.maplibregl-canvas').evaluate('(x)=>x.getBoundingClientRect().width>200')
  assert page.evaluate('rentalMap.getLayoutProperty("refs","text-field")')==['get','ref']
  assert page.evaluate('rentalMap.getLayoutProperty("houses","text-field")')=='{housenumber}'
  rendered=page.evaluate('rentalMap.queryRenderedFeatures({layers:["labels"]}).map(x=>x.properties)')
  assert len(rendered)==3,rendered
  resolved=page.evaluate('(features)=>features.map(feature=>rentalMap.style.getLayer("labels").layout.get("text-field").evaluate({type:1,properties:feature}).toString())',[x['properties'] for x in features])
  assert resolved==['Русское название','Latin fallback','English fallback'],resolved
  zoom=page.evaluate('rentalMap.getZoom()');page.get_by_role('button',name='Приблизить',exact=True).click();page.wait_for_function('(z)=>rentalMap.getZoom()===z+1',arg=zoom)
  for width in [320,390,430]:
   page.set_viewport_size({'width':width,'height':844});assert page.evaluate('document.documentElement.scrollWidth')==width
   page.screenshot(path=str(O/f'map-{width}.png'),animations='disabled')
  close_map()
  with s.db() as c:c.execute('DELETE FROM listing_locations')
  points.append({'lat':40.18,'lon':44.52,'label':'Переулок','precision':'building'})
  open_map();page.wait_for_function('rentalMap?.loaded()')
  assert not page.locator('[data-action=map-point],[data-action=save-map]').count();close_map()
  with s.db() as c:c.execute('DELETE FROM listing_locations')
  points[:]=[{'lat':40.2,'lon':44.51,'label':'Улица','precision':'street'}]
  open_map();expect(page.locator('#map-status')).to_contain_text('расположение улицы',timeout=15000);close_map()
  style_failure=True;open_map();expect(page.locator('#map-status')).to_contain_text('временно недоступна');expect(page.locator('#map-external')).to_be_visible();assert '#map=15/40.2/44.51' in page.locator('#map-external').get_attribute('href');close_map();style_failure=False
  page.evaluate('()=>{window.originalContext=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(type,...args){return type==="webgl2"?null:originalContext.call(this,type,...args)}}')
  open_map();expect(page.locator('#map-status')).to_contain_text('временно недоступна');expect(page.locator('#map-external')).to_be_visible();close_map()
  page.evaluate('()=>{HTMLCanvasElement.prototype.getContext=originalContext}')
  open_map();page.wait_for_function('rentalMap?.loaded()');close_map()
  page.evaluate('window.originalApi=api;api=(path,...args)=>path.endsWith("/map")?new Promise(resolve=>window.resolveMap=()=>originalApi(path,...args).then(resolve)):originalApi(path,...args);void openMap('+json.dumps(ad['id'])+')')
  page.locator('#map-status').wait_for();close_map();page.evaluate('()=>{resolveMap();api=originalApi}');page.wait_for_timeout(250);assert page.locator('.maplibregl-canvas').count()==0
  points.clear()
  with s.db() as c:c.execute('DELETE FROM listing_locations')
  open_map();expect(page.locator('#map-status')).to_contain_text('Адрес не найден');expect(page.locator('#map-external')).to_be_hidden()
  assert not errors,errors
  print(json.dumps({'result':'passed','external_requests':0,'scenarios':['lazy assets and geocoding','Yerevan only','Russian and Latin labels','house and road reference labels preserved','zoom and cleanup','stored approximate point','street precision','provider and WebGL failure fallback','stale result after close','no result'],'js_errors':errors}))
  browser.close()
