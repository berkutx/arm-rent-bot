"""City, travel and empty catalog regressions."""
from test_server import client, body, signed
import server as s
import pytest

def test_yerevan_travel_and_district_persist(client):
    payload=body()
    payload['listing'].update(city='Ереван',district='Арабкир',metro_walk_minutes=10,center_drive_minutes=20)
    r=client.post('/api/listings',json=payload,headers=signed())
    assert r.status_code==200,r.text
    listing=client.get('/api/listings/'+r.json()['id']).json()
    assert listing['district']=='Арабкир'
    assert listing['metro_walk_minutes']==10 and listing['center_drive_minutes']==20

def test_other_city_clears_yerevan_only_fields(client):
    payload=body()
    payload['listing'].update(city='Дилижан',district='Арабкир',metro_walk_minutes=10,center_drive_minutes=15)
    r=client.post('/api/listings',json=payload,headers=signed())
    assert r.status_code==200,r.text
    assert r.json()['city']=='Дилижан' and r.json()['district']==''
    assert r.json()['metro_walk_minutes'] is None and r.json()['center_drive_minutes']==15
    assert s.matches(r.json(),{'city':'Дилижан'})
    assert not s.matches(r.json(),{'city':'Ереван'})

@pytest.mark.parametrize('field,value',[('metro_walk_minutes',0),('metro_walk_minutes',181),('center_drive_minutes',-1),('center_drive_minutes',361),('center_drive_minutes',1.5)])
def test_travel_bounds(client,field,value):
    payload=body();payload['listing'][field]=value
    assert client.post('/api/listings',json=payload,headers=signed()).status_code==422

def test_unspecified_travel_is_unknown(client):
    r=client.post('/api/listings',json=body(),headers=signed())
    assert r.status_code==200
    assert r.json()['metro_walk_minutes'] is None and r.json()['center_drive_minutes'] is None

def test_empty_database_and_no_example_endpoint(client):
    assert client.get('/api/listings').json()==[]
    assert client.get('/api/examples').status_code==404
    with s.db() as c:assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
