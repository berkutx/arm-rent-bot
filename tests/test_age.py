"""Regression checks for age-only ranking. Telegram transport is mocked."""
import asyncio, json, time
import server as s
from test_server import client, create, signed


def set_age(lid, days):
    with s.db() as c:
        c.execute('UPDATE listings SET created=?,confirmed=NULL WHERE id=?',(time.time()-days*86400,lid))


def test_no_expiry_fields_in_public_listing(client):
    l=create(client)
    assert not {'confirmed_at','expires_at','confirmation_by'} & l.keys()


def test_old_listings_stay_visible_and_contactable(client):
    for days in (1,2,3,30):
        l=create(client,address=f'Тестовая {days}');set_age(l['id'],days)
        r=client.get('/api/listings/'+l['id']).json()
        assert r['status']=='active'
        assert r['contact']=='@test_user'
    assert len(client.get('/api/listings').json())==4


def test_sorted_by_original_publication(client):
    old=create(client,address='Старая 10');set_age(old['id'],10)
    new=create(client,address='Новая 1');set_age(new['id'],1)
    medium=create(client,address='Средняя 2');set_age(medium['id'],2)
    # A historic v3 confirmation must not bump an old listing.
    with s.db() as c:c.execute('UPDATE listings SET confirmed=? WHERE id=?',(time.time(),old['id']))
    assert [x['id'] for x in client.get('/api/listings').json()]==[new['id'],medium['id'],old['id']]


def test_no_renewal_jobs_or_auto_hiding(client):
    l=create(client);set_age(l['id'],10)
    asyncio.run(s.maintenance());asyncio.run(s.maintenance())
    assert s.getrow(l['id'])['status']=='active'
    with s.db() as c:assert c.execute("SELECT count(*) FROM jobs WHERE kind='reminder'").fetchone()[0]==0


def test_expired_v3_jobs_cannot_send_messages(client,monkeypatch):
    l=create(client);calls=[]
    async def fake(method,payload):calls.append((method,payload));return {}
    monkeypatch.setattr(s,'tg',fake)
    asyncio.run(s.process_job({'kind':'reminder','payload':json.dumps({'id':l['id']})}))
    assert not calls


def test_migration_restores_only_auto_hidden_without_redating(client):
    old=create(client,address='Скрытая 1');set_age(old['id'],7)
    rented=create(client,address='Сданная 2')
    client.post('/api/listings/'+rented['id']+'/status',json={'status':'rented'},headers=signed())
    created=s.getrow(old['id'])['created']
    with s.db() as c:
        c.execute("UPDATE listings SET status='stale' WHERE id=?",(old['id'],))
        c.execute("DELETE FROM meta WHERE key='age_only_v4'")
    s.enqueue('reminder',{'id':old['id']},'old-reminder')
    s.setup();s.setup()
    assert s.getrow(old['id'])['status']=='active'
    assert s.getrow(old['id'])['created']==created
    assert s.getrow(rented['id'])['status']=='rented'
    with s.db() as c:assert c.execute("SELECT status FROM jobs WHERE jobkey='old-reminder'").fetchone()[0]=='cancelled'


def test_manual_rented_hides_and_keeps_date(client):
    l=create(client);set_age(l['id'],4)
    before=client.get('/api/listings/'+l['id']).json()['created_at']
    r=client.post('/api/listings/'+l['id']+'/status',json={'status':'rented'},headers=signed()).json()
    assert r['status']=='rented' and r['created_at']==before
    assert client.get('/api/listings').json()==[]
    assert not s.keyboard(r)['inline_keyboard']


def test_late_moderation_does_not_expire_or_redate(client):
    l=create(client);set_age(l['id'],4)
    with s.db() as c:c.execute("UPDATE listings SET status='review' WHERE id=?",(l['id'],))
    before=s.getrow(l['id'])['created']
    client.post('/api/admin/'+l['id']+'/decision',json={'decision':'approve'},headers=signed(99))
    assert s.getrow(l['id'])['created']==before
    assert client.get('/api/listings').json()[0]['status']=='active'


def test_bot_text_uses_absolute_creation_not_confirmation(client):
    l=create(client)
    assert 'Опубликовано ' in s.public_text(l)
    assert 'подтвердил' not in s.public_text(l)
    assert 'fav:' not in json.dumps(s.keyboard(l))


def test_legacy_renew_button_is_noop(client,monkeypatch):
    l=create(client);before=s.getrow(l['id'])['created'];calls=[]
    async def fake(method,payload):calls.append((method,payload));return {}
    monkeypatch.setattr(s,'tg',fake)
    asyncio.run(s.receive({'callback_query':{'id':'old-button','from':{'id':42},'data':'still:'+l['id']}}))
    assert s.getrow(l['id'])['created']==before
    assert all(method=='answerCallbackQuery' for method,payload in calls)
    assert 'Продлевать не нужно' in calls[-1][1]['text']
