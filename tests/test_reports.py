import asyncio,json,time
from fastapi.responses import Response
import pytest
import server as s
from test_server import legacy_create,client,create,signed


def draft(client,lid,uid=43):
    r=client.post(f'/api/listings/{lid}/report-draft',headers=signed(uid),json={})
    assert r.status_code==200,r.text
    return r.json()


def submit(client,r,uid=43):
    return client.post('/api/listings/'+r['listing']['id']+'/report',headers=signed(uid),json={'report_id':r['id'],'reason':'Скрытая комиссия','details':'В переписке требуют доплату агенту.','evidence':'https://t.me/example_thread/123'})


def incoming(uid,**message):
    return {'message':{'chat':{'type':'private','id':uid},'from':{'id':uid,'first_name':'Reporter','username':'report_user'},**message}}


def photo(n=0):
    return [{'file_id':'PRIVATE_FILE_'+str(n),'file_unique_id':'private_unique_'+str(n),'width':1200,'height':1600}]


@pytest.fixture
def telegram(monkeypatch):
    calls=[]
    async def tg(method,data):
        calls.append((method,data));return {'message_id':1}
    monkeypatch.setattr(s,'tg',tg);monkeypatch.setattr(s,'PUBLIC_URL','https://rent.test');monkeypatch.setattr(s,'BOT','test_bot')
    return calls


def test_report_requires_form_and_auth(client):
    l=create(client);url='/api/listings/'+l['id']+'/report'
    assert client.post(url,json={}).status_code==401
    assert client.post(url,headers=signed(),json={}).status_code==422
    r=draft(client,l['id'])
    assert client.post(url,headers=signed(43),json={'report_id':r['id'],'reason':'   ','details':'Enough details here'}).status_code==400
    assert client.get('/api/admin/reports',headers=signed(99)).json()==[]
    assert client.post(url,headers=signed(43),json={'report_id':r['id'],'reason':'Too big','details':'x'*2001}).status_code==422


def test_report_identity_queue_idempotency_and_privacy(client,telegram):
    l=create(client);r=draft(client,l['id'])
    assert draft(client,l['id'])['id']==r['id']
    for _ in range(2):assert submit(client,r).status_code==200
    second=draft(client,l['id'],44);assert submit(client,second,44).status_code==200
    queue=client.get('/api/admin/reports',headers=signed(99)).json()
    assert len(queue)==2 and {x['reporter']['id'] for x in queue}=={43,44}
    assert all(x['reason']=='Скрытая комиссия' for x in queue)
    assert client.get('/api/admin/reports',headers=signed()).status_code==403
    for uid in [42,44]:assert client.get('/api/reports/'+r['id'],headers=signed(uid)).status_code==404
    assert 'reporter' not in client.get('/api/listings').text
    assert 'Скрытая комиссия' not in client.get('/api/listings').text
    assert client.get('/api/reports/'+r['id'],headers=signed(43)).headers['cache-control']=='no-store'
    with s.db() as c:
        jobs=c.execute("SELECT * FROM jobs WHERE kind='report'").fetchall()
        assert len(jobs)==2
        assert 'Скрытая комиссия'.encode() not in c.execute('SELECT payload FROM reports LIMIT 1').fetchone()[0]
    asyncio.run(s.process_job(jobs[0]))
    message=telegram[0][1]
    assert message['chat_id']==99 and 'ID 43' in message['text'] and '@test_user' in message['text']
    assert 'Скрытая комиссия' in message['text'] and 'требуют доплату' in message['text']
    buttons=message['reply_markup']['inline_keyboard']
    assert buttons==[[s.app_button('Разобрать жалобу','report_'+r['id'])]]
    assert 'callback_data' not in json.dumps(buttons) and 'startapp' not in json.dumps(buttons)


def test_private_evidence_handoff_limits_and_delete(client,telegram,monkeypatch):
    l=create(client);r=draft(client,l['id'])
    asyncio.run(s.receive(incoming(44,text='/start proof_'+r['id'])))
    with s.db() as c:assert not c.execute('SELECT * FROM report_uploads').fetchall()
    asyncio.run(s.receive(incoming(43,text='/start proof_'+r['id'])))
    for n in [0,0,1,2,3,4,5]:asyncio.run(s.receive(incoming(43,photo=photo(n))))
    updated=client.get('/api/reports/'+r['id'],headers=signed(43)).json()
    assert len(updated['photos'])==5
    with s.db() as c:
        assert c.execute('SELECT count(*) FROM photos').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM drafts').fetchone()[0]==0
    proof=updated['photos'][0]
    for uid in [42,44]:assert client.get(proof['url'],headers=signed(uid)).status_code==404
    assert client.get(proof['url']).status_code==401
    assert client.get('/media/'+proof['id']+'.jpg').status_code==404
    calls=[]
    async def stream(p,private=False):
        calls.append((p,private));return Response(b'image',media_type='image/jpeg')
    monkeypatch.setattr(s,'stream_telegram_photo',stream)
    response=client.get(proof['url'],headers=signed(99))
    assert response.status_code==200 and response.headers['cache-control']=='no-store'
    assert calls[0][1] is True and calls[0][0]['file_id']=='PRIVATE_FILE_0'
    assert client.delete(proof['url'],headers=signed(99)).status_code==409
    assert client.delete(proof['url'],headers=signed(43)).status_code==200
    assert client.get(proof['url'],headers=signed(43)).status_code==404
    assert submit(client,r).status_code==200
    asyncio.run(s.receive(incoming(43,photo=photo(6))))
    with s.db() as c:assert c.execute('SELECT count(*) FROM drafts').fetchone()[0]==0
    assert client.put('/api/reports/'+r['id'],headers=signed(43),json={'reason':'change'}).status_code==409
    asyncio.run(s.receive(incoming(43,text='/cancel')))
    with s.db() as c:assert c.execute('SELECT count(*) FROM report_uploads').fetchone()[0]==0


@pytest.mark.parametrize('outcome',['dismiss','ban'])
def test_resolution_admin_only_atomic_and_reason_required(client,outcome):
    l=create(client);r=draft(client,l['id']);submit(client,r)
    url='/api/admin/reports/'+r['id']+'/resolve'
    assert client.post(url,headers=signed(43),json={'outcome':outcome,'reason':'Обоснование'}).status_code==403
    assert client.post(url,headers=signed(99),json={'outcome':outcome,'reason':'   '}).status_code==400
    result=client.post(url,headers=signed(99),json={'outcome':outcome,'reason':'Условия подтверждены перепиской.'})
    assert result.status_code==200 and result.json()['status']=='resolved'
    assert client.post(url,headers=signed(99),json={'outcome':outcome,'reason':'Повтор'}).status_code==409
    row=s.getrow(l['id']);assert row['status']==('banned' if outcome=='ban' else 'active')
    assert s.stamp(row['created'])==l['created_at']
    own=client.get('/api/mine',headers=signed()).json()[0]
    if outcome=='ban':assert own['ban_reason']=='Условия подтверждены перепиской.'
    assert 'reporter' not in own
    assert client.get('/api/reports/'+r['id'],headers=signed(43)).json()['outcome']==outcome


def test_existing_report_preserved_without_invented_reason(client):
    l=create(client);s.audit(43,l['id'],'report');s.enqueue('admin',{'id':l['id'],'report':True},'legacy-report')
    with s.db() as c:c.execute("DELETE FROM meta WHERE key='reports_v1'")
    s.setup();s.setup()
    queue=client.get('/api/admin/reports',headers=signed(99)).json()
    assert len(queue)==1 and queue[0]['reporter']['id']==43 and queue[0]['reason']==''
    with s.db() as c:assert c.execute("SELECT status FROM jobs WHERE jobkey='legacy-report'").fetchone()[0]=='cancelled'


def test_deep_links_and_review_buttons_use_supported_launch(client,telegram):
    l=legacy_create(client,private={'document_number':'PRIVATE_NUMBER','document_password':'PRIVATE_PASSWORD'})
    asyncio.run(s.process_job({'kind':'admin','payload':json.dumps({'id':l['id']})}))
    assert telegram[-1][1]['reply_markup']['inline_keyboard']==[[s.app_button('Открыть задачу','review_'+l['id'])]]
    for target in ['l_'+l['id'],'l_s123456','report_'+'a'*16,'admin']:
        asyncio.run(s.receive(incoming(99,text='/start '+target)))
        assert telegram[-1][1]['reply_markup']['inline_keyboard'][0][0]['web_app']['url']=='https://rent.test?start='+target
    assert s.link('l_'+l['id'])=='https://t.me/test_bot?start=l_'+l['id']


def test_legacy_report_buttons_cannot_publish_or_reject(client,telegram):
    l=create(client)
    for action in ['approve','reject']:
        asyncio.run(s.receive({'callback_query':{'id':'cb','from':{'id':99},'data':action+':'+l['id'],'message':{'text':'Жалоба на объявление\nАдрес'}}}))
        assert telegram[-1][0]=='answerCallbackQuery' and telegram[-1][1]['show_alert']
        assert s.getrow(l['id'])['status']=='active'
