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
