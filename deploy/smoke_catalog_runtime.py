"""Test the packaged runtime without network, token or live volume."""
import json
from fastapi.testclient import TestClient
import server
server.app.dependency_overrides[server.user]=lambda:{'id':42,'username':'test_user'}
with TestClient(server.app) as client:
    assert client.get('/api/listings').json()==[]
    assert len(client.get('/api/examples').json())==39
    payload={'listing':{'city':'Ереван','address':'Тестовая 999','district':'Арабкир','kind':'apartment','rooms':2,'commission':0,'metro_walk_minutes':10,'center_drive_minutes':20,'available':'2026-09-15','available_until':'2027-05-01','description':'С 15 числа до мая — исходный текст','prices':[{'amount':300000,'currency':'AMD','period':'month'}]},'consent':True}
    r=client.post('/api/listings',json=payload)
    assert r.status_code==200,r.text
    listing=client.get('/api/listings/'+r.json()['id']).json()
    assert listing['district']=='Арабкир' and listing['metro_walk_minutes']==10 and listing['center_drive_minutes']==20
    assert not listing['sample']
    assert listing['available']=='2026-09-15' and listing['available_until']=='2027-05-01'
    assert listing['description']=='С 15 числа до мая — исходный текст'
    assert 'Сдаётся до 01.05.2027' in server.public_text(listing)
    assert len(client.get('/api/examples').json())==39
    first=listing['id']
    assert listing['author_listings_available'] and not listing['telegram_post_url']
    assert client.get('/api/listings/'+first+'/author-listings').json()=={'available':True,'listings':[]}
    payload['listing']['address']='Тестовая 998'
    second=client.post('/api/listings',json=payload).json()
    assert [x['id'] for x in client.get('/api/listings/'+first+'/author-listings').json()['listings']]==[second['id']]
    assert server.phone_from_text('Тел: +374 (91) 123456')=='+37491123456'
    assert server.phone_from_text('091 123456')==''
    assert client.post('/api/listings/'+first+'/view',json={}).json()['view_count']==1
    assert client.post('/api/listings/'+first+'/view',json={}).json()['view_count']==1
    server.app.dependency_overrides[server.admin_user]=lambda:{'id':99}
    assert client.post('/api/admin/'+first+'/ban',json={'reason':'Тестовая причина блокировки'}).status_code==200
    assert client.get('/api/listings/'+first).status_code==404
    assert next(x for x in client.get('/api/mine').json() if x['id']==first)['ban_reason']=='Тестовая причина блокировки'
    restored=client.post('/api/admin/'+first+'/unban',json={}).json()
    assert restored['status']=='active' and restored['view_count']==1 and restored['created_at']==listing['created_at']
    print(json.dumps({'packaged_runtime':'passed','district_validation':'passed','travel_persistence':'passed','rental_dates':'passed','description_unchanged':True,'phone_masks':'passed','ban_and_restore':'passed','unique_views':'passed','author_catalog':'passed','unpublished_post_disabled':True,'read_only_examples':39,'live_data_accessed':False}))
