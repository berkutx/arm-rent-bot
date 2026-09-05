import hashlib,hmac,json,time,tempfile
from urllib.parse import urlencode
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
import server as s

def signed(uid=42,ts=None,token='test-token'):
    d={'auth_date':str(int(ts or time.time())),'user':json.dumps({'id':uid,'first_name':'Tester','username':'test_user'},separators=(',',':'))}
    sec=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    d['hash']=hmac.new(sec,'\n'.join(f'{k}={v}' for k,v in sorted(d.items())).encode(),hashlib.sha256).hexdigest()
    return {'X-Telegram-Init-Data':urlencode(d)}

@pytest.fixture
def client(monkeypatch,tmp_path):
    monkeypatch.setattr(s,'DATA',tmp_path);monkeypatch.setattr(s,'DB',tmp_path/'db.sqlite3');s.setup()
    monkeypatch.setattr(s,'LIVE',True);monkeypatch.setattr(s,'TOKEN','test-token');monkeypatch.setattr(s,'ADMINS',{99});s.limits.clear()
    return TestClient(s.app)

def body(address='Тестовая 12',commission=0,private=None):
    return {'listing':{'address':address,'kind':'apartment','prices':[{'amount':300000,'currency':'AMD','period':'month'}],'rooms':2,'commission':commission,'role':'owner','document_status':'document_checked','sample':False},'private':private or {},'consent':True}

def create(c,**kw):
    r=c.post('/api/listings',json=body(**kw),headers=signed());assert r.status_code==200,r.text;return r.json()

def test_auth_valid_and_tampered():
    raw=signed()['X-Telegram-Init-Data'];assert s.validate_init_data(raw,'test-token')['id']==42
    with pytest.raises(ValueError):s.validate_init_data(raw.replace('Tester','Attacker'),'test-token')
    with pytest.raises(ValueError):s.validate_init_data(signed(ts=time.time()-90000)['X-Telegram-Init-Data'],'test-token')

def test_unauthenticated_cannot_write(client):
    assert client.post('/api/listings',json=body()).status_code==401

def test_public_projection_and_admin_only(client):
    l=create(client,private={'note':'SECRET-NOTE','document_number':'SECRET-NUMBER','document_password':'SECRET-PASS'})
    assert l['status']=='active' and l['document_status']=='pending'
    public=client.get('/api/listings').text
    assert 'SECRET' not in public and 'private' not in public
    assert client.get('/api/admin/'+l['id']+'/private',headers=signed()).status_code==403
    r=client.get('/api/admin/'+l['id']+'/private',headers=signed(99));assert r.json()['document_password']=='SECRET-PASS'
    assert b'SECRET-PASS' not in s.DB.read_bytes()
    r=client.post('/api/admin/'+l['id']+'/decision',headers=signed(99),json={'decision':'document_checked'});assert r.status_code==400
    r=client.post('/api/admin/'+l['id']+'/verification',headers=signed(99),json={'result':'document_checked','document_valid':True,'object_matches':True,'checked_on':'2026-09-05'});assert r.status_code==200,r.text
    r=client.get('/api/admin/'+l['id']+'/private',headers=signed(99));assert not r.json()['document_password']

def test_cannot_fake_verification(client):
    l=create(client);assert l['document_status']=='none'

def test_duplicate_submission_is_idempotent(client):
    a=create(client);b=create(client);assert a['id']==b['id']
    assert len(client.get('/api/listings').json())==1

def test_unknown_fee_is_not_zero(client):
    assert client.post('/api/listings',json=body(commission=None),headers=signed()).status_code==400
    assert client.post('/api/listings',json=body(commission=50),headers=signed()).status_code==400
    assert client.get('/api/listings').json()==[]

def test_duplicate_address_goes_to_review(client):
    create(client)
    x=body();x['listing']['description']='Другой текст, тот же адрес'
    r=client.post('/api/listings',json=x,headers=signed());assert r.status_code==200
    l=r.json();assert l['status']=='review'
    assert client.post('/api/listings/'+l['id']+'/status',json={'status':'active'},headers=signed()).status_code==403
    assert client.get('/api/listings/'+l['id']).status_code==404
    assert client.post('/api/admin/'+l['id']+'/decision',json={'decision':'approve'},headers=signed()).status_code==403

def test_status_owner_only(client):
    l=create(client)
    assert client.post('/api/listings/'+l['id']+'/status',json={'status':'rented'},headers=signed(100)).status_code==403
    assert client.post('/api/listings/'+l['id']+'/status',json={'status':'rented'},headers=signed()).status_code==200
    assert client.get('/api/listings').json()==[]

def test_matches_same_currency_period_only():
    l={'status':'active','city':'Ереван','kind':'apartment','rooms':2,'commission':0,'role':'unknown','pets':'unknown','prices':[{'amount':25000,'currency':'AMD','period':'day'},{'amount':1200,'currency':'USD','period':'month'}]}
    assert not s.matches(l,{'period':'month','currency':'AMD','max':'300000'})
    assert s.matches(l,{'period':'day','currency':'AMD','max':'30000'})
    assert not s.matches(l,{'pets':True})

def test_photo_ownership_and_no_url_injection(client):
    x=body();x['listing']['photos']=[{'id':'a'*32,'url':'http://127.0.0.1/admin'}]
    assert client.post('/api/listings',json=x,headers=signed()).status_code==400

def test_doc_requires_both_fields(client):
    assert client.post('/api/listings',json=body(private={'document_password':'SECRET'}),headers=signed()).status_code==400

def test_subscription_private_and_pause(client):
    r=client.post('/api/subscriptions',headers=signed(),json={'name':'Дом','filters':{'period':'month','market':'free'},'frequency':'instant'});assert r.status_code==200
    sid=r.json()['id'];assert len(client.get('/api/subscriptions',headers=signed()).json())==1
    assert client.get('/api/subscriptions',headers=signed(100)).json()==[]
    client.patch('/api/subscriptions/'+sid,headers=signed(100),json={'active':False})
    assert client.get('/api/subscriptions',headers=signed()).json()[0]['active']
    client.patch('/api/subscriptions/'+sid,headers=signed(),json={'active':False})
    assert not client.get('/api/subscriptions',headers=signed()).json()[0]['active']

def test_no_private_in_telegram_message(client):
    l=create(client,private={'note':'SECRET','document_number':'NUMBER','document_password':'PASSWORD'})
    assert not any(x in s.public_text(l) for x in ['SECRET','NUMBER','PASSWORD'])
    assert not any(x in json.dumps(s.keyboard(l)) for x in ['SECRET','NUMBER','PASSWORD','fav:'])

def test_untrusted_callback_rejected(client,monkeypatch):
    import asyncio
    l=create(client);calls=[]
    with s.db() as c:c.execute("UPDATE listings SET status='review' WHERE id=?",(l['id'],))
    async def fake(method,payload):calls.append((method,payload));return {}
    monkeypatch.setattr(s,'tg',fake)
    asyncio.run(s.receive({'callback_query':{'id':'x','from':{'id':42},'data':'approve:'+l['id']}}))
    assert s.getrow(l['id'])['status']=='review'
    assert calls[-1][1].get('show_alert')
