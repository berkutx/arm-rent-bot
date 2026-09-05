"""Manual rental dates never imply parsing, expiration or publication freshness."""
import asyncio
import pytest
import server as s
from test_server import client, body, signed

SOURCE='С 15 числа сдается дом до мая месяца, стоимость 600.000 + ком услуги, дом находится в центре Еревана на улице Таирова, за подробностями в ЛС'

def test_description_does_not_fill_fields(client):
    payload=body();payload['listing'].update(description=SOURCE,kind='house',rooms=None,address='Другая улица',prices=[{'amount':900,'currency':'USD','period':'day'}])
    r=client.post('/api/listings',json=payload,headers=signed());assert r.status_code==200,r.text
    listing=r.json()
    assert listing['description']==SOURCE and listing['address']=='Другая улица'
    assert listing['prices'][0]['amount']==900 and listing['prices'][0]['currency']=='USD'
    assert listing['available'] is None and listing['available_until'] is None

@pytest.mark.parametrize('start,end',[(None,None),('2026-09-15',None),(None,'2027-05-01'),('2026-09-15','2027-05-01'),('2026-09-15','2026-09-15')])
def test_rental_dates_persist(client,start,end):
    payload=body();payload['listing'].update(available=start,available_until=end,description=SOURCE)
    r=client.post('/api/listings',json=payload,headers=signed());assert r.status_code==200,r.text
    listing=client.get('/api/listings/'+r.json()['id']).json()
    assert listing['available']==start and listing['available_until']==end
    assert listing['description']==SOURCE

@pytest.mark.parametrize('field,value',[('available','2026-02-30'),('available','2026-9-15'),('available_until','2027-13-01'),('available_until','до мая'),('available','15 числа')])
def test_incomplete_or_invalid_dates_rejected(client,field,value):
    payload=body();payload['listing'][field]=value
    assert client.post('/api/listings',json=payload,headers=signed()).status_code==400

def test_end_before_start_rejected(client):
    payload=body();payload['listing'].update(available='2026-09-15',available_until='2026-05-01')
    r=client.post('/api/listings',json=payload,headers=signed())
    assert r.status_code==400
    assert client.get('/api/listings').json()==[]

def test_rental_end_does_not_expire_or_redate_listing(client):
    payload=body();payload['listing'].update(available='2025-01-01',available_until='2025-05-01')
    r=client.post('/api/listings',json=payload,headers=signed());assert r.status_code==200,r.text
    before=r.json();asyncio.run(s.maintenance())
    after=client.get('/api/listings/'+before['id']).json()
    assert after['status']=='active' and after['created_at']==before['created_at']
    text=s.public_text(after)
    assert 'Сдаётся с 01.01.2025' in text and 'Сдаётся до 01.05.2025' in text
