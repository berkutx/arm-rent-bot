import asyncio,json,time
import httpx
import pytest
import server as s
from test_server import client,create,signed

SOURCE='https://t.me/catalog_fixture/101'
CDN='https://cdn4.telesco.pe/file/fixture.jpg'

def imported(client,uid=-42):
    row=create(client)
    with s.db() as c:
        payload=json.loads(c.execute('SELECT payload FROM listings WHERE id=?',(row['id'],)).fetchone()[0])
        payload['_source_post']={'chat':{'username':'catalog_fixture','type':'supergroup'},'message_id':101,'message_thread_id':55}
        c.execute('UPDATE listings SET uid=?,payload=? WHERE id=?',(uid,s.dumps(payload),row['id']))
        c.execute('DELETE FROM jobs')
    return row['id']

def test_imported_links_and_effects(client,monkeypatch):
    lid=imported(client);row=client.get('/api/listings/'+lid).json()
    assert row['telegram_post_url']==SOURCE+'?thread=55'
    assert row['telegram_discussion_url']==row['telegram_post_url']
    assert row['author_listings_available'] and not row['is_mine']
    assert '_source_post' not in row
    s.active_effects(lid)
    with s.db() as c:assert c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
    calls=[]
    async def telegram(*args):calls.append(args)
    monkeypatch.setattr(s,'tg',telegram)
    for kind in ['publish','edit']:asyncio.run(s.process_job({'kind':kind,'payload':json.dumps({'id':lid})}))
    assert calls==[]
    assert client.get('/api/mine',headers=signed()).json()==[]


def add_source_photo():
    pid='e'*32;photo={'source_post':SOURCE,'source_message':SOURCE,'width':800,'height':600}
    with s.db() as c:c.execute('INSERT INTO photos(id,uid,sizes) VALUES(?,?,?)',(pid,-42,s.dumps({'full':photo,'thumb':photo})))
    return pid


def test_source_photo_redirects_only_to_telegram_cdn(client,monkeypatch):
    pid=add_source_photo();s.FILE_PATHS.clear();calls=[]
    html=f'<div data-post="catalog_fixture/101"><a class="tgme_widget_message_photo_wrap" href="{SOURCE}?single" style="background-image:url(\'{CDN}\')"></a></div>'
    def request(req):
        calls.append(str(req.url));assert str(req.url)==SOURCE+'?embed=1'
        return httpx.Response(200,text=html)
    original=httpx.AsyncClient
    monkeypatch.setattr(s.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(request),**kw))
    for suffix in ['','-thumb']:
        response=client.get('/media/'+pid+suffix+'.jpg',follow_redirects=False)
        assert response.status_code==302 and response.headers['location']==CDN
        assert s.TOKEN not in str(response.headers)
    assert len(calls)==1 and not list(s.DATA.rglob('*.jpg'))


@pytest.mark.parametrize('url',['https://evil.example/a.jpg','https://cdn4.telesco.pe.evil.example/file/a.jpg','https://cdn4.telesco.pe@127.0.0.1/file/a.jpg','http://cdn4.telesco.pe/file/a.jpg','https://api.telegram.org/file/botsecret/a.jpg'])
def test_other_image_hosts_are_rejected(url):
    assert not s.public_photo_url(url)


def test_unavailable_source_returns_clear_error(client,monkeypatch):
    pid=add_source_photo();s.FILE_PATHS.clear();original=httpx.AsyncClient
    monkeypatch.setattr(s.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='Message not found')),**kw))
    response=client.get('/media/'+pid+'.jpg',follow_redirects=False)
    assert response.status_code==502 and 'location' not in response.headers


@pytest.fixture
def source_transport(monkeypatch):
    monkeypatch.setattr(s,'FILE_PATHS',{})
    monkeypatch.setattr(s,'SOURCE_PHOTO_REQUESTS',asyncio.Semaphore(4))
    monkeypatch.setattr(s,'SOURCE_PHOTO_INFLIGHT',{})
    original=httpx.AsyncClient
    def install(handler):
        monkeypatch.setattr(s.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    return install


def album_html(source,messages):
    photos=''.join(f'<a class="tgme_widget_message_photo_wrap" href="{message}?single" style="background-image:url(\'{url}\')"></a>' for message,url in messages.items())
    return f'<div data-post="{source.removeprefix("https://t.me/")}">{photos}</div>'


def test_album_previews_share_one_inflight_request(source_transport):
    async def run():
        started=asyncio.Event();release=asyncio.Event();calls=[]
        messages={SOURCE:CDN,SOURCE[:-3]+'102':CDN.replace('fixture','second'),SOURCE[:-3]+'103':CDN.replace('fixture','third')}
        async def request(req):
            calls.append(str(req.url));started.set();await release.wait()
            return httpx.Response(200,text=album_html(SOURCE,messages))
        source_transport(request)
        tasks=[asyncio.create_task(s.source_photo_url({'source_post':SOURCE,'source_message':message})) for message in messages]
        await asyncio.wait_for(started.wait(),1)
        await asyncio.sleep(0)
        assert calls==[SOURCE+'?embed=1']
        release.set()
        assert await asyncio.wait_for(asyncio.gather(*tasks),1)==list(messages.values())
        assert not s.SOURCE_PHOTO_INFLIGHT
        assert await s.source_photo_url({'source_post':SOURCE,'source_message':SOURCE})==CDN
        assert len(calls)==1
    asyncio.run(run())


def test_source_request_limit_and_error_release(source_transport):
    async def run():
        saturated=asyncio.Event();release=asyncio.Event();calls={};active=0;peak=0
        async def request(req):
            nonlocal active,peak
            source=str(req.url).split('?')[0];calls[source]=calls.get(source,0)+1
            active+=1;peak=max(peak,active)
            if active==4:saturated.set()
            try:
                await release.wait()
                if source==SOURCE and calls[source]==1:raise httpx.ConnectError('temporary',request=req)
                return httpx.Response(200,text=album_html(source,{source:CDN}))
            finally:active-=1
        source_transport(request)
        sources=[SOURCE.rsplit('/',1)[0]+'/'+str(101+i) for i in range(8)]
        tasks=[asyncio.create_task(s.source_photo_url({'source_post':source,'source_message':source})) for source in sources]
        await asyncio.wait_for(saturated.wait(),1)
        await asyncio.sleep(0)
        assert sum(calls.values())==4 and active==4
        release.set()
        results=await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),1)
        assert isinstance(results[0],s.HTTPException) and results[0].status_code==502
        assert results[1:]==[CDN]*7
        assert peak==4 and active==0 and not s.SOURCE_PHOTO_INFLIGHT
        assert 'source:'+SOURCE not in s.FILE_PATHS
        assert await s.source_photo_url({'source_post':SOURCE,'source_message':SOURCE})==CDN
        assert calls[SOURCE]==2
    asyncio.run(run())


@pytest.mark.parametrize('failure',['missing','empty','invalid_utf8','http_error'])
def test_failed_album_load_is_shared_and_retryable(source_transport,failure):
    async def run():
        started=asyncio.Event();release=asyncio.Event();calls=[]
        async def request(req):
            calls.append(str(req.url));started.set();await release.wait()
            if len(calls)>1:return httpx.Response(200,text=album_html(SOURCE,{SOURCE:CDN}))
            if failure=='missing':return httpx.Response(200,text='Message not found')
            if failure=='empty':return httpx.Response(200,text=album_html(SOURCE,{}))
            if failure=='invalid_utf8':return httpx.Response(200,content=b'\xff')
            return httpx.Response(503)
        source_transport(request)
        photo={'source_post':SOURCE,'source_message':SOURCE}
        tasks=[asyncio.create_task(s.source_photo_url(photo)) for _ in range(3)]
        await asyncio.wait_for(started.wait(),1)
        await asyncio.sleep(0)
        assert len(calls)==1
        release.set()
        results=await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),1)
        assert all(isinstance(result,s.HTTPException) and result.status_code==502 for result in results)
        assert not s.SOURCE_PHOTO_INFLIGHT and not s.FILE_PATHS
        assert await s.source_photo_url(photo)==CDN and len(calls)==2
    asyncio.run(run())


def test_cancelled_preview_does_not_cancel_shared_load(source_transport):
    async def run():
        started=asyncio.Event();release=asyncio.Event();calls=[]
        async def request(req):
            calls.append(str(req.url));started.set();await release.wait()
            return httpx.Response(200,text=album_html(SOURCE,{SOURCE:CDN}))
        source_transport(request)
        photo={'source_post':SOURCE,'source_message':SOURCE}
        first=asyncio.create_task(s.source_photo_url(photo));second=asyncio.create_task(s.source_photo_url(photo))
        await asyncio.wait_for(started.wait(),1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):await first
        release.set()
        assert await asyncio.wait_for(second,1)==CDN
        assert len(calls)==1 and not s.SOURCE_PHOTO_INFLIGHT
    asyncio.run(run())


def test_source_album_cache_is_bounded_and_missing_photo_not_cached(source_transport):
    async def run():
        calls=[];message=SOURCE.rsplit('/',1)[0]+'/102'
        def request(req):
            calls.append(str(req.url))
            return httpx.Response(200,text=album_html(SOURCE,{message:CDN} if len(calls)==1 else {SOURCE:CDN}))
        source_transport(request)
        s.FILE_PATHS.update({str(i):('path',time.monotonic()+3000) for i in range(512)})
        assert await s.source_photo_url({'source_post':SOURCE,'source_message':message})==CDN
        assert len(s.FILE_PATHS)==512 and '0' not in s.FILE_PATHS
        with pytest.raises(s.HTTPException) as error:
            await s.source_photo_url({'source_post':SOURCE,'source_message':SOURCE})
        assert error.value.status_code==404 and 'source:'+SOURCE not in s.FILE_PATHS
        assert await s.source_photo_url({'source_post':SOURCE,'source_message':SOURCE})==CDN
        assert len(calls)==2 and len(s.FILE_PATHS)==512
    asyncio.run(run())
