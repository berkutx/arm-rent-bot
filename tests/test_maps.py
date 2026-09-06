import asyncio,time,json
import pytest,httpx
import server as s
from test_server import client,create

@pytest.fixture
def maps(client,monkeypatch):
 monkeypatch.setattr(s,'GEOCODE_LOCK',asyncio.Lock());monkeypatch.setattr(s,'GEOCODER_URL','https://geocoder.test/search')
 return client

def point(**kwargs):
 return {'lat':'40.19','lon':'44.51','display_name':'Улица 12, Ереван','addresstype':'building','address':{'country_code':'am','city':'Ереван','house_number':'12'},**kwargs}

def mock_provider(monkeypatch,rows,status=200,headers=None):
 calls=[]
 class Fake:
  def __init__(self,**kw):pass
  async def __aenter__(self):return self
  async def __aexit__(self,*a):pass
  async def get(self,url,**kw):
   calls.append((url,kw,time.monotonic()));return httpx.Response(status,json=rows,headers=headers,request=httpx.Request('GET',url))
 monkeypatch.setattr(s.httpx,'AsyncClient',Fake)
 return calls

def test_map_cache_only_public_address_and_invalidation(maps,monkeypatch):
 l=create(maps,address='Абовяна 12');calls=mock_provider(monkeypatch,[point()])
 assert maps.get('/api/listings/'+l['id']+'/map').json()['points'][0]['precision']=='building'
 assert maps.get('/api/listings/'+l['id']+'/map').status_code==200 and len(calls)==1
 assert calls[0][1]['params']['street']=='Абовяна 12'
 assert not any(k in json.dumps(calls) for k in ['test_user','description','phone','private','init_data'])
 assert 'ArmeniaRent' in calls[0][1]['headers']['User-Agent']
 with s.db() as c:
  payload=json.loads(s.getrow(l['id'])['payload']);payload['address']='Абовяна 13';c.execute('UPDATE listings SET payload=? WHERE id=?',(s.dumps(payload),l['id']))
 assert maps.get('/api/listings/'+l['id']+'/map').status_code==200
 assert len(calls)==2 and calls[1][2]-calls[0][2]>=1

@pytest.mark.parametrize('change',[{'city':'Севан'},{'status':'banned'},{'status':'review'},{'status':'source_deleted'}])
def test_map_hidden_or_outside_yerevan_never_calls_provider(maps,monkeypatch,change):
 l=create(maps);calls=mock_provider(monkeypatch,[point()])
 with s.db() as c:
  payload=json.loads(s.getrow(l['id'])['payload']);payload.update(change);c.execute('UPDATE listings SET payload=?,status=? WHERE id=?',(s.dumps(payload),change.get('status','active'),l['id']))
 assert maps.get('/api/listings/'+l['id']+'/map').status_code==404 and not calls

def test_map_rejects_foreign_coordinates_and_marks_street_precision():
 assert not s.map_candidates([point(lat='41.0'),point(lon='NaN'),point(address={'country_code':'ge'})])
 street=point(addresstype='road',address={'country_code':'am','city':'Ереван'})
 assert s.map_candidates([street])[0]['precision']=='street'
 assert len(s.map_candidates([point(),point()]))==1

def test_map_empty_response_cached_and_provider_limit_respected(maps,monkeypatch):
 l=create(maps);calls=mock_provider(monkeypatch,[])
 assert maps.get('/api/listings/'+l['id']+'/map').json()['points']==[]
 assert maps.get('/api/listings/'+l['id']+'/map').status_code==200 and len(calls)==1
 with s.db() as c:c.execute('DELETE FROM geocode_cache');c.execute('DELETE FROM listing_locations')
 calls=mock_provider(monkeypatch,{},429,{'Retry-After':'120'})
 assert maps.get('/api/listings/'+l['id']+'/map').status_code==503
 assert maps.get('/api/listings/'+l['id']+'/map').status_code==503 and len(calls)==1

def test_maplibre_assets_headers_and_no_directory_access(maps):
 assert maps.get('/').headers['referrer-policy']=='strict-origin-when-cross-origin'
 for name in ['maplibre-gl.mjs','maplibre-gl-shared.mjs','maplibre-gl-worker.mjs','maplibre-gl.css']:
  response=maps.get('/assets/maplibre-6.7.0/'+name)
  assert response.status_code==200 and len(response.content)>1000
  assert response.headers['content-type'].startswith('text/css' if name.endswith('.css') else 'text/javascript')
  assert 'immutable' in response.headers['cache-control']
 assert maps.get('/assets/maplibre-6.7.0/../../server.py').status_code==404
 assert maps.get('/assets/maplibre-6.7.0/LICENSE.txt').status_code==404
 assert maps.get('/assets/server.py').status_code==404


def test_map_edit_during_lookup_does_not_return_old_coordinates(maps,monkeypatch):
 l=create(maps)
 async def lookup(address):
  with s.db() as c:c.execute("UPDATE listings SET status='banned' WHERE id=?",(l['id'],))
  return [{'lat':40.19,'lon':44.51}]
 monkeypatch.setattr(s,'geocode_address',lookup)
 response=maps.get('/api/listings/'+l['id']+'/map')
 assert response.status_code==409 and '44.51' not in response.text


def test_approximate_location_is_persisted_without_repeated_choice(maps,monkeypatch):
 l=create(maps);calls=mock_provider(monkeypatch,[point(),point(lat='40.20',display_name='Переулок')])
 path='/api/listings/'+l['id']+'/map';result=maps.get(path).json()
 assert len(result['points'])==1 and result['points'][0]['lat']==40.19 and result['points'][0]['precision']=='area'
 with s.db() as c:c.execute('DELETE FROM geocode_cache')
 assert maps.get(path).json()==result and len(calls)==1


def test_missing_building_uses_explicit_district_and_invalidates_when_changed(maps,monkeypatch):
 l=create(maps)
 with s.db() as c:
  payload=json.loads(s.getrow(l['id'])['payload']);payload['district']='Арабкир';c.execute('UPDATE listings SET payload=? WHERE id=?',(s.dumps(payload),l['id']))
 calls=[]
 async def lookup(address,area=False):
  calls.append((address,area));return [{'lat':40.2,'lon':44.5,'label':'Арабкир','precision':'area'}] if area else []
 monkeypatch.setattr(s,'geocode_address',lookup)
 result=maps.get('/api/listings/'+l['id']+'/map').json()
 assert result['points'][0]['precision']=='district' and result['points'][0]['district']=='Арабкир'
 assert calls==[(l['address'],False),('Арабкир',True)]
 with s.db() as c:
  payload['district']='Кентрон';c.execute('UPDATE listings SET payload=? WHERE id=?',(s.dumps(payload),l['id']))
 assert maps.get('/api/listings/'+l['id']+'/map').json()['points'][0]['district']=='Кентрон'
