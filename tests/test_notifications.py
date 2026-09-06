import asyncio
import json

import pytest

import server as s
from test_server import body, client, create, signed


def subscription(client, frequency='daily'):
    response=client.post('/api/subscriptions',headers=signed(43),json={
        'name':'Квартира','frequency':frequency,'filters':{'market':'free'}})
    assert response.status_code==200


def delivery_ids():
    with s.db() as c:return {r['lid'] for r in c.execute('SELECT lid FROM deliveries WHERE uid=43')}


def digest():
    asyncio.run(s.process_job({'kind':'digest','payload':json.dumps({'uid':43})}))


def telegram_mock(monkeypatch):
    calls=[]
    async def telegram(method,payload):
        assert method=='sendMessage'
        assert payload['chat_id']==43
        calls.append(payload)
        return {'message_id':len(calls)}
    monkeypatch.setattr(s,'tg',telegram)
    return calls


def test_daily_excludes_legacy_and_live_imports(client,monkeypatch):
    subscription(client);calls=telegram_mock(monkeypatch)
    native=create(client,address='Нативная 10')
    for i in range(2):
        imported=create(client,address=f'Импорт {i+10}')
        with s.db() as c:
            payload=json.loads(s.getrow(imported['id'])['payload'])
            payload['_source_post']={'chat':{'username':'test_source','type':'supergroup'},'message_id':i+1}
            if i:payload['_source_key']='100:2'
            c.execute('UPDATE listings SET payload=? WHERE id=?',(s.dumps(payload),imported['id']))
    digest();digest()
    assert len(calls)==1
    assert s.link('l_'+native['id']) in calls[0]['text']
    assert 'Импорт' not in calls[0]['text']
    assert delivery_ids()=={native['id']}


def test_daily_sends_complete_cards_and_keeps_overflow_for_next_digest(client,monkeypatch):
    subscription(client);calls=telegram_mock(monkeypatch);listings={}
    for i in range(8):
        request=body(address=f'Длинная улица {i+10}')
        request['listing']['prices']=[
            {'amount':300000,'currency':'AMD','period':'month','condition':'Срок '+('🏠'*190)},
            {'amount':1000,'currency':'USD','period':'month','condition':'Договор '+('🏠'*190)}]
        response=client.post('/api/listings',headers=signed(),json=request)
        assert response.status_code==200,response.text
        listing=response.json();listings[listing['id']]=listing
    assert sum(len(s.public_text(l)) for l in listings.values())>4000
    sent=set()
    for _ in range(8):
        digest();text=calls[-1]['text']
        included={lid for lid in listings if s.link('l_'+lid) in text}
        assert included and not included&sent
        assert len(text.encode('utf-16-le'))//2<=4000
        for lid in included:assert s.public_text(listings[lid])+'\n'+s.link('l_'+lid) in text
        sent|=included
        assert delivery_ids()==sent
        if len(calls)==1:assert len(sent)<8
        if len(sent)==8:break
    assert sent==set(listings)
    previous=len(calls);digest()
    assert len(calls)==previous


def test_failed_daily_send_does_not_mark_delivery(client,monkeypatch):
    subscription(client);listing=create(client)
    async def rejected(*args):raise s.TelegramError(403)
    monkeypatch.setattr(s,'tg',rejected)
    with pytest.raises(s.TelegramError):digest()
    assert delivery_ids()==set()
    calls=telegram_mock(monkeypatch);digest()
    assert len(calls)==1 and delivery_ids()=={listing['id']}


def test_instant_rechecks_pause_and_deduplicates(client,monkeypatch):
    subscription(client,'instant');calls=telegram_mock(monkeypatch);listing=create(client)
    with s.db() as c:
        job=dict(c.execute("SELECT * FROM jobs WHERE kind='match'").fetchone())
        c.execute('UPDATE subscriptions SET active=0 WHERE uid=43')
    asyncio.run(s.process_job(job))
    assert calls==[] and delivery_ids()==set()
    with s.db() as c:c.execute('UPDATE subscriptions SET active=1 WHERE uid=43')
    asyncio.run(s.process_job(job));asyncio.run(s.process_job(job))
    assert len(calls)==1 and delivery_ids()=={listing['id']}
