"""Publication links and same-author public catalog; Telegram calls are mocked."""
import asyncio,json
import pytest
import server as s
from test_server import client,body,signed,create


def test_same_author_uses_authenticated_creator_not_contact(client):
    first=create(client,address='Первая 1')
    second=create(client,address='Вторая 2',private={'note':'SECRET','document_number':'DOC-SECRET','document_password':'PASS-SECRET'})
    other=client.post('/api/listings',json=body(address='Чужая 3'),headers=signed(43)).json()
    # Both Telegram accounts deliberately have the same username in the fixture.
    assert second['contact']==other['contact']
    with s.db() as c:before=c.execute('SELECT count(*) FROM jobs').fetchone()[0]
    response=client.get('/api/listings/'+first['id']+'/author-listings')
    assert response.status_code==200 and response.json()['available']
    assert [x['id'] for x in response.json()['listings']]==[second['id']]
    assert all(x not in response.text for x in ('SECRET','fingerprint','"uid"','private','review_reason'))
    with s.db() as c:assert c.execute('SELECT count(*) FROM jobs').fetchone()[0]==before


def test_author_list_hides_nonpublic_and_rented_offers(client):
    first=create(client,address='Основная 1')
    active=create(client,address='Активная 2')
    for i,status in enumerate(('review','rejected','rented')):
        row=create(client,address=f'Скрытая {i+3}')
        with s.db() as c:c.execute('UPDATE listings SET status=? WHERE id=?',(status,row['id']))
        if status!='rented':assert client.get('/api/listings/'+row['id']+'/author-listings').status_code==404
    with s.db() as c:c.execute("UPDATE listings SET status='rented' WHERE id=?",(first['id'],))
    data=client.get('/api/listings/'+first['id']+'/author-listings').json()
    assert [x['id'] for x in data['listings']]==[active['id']]
    assert client.get('/api/listings/missing/author-listings').status_code==404


@pytest.mark.parametrize('uid,sample',[(0,False),(42,True)])
def test_unknown_or_sample_authors_are_not_grouped(client,uid,sample):
    first=create(client,address='Пример 1');create(client,address='Пример 2')
    with s.db() as c:
        payload=json.loads(s.getrow(first['id'])['payload']);payload['sample']=sample
        c.execute('UPDATE listings SET uid=?,payload=? WHERE id=?',(uid,s.dumps(payload),first['id']))
    assert not client.get('/api/listings/'+first['id']).json()['author_listings_available']
    assert client.get('/api/listings/'+first['id']+'/author-listings').json()=={'available':False,'listings':[]}


def test_empty_author_catalog_and_unpublished_post(client,monkeypatch):
    monkeypatch.setattr(s,'CHAT','')
    first=create(client)
    assert first['author_listings_available'] and first['telegram_post_url']==''
    assert client.get('/api/listings/'+first['id']+'/author-listings').json()=={'available':True,'listings':[]}
    assert not client.get('/api/config').json()['channel_configured']
    monkeypatch.setattr(s,'CHAT','@test_channel')
    assert client.get('/api/config').json()['channel_configured']
    assert client.get('/api/listings/'+first['id']).json()['telegram_post_url']==''


def test_submission_cannot_forge_publication_or_author(client):
    request=body()
    request['listing'].update(uid=123,author_listings_available=False,telegram_post_url='https://evil.example',_telegram_post={'chat':{'type':'channel','username':'fake_channel'},'message_id':20})
    response=client.post('/api/listings',json=request,headers=signed())
    assert response.status_code==200
    assert response.json()['telegram_post_url']=='' and response.json()['author_listings_available']
    assert s.getrow(response.json()['id'])['uid']==42


@pytest.mark.parametrize('chat,thread,expected',[
    ({'id':-1001234567890,'type':'channel','username':'test_channel'},None,'https://t.me/test_channel/45'),
    ({'id':-1001234567890,'type':'supergroup'},7,'https://t.me/c/1234567890/45?thread=7'),
    ({'id':-1001234567890,'type':'supergroup','username':'test_group'},7,'https://t.me/test_group/45?thread=7'),
    ({'id':42,'type':'private','username':'test_person'},None,''),
    ({'id':-1234,'type':'group'},None,''),
])
def test_actual_publication_link_persists_without_exposing_chat(client,monkeypatch,chat,thread,expected):
    first=create(client);monkeypatch.setattr(s,'CHAT','@configured_channel')
    async def sent(_):return {'message_id':45,'chat':{**chat,'title':'PRIVATE-CHAT-TITLE'},'message_thread_id':thread},'text'
    monkeypatch.setattr(s,'send_listing',sent)
    asyncio.run(s.process_job({'kind':'publish','payload':json.dumps({'id':first['id']})}))
    # The original target remains correct if publication configuration later changes.
    monkeypatch.setattr(s,'CHAT','@different_channel')
    response=client.get('/api/listings/'+first['id'])
    assert response.json()['telegram_post_url']==expected
    assert '_telegram_post' not in response.text and 'PRIVATE-CHAT-TITLE' not in response.text
    assert response.json()['created_at']==first['created_at']
