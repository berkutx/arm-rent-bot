import asyncio
import json

import pytest
import httpx

import server as s
from test_server import agent_profile, body, client, signed


@pytest.mark.parametrize('role', ['owner', 'tenant', 'unknown'])
def test_paid_requires_agent(client, role):
    request = body(commission=50)
    request['listing']['role'] = role
    assert client.post('/api/listings', json=request, headers=signed()).status_code == 400


@pytest.mark.parametrize('fee,kind,accepted', [(50, 'percent', True), (101, 'percent', False), (80000, 'fixed', True), (-1, 'fixed', False)])
def test_commission_values_and_market(client, fee, kind, accepted):
    agent_profile(client)
    request = body(commission=fee)
    request['listing'].update(role='agent', commission_type=kind, commission_currency='USD', commission_basis='day')
    response = client.post('/api/listings', json=request, headers=signed())
    assert (response.status_code == 200) == accepted
    if accepted:
        listing = response.json()
        assert listing['commission'] == fee
        assert s.matches(listing, {'market': 'paid'})
        assert not s.matches(listing, {'market': 'free'})
        assert not s.matches(listing, {})
        text = s.public_text(listing)
        assert ('USD' if kind == 'fixed' else '% от аренды за сутки') in text


def test_paid_never_falls_back_to_free_channel(client, monkeypatch):
    agent_profile(client)
    request = body(commission=50)
    request['listing']['role'] = 'agent'
    listing = client.post('/api/listings', json=request, headers=signed()).json()
    monkeypatch.setattr(s, 'CHAT', '@free_channel')
    monkeypatch.setattr(s, 'PAID_CHAT', '')
    calls = []

    async def telegram(method, args):
        calls.append((method, args))
        return {'message_id': 17, 'chat': {'id': -100456, 'type': 'supergroup'}, 'message_thread_id': args.get('message_thread_id')}

    monkeypatch.setattr(s, 'tg', telegram)
    job = {'kind': 'publish', 'payload': json.dumps({'id': listing['id']})}
    asyncio.run(s.process_job(job))
    assert calls == []
    monkeypatch.setattr(s, 'PAID_CHAT', '@paid_channel')
    monkeypatch.setattr(s, 'PAID_THREAD', 23)
    asyncio.run(s.process_job(job))
    assert calls[0][1]['chat_id'] == '@paid_channel'
    assert calls[0][1]['message_thread_id'] == 23
    monkeypatch.setattr(s, 'PAID_CHAT', '@changed_channel')
    asyncio.run(s.process_job({**job, 'kind': 'edit'}))
    assert calls[-1][1]['chat_id'] == -100456
    assert '_telegram_post' not in client.get('/api/listings/' + listing['id']).text


def test_subscriptions_keep_markets_separate(client):
    agent_profile(client)
    for uid, market in [(43, 'free'), (44, 'paid')]:
        response = client.post('/api/subscriptions', headers=signed(uid), json={'name': market, 'frequency':'instant', 'filters':{'market':market}})
        assert response.status_code == 200
    ids = {}
    for market, fee in [('free', 0), ('paid', 50)]:
        request = body(address='Раздел 10 ' + market, commission=fee)
        request['listing']['role'] = 'agent'
        ids[market] = client.post('/api/listings', headers=signed(), json=request).json()['id']
    with s.db() as c:
        jobs = [json.loads(row['payload']) for row in c.execute("SELECT payload FROM jobs WHERE kind='match'")]
    assert {(job['uid'], job['id']) for job in jobs} == {(43, ids['free']), (44, ids['paid'])}


def sizes(prefix=''):
    return [{'file_id':prefix+name,'file_unique_id':prefix+'unique-'+name,'width':w,'height':h} for name,w,h in [('small',320,240),('preview',800,600),('full',2560,1920)]]


def mock_telegram_files(monkeypatch,content):
    requests=[]
    async def telegram(method,args):
        assert method=='getFile'
        requests.append(args['file_id'])
        return {'file_path':'photos/'+args['file_id']+'.jpg'}
    def download(request):
        name=request.url.path.rsplit('/',1)[-1][:-4]
        return httpx.Response(200,content=content[name])
    original=httpx.AsyncClient
    monkeypatch.setattr(s,'tg',telegram)
    monkeypatch.setattr(s.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(download),**kw))
    s.FILE_PATHS.clear()
    return requests


def test_photo_bytes_pass_through_and_metadata_is_trusted(client,monkeypatch):
    requests=mock_telegram_files(monkeypatch,{'full':b'original telegram jpeg','preview':b'telegram preview'})
    photo=s.store_telegram_photo(sizes(),42)
    assert (photo['width'],photo['height'])==(2560,1920)
    for _ in range(2):
        response=client.get(photo['url'])
        assert response.content==b'original telegram jpeg'
        assert response.headers['cache-control']=='public, max-age=86400'
        assert s.TOKEN not in str(response.headers)
    assert client.get(photo['thumb_url']).content==b'telegram preview'
    assert requests==['full','preview']
    assert not list(s.DATA.rglob('*.jpg'))
    request=body();request['listing']['photos']=[{**photo,'width':1,'height':1,'thumb_url':'https://untrusted.example/a.jpg'}]
    response=client.post('/api/listings',json=request,headers=signed())
    assert response.json()['photos']==[photo]
    assert 'file_id' not in response.text and s.TOKEN not in response.text
    assert client.post('/api/listings',json=request,headers=signed(43)).status_code==400
    assert client.post('/api/photos',headers=signed(),content=b'raw photo').status_code==404


def test_receive_album_stores_metadata_without_downloading(client,monkeypatch):
    calls=[]
    async def telegram(method,args):calls.append(method);return {}
    monkeypatch.setattr(s,'tg',telegram)
    for i in range(11):
        asyncio.run(s.receive({'message':{'chat':{'type':'private'},'from':{'id':42},'photo':sizes(str(i)),'media_group_id':'album'}}))
    draft=client.get('/api/draft',headers=signed()).json()
    assert len(draft['photos'])==10 and calls==['sendMessage']
    assert not list(s.DATA.rglob('*.jpg'))
    assert not client.get('/api/draft',headers=signed(43)).json()['photos']
    with s.db() as c:assert c.execute('SELECT COUNT(*) FROM photos').fetchone()[0]==10


def test_draft_sync_checks_ownership_and_removes_photos(client):
    mine=s.store_telegram_photo(sizes(),42);other=s.store_telegram_photo(sizes('other'),43)
    request={'text':'Описание сохраняется','photos':[mine['id']]}
    assert client.post('/api/draft',json=request,headers=signed()).status_code==200
    assert client.get('/api/draft',headers=signed()).json()=={'text':request['text'],'photos':[mine]}
    request['photos']=[other['id']]
    assert client.post('/api/draft',json=request,headers=signed()).status_code==400
    request['photos']=[]
    assert client.post('/api/draft',json=request,headers=signed()).status_code==200
    assert client.get('/api/draft',headers=signed()).json()['photos']==[]
    assert client.post('/api/draft',json=request).status_code==401


def test_publication_reuses_telegram_file_id(client,monkeypatch):
    photo=s.store_telegram_photo(sizes(),42);request=body();request['listing']['photos']=[photo]
    listing=client.post('/api/listings',json=request,headers=signed()).json();calls=[]
    async def telegram(method,args):calls.append((method,args));return {}
    monkeypatch.setattr(s,'tg',telegram)
    asyncio.run(s.send_listing(listing))
    assert calls[0][0]=='sendPhoto' and calls[0][1]['photo']=='full'


@pytest.mark.parametrize('failure',['telegram','http','unsafe_path','oversized'])
def test_media_errors_hide_token(client,monkeypatch,failure):
    photo=s.store_telegram_photo(sizes(),42);s.FILE_PATHS.clear()
    async def telegram(method,args):
        if failure=='telegram':raise s.TelegramError(400)
        return {'file_path':'../../secret' if failure=='unsafe_path' else 'photos/full.jpg'}
    def download(request):
        if failure=='http':raise httpx.ReadError('secret '+s.TOKEN,request=request)
        return httpx.Response(200,headers={'content-length':'20000001'})
    original=httpx.AsyncClient
    monkeypatch.setattr(s,'tg',telegram)
    monkeypatch.setattr(s.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(download),**kw))
    response=client.get(photo['url'])
    assert response.status_code==502 and s.TOKEN not in response.text
    assert response.headers.get('location') is None


def test_expired_file_path_refreshes_once(client,monkeypatch):
    photo=s.store_telegram_photo(sizes(),42);s.FILE_PATHS.clear();calls=[]
    async def telegram(method,args):calls.append(method);return {'file_path':'photos/'+str(len(calls))+'.jpg'}
    original=httpx.AsyncClient
    def download(request):return httpx.Response(404 if request.url.path.endswith('/1.jpg') else 200,content=b'jpeg')
    monkeypatch.setattr(s,'tg',telegram)
    monkeypatch.setattr(s.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(download),**kw))
    assert client.get(photo['url']).content==b'jpeg' and len(calls)==2
    assert client.get('/media/'+('a'*32)+'.jpg').status_code==404


def test_update_empty_photo_table_preserves_other_data(client):
    key=(s.DATA/'private.key').read_bytes()
    with s.db() as c:
        c.execute('DROP TABLE photos')
        c.execute('CREATE TABLE photos(id TEXT PRIMARY KEY,uid INTEGER,path TEXT,sha TEXT,tg_file_id TEXT,created REAL)')
        c.execute("INSERT INTO meta VALUES('offset','123')")
    s.setup();photo=s.store_telegram_photo(sizes(),42)
    assert photo['width']==2560 and (s.DATA/'private.key').read_bytes()==key
    with s.db() as c:assert c.execute("SELECT value FROM meta WHERE key='offset'").fetchone()[0]=='123'


def test_interrupted_photo_is_not_returned_as_success(client,monkeypatch):
    photo=s.store_telegram_photo(sizes(),42);s.FILE_PATHS.clear()
    class BrokenPhoto(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'x'*65536
            raise httpx.ReadError('secret '+s.TOKEN)
    async def telegram(method,args):return {'file_path':'photos/full.jpg'}
    original=httpx.AsyncClient
    monkeypatch.setattr(s,'tg',telegram)
    monkeypatch.setattr(s.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=BrokenPhoto())),**kw))
    async def consume():
        response=await s.media(photo['url'].split('/')[-1])
        with pytest.raises(RuntimeError,match='Photo transfer interrupted') as error:
            async for _ in response.body_iterator:pass
        assert s.TOKEN not in str(error.value)
    asyncio.run(consume())
