import asyncio
import io
import json

import pytest
from PIL import Image

import server as s
from test_server import body, client, signed


@pytest.mark.parametrize('role', ['owner', 'tenant', 'unknown'])
def test_paid_requires_agent(client, role):
    request = body(commission=50)
    request['listing']['role'] = role
    assert client.post('/api/listings', json=request, headers=signed()).status_code == 400


@pytest.mark.parametrize('fee,kind,accepted', [(50, 'percent', True), (101, 'percent', False), (80000, 'fixed', True), (-1, 'fixed', False)])
def test_commission_values_and_market(client, fee, kind, accepted):
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


def test_photo_preview_and_dimensions_are_server_owned(client):
    source = io.BytesIO()
    photo = Image.new('RGB', (2000, 1000), '#386e91')
    exif = Image.Exif()
    exif[274] = 6
    photo.save(source, format='JPEG', exif=exif)
    response = client.post('/api/photos', files={'file': ('test.jpg', source.getvalue(), 'image/jpeg')}, headers=signed())
    assert response.status_code == 200, response.text
    uploaded = response.json()
    assert (uploaded['width'], uploaded['height']) == (800, 1600)
    preview = client.get(uploaded['thumb_url'])
    assert preview.status_code == 200
    with Image.open(io.BytesIO(preview.content)) as thumbnail:
        assert thumbnail.size == (320, 640)
        assert not thumbnail.getexif()
    request = body()
    request['listing']['photos'] = [{**uploaded, 'width': 1, 'height': 1, 'thumb_url': 'https://untrusted.example/photo.jpg'}]
    listing = client.post('/api/listings', json=request, headers=signed()).json()
    assert listing['photos'][0] == uploaded
    assert client.get('/media/../private.key').status_code == 404


def test_subscriptions_keep_markets_separate(client):
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
