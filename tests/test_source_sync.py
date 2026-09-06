import asyncio,time
from datetime import datetime,timezone
from types import SimpleNamespace
import pytest
from telethon.tl import types
import server as s
from source_sync import SourceStore,SourceReader,normalize_message,reader_lock,source_config
from test_server import client

@pytest.fixture
def store(client):
    value=SourceStore(s.db);value.setup();value.begin(-1000000000123,{55:'free'},now=time.time());return value

def message(mid=1,**values):
    return {'channel':-1000000000123,'username':'source_fixture','id':mid,'topic':55,'market':'free','date':time.time()-100,'edited':None,'group':'','text':'Описание квартиры','author':42,'contact':'author_fixture','photo':None,**values}

def telegram_message(mid=1,**values):
    args={'id':mid,'peer_id':types.PeerChannel(123),'date':datetime.now(timezone.utc),'message':'Описание квартиры','from_id':types.PeerUser(42),'reply_to':types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=55)}
    args.update(values);m=types.Message(**args);m._sender=types.User(id=42,first_name='Author',username='author_fixture');return m

def test_dedup_album_edits_and_compact_snapshot(store):
    first=message(group='album');assert store.ingest(first)
    assert not store.ingest(first)
    store.ingest(message(2,group='album',text='',photo={'id':'photo1','width':800,'height':600}))
    old=store.snapshot('-1000000000123:galbum');assert old['root']['id']==1 and len(old['messages'])==2
    edited={**first,'text':'Новая цена','edited':time.time()};assert store.ingest(edited)
    latest=store.snapshot(old['key']);assert latest['revision']!=old['revision']
    assert not store.acknowledge(old,'published')
    assert store.acknowledge(latest,'review')
    assert store.pending(time.time()+3) is None

def test_late_history_cannot_restore_deletion_or_overwrite_edit(store):
    first=message();store.ingest(first)
    store.ingest({**first,'text':'Исправлено','edited':time.time()})
    assert not store.ingest(first,mode='backfill')
    store.delete(-1000000000123,[1,2]);assert not store.ingest(first)
    assert not store.ingest(message(2),mode='backfill')
    assert store.snapshot('-1000000000123:m1')['deleted']

def test_deleting_one_photo_and_deleting_root(store):
    store.ingest(message(group='album'))
    store.ingest(message(2,group='album',text=''))
    store.delete(-1000000000123,[2]);assert not store.snapshot('-1000000000123:galbum')['deleted']
    store.ingest(message(3,group='album',text=''))
    store.delete(-1000000000123,[1]);assert store.snapshot('-1000000000123:galbum')['deleted']

def test_stale_authoritative_response_does_not_undo_new_event(store):
    original=message();started=time.time();store.ingest(original,now=started+1)
    assert not store.ingest({**original,'text':'Устаревший ответ'},requested_at=started)
    assert not store.delete(-1000000000123,[1],requested_at=started)
    assert store.snapshot('-1000000000123:m1')['text']==original['text']

def test_only_price_and_address_history_kept_with_limit(store):
    with s.db() as c:
        for n in range(9):store.record_changes(c,'listing',{'address':str(n),'prices':[n],'description':'private old'}, {'address':str(n+1),'prices':[n+1],'description':'private new'},time.time())
        rows=c.execute('SELECT * FROM listing_changes').fetchall()
    assert len(rows)==5 and all('private' not in r['fields'] and 'description' not in r['fields'] for r in rows)

def test_normalize_topic_and_authentic_poster():
    msg=telegram_message(reply_to=types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=999,reply_to_top_id=55))
    d=normalize_message(msg,-1000000000123,'source_fixture',{55:'free'})
    assert d['topic']==55 and d['author']==42 and d['contact']=='author_fixture'
    assert normalize_message(msg,-1000000000123,'source_fixture',{66:'paid'}) is None
    msg.from_id=types.PeerChannel(999)
    d=normalize_message(msg,-1000000000123,'source_fixture',{55:'free'})
    assert d['author']==0 and d['contact']==''

def test_bootstrap_is_last_ten_days_then_old_tracked_edits_continue(store):
    now=time.time();cutoff=store.get('cutoff')
    class Fake:
        async def iter_messages(self,*args,**kwargs):
            yield telegram_message(30,date=datetime.fromtimestamp(now,timezone.utc))
            yield telegram_message(20,date=datetime.fromtimestamp(cutoff+1,timezone.utc))
            yield telegram_message(10,date=datetime.fromtimestamp(cutoff-1,timezone.utc))
    reader=SourceReader(store,Fake(),types.Channel(id=123,title='Source',photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),username='source_fixture'),{55:'free'})
    asyncio.run(reader.history(initial=True))
    assert store.get('history_complete') and store.get('history_cursor')==30
    with s.db() as c:assert [r[0] for r in c.execute('SELECT mid FROM source_messages ORDER BY mid')]==[20,30]
    store.set('cutoff',now+86400)
    assert reader.accept(telegram_message(20,date=datetime.fromtimestamp(cutoff+1,timezone.utc),edit_date=datetime.now(timezone.utc),message='Исправлено спустя месяц'))
    assert not reader.accept(telegram_message(9,date=datetime.fromtimestamp(cutoff-100,timezone.utc)))

@pytest.mark.parametrize('failure',['short','order','exception'])
def test_reconciliation_errors_never_delete_ads(store,failure):
    store.ingest(message(),now=time.time()-600);store.ingest(message(2),now=time.time()-600)
    class Fake:
        async def get_messages(self,*args,**kwargs):
            if failure=='exception':raise ConnectionError()
            return [None] if failure=='short' else [None,telegram_message(999)]
    reader=SourceReader(store,Fake(),types.Channel(id=123,title='Source',photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),username='source_fixture'),{55:'free'})
    with pytest.raises((ConnectionError,ValueError)):asyncio.run(reader.reconcile())
    assert not store.snapshot('-1000000000123:m1')['deleted'] and not store.snapshot('-1000000000123:m2')['deleted']

def test_reconcile_confirms_edits_deletes_and_ignores_other_chats(store):
    store.ingest(message(),now=time.time()-600);store.ingest(message(2),now=time.time()-600)
    class Fake:
        async def get_messages(self,*args,**kwargs):return [None,telegram_message(2,message='Цена изменилась',edit_date=datetime.now(timezone.utc))]
    reader=SourceReader(store,Fake(),types.Channel(id=123,title='Source',photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),username='source_fixture'),{55:'free'})
    asyncio.run(reader.delete_event(SimpleNamespace(chat_id=-1000000000999,deleted_ids=[2])))
    assert not store.snapshot('-1000000000123:m2')['deleted']
    asyncio.run(reader.reconcile())
    assert store.snapshot('-1000000000123:m1')['deleted']
    assert store.snapshot('-1000000000123:m2')['text']=='Цена изменилась'

def test_source_scope_cannot_silently_change(store,monkeypatch):
    with pytest.raises(ValueError):store.begin(-1000000000999,{55:'free'})
    monkeypatch.setenv('TELEGRAM_SOURCE','source_fixture');monkeypatch.setenv('TELEGRAM_TOPICS','{"55":"free","56":"paid"}')
    assert source_config()==('source_fixture',{55:'free',56:'paid'})
    monkeypatch.setenv('TELEGRAM_TOPICS','{"0":"free","56":"paid"}')
    with pytest.raises(ValueError):source_config()

def test_only_one_reader_uses_session(tmp_path):
    with reader_lock(tmp_path/'reader.lock'):
        with pytest.raises(OSError):
            with reader_lock(tmp_path/'reader.lock'):pass
    with reader_lock(tmp_path/'reader.lock'):pass

@pytest.fixture
def catalog(store):
    store.set('source_username','source_fixture')
    return s.SOURCE_CATALOG

def fields(amount=300000,address='Улица 10'):
    return {'address':address,'city':'Ереван','kind':'apartment','rooms':2,'prices':[{'amount':amount,'currency':'AMD','period':'month'}],'commission':0,'role':'unknown'}

def publish_source(store,catalog,mid=1,**attrs):
    m=message(mid,**attrs);store.ingest(m);snap=store.snapshot(f"-1000000000123:m{mid}")
    lid=catalog.apply(snap,fields());return lid,m

def test_staging_and_atomic_cutover_preserves_native_and_other_source(client,store,catalog):
    from test_server import create
    native=create(client);other=create(client,address='Другая 20');legacy=create(client,address='Каталог 30')
    with s.db() as c:
        for lid,name in [(legacy['id'],'source_fixture'),(other['id'],'another_fixture')]:
            d=__import__('json').loads(s.getrow(lid)['payload']);d['_source_post']={'chat':{'username':name,'type':'supergroup'},'message_id':123}
            c.execute('UPDATE listings SET payload=? WHERE id=?',(s.dumps(d),lid))
    lid,m=publish_source(store,catalog)
    assert s.getrow(lid)['status']=='source_staged'
    assert client.get('/api/listings/'+lid).status_code==404
    with pytest.raises(ValueError):catalog.activate()
    store.set('history_complete',True)
    result=catalog.activate();assert result=={'removed':1,'activated':1}
    assert s.getrow(native['id']) and s.getrow(other['id']) and s.getrow(lid)['status']=='active'
    assert client.get('/api/listings/'+legacy['id']).status_code==404
    with s.db() as c:assert not c.execute("SELECT 1 FROM jobs WHERE json_extract(payload,'$.id')=?",(lid,)).fetchone()

def test_edit_hold_history_original_date_and_stale_review(client,store,catalog):
    lid,m=publish_source(store,catalog);store.set('history_complete',True);catalog.activate()
    created=s.getrow(lid)['created'];old=store.snapshot('-1000000000123:m1')
    store.ingest({**m,'text':'Новая цена и адрес','edited':time.time()});snap=store.snapshot(old['key']);catalog.hold(snap)
    assert client.get('/api/listings/'+lid).status_code==404
    with pytest.raises(ValueError):catalog.apply(old,fields(200000))
    catalog.apply(snap,fields(250000,'Улица 11'))
    public=client.get('/api/listings/'+lid).json()
    assert public['prices'][0]['amount']==250000 and s.getrow(lid)['created']==created
    assert set(public['history'][0]['fields'])=={'prices','address'}
    assert not any(k.startswith('_source') for k in public)

def test_source_edit_and_delete_do_not_lift_ban(client,store,catalog):
    from test_server import signed
    lid,m=publish_source(store,catalog);store.set('history_complete',True);catalog.activate()
    client.post('/api/admin/'+lid+'/ban',headers=signed(99),json={'reason':'Нарушение правил'})
    store.ingest({**m,'text':'Изменение','edited':time.time()});catalog.apply(store.snapshot('-1000000000123:m1'),fields(220000))
    assert s.getrow(lid)['status']=='banned' and s.getrow(lid)['reason']=='Нарушение правил'
    store.delete(m['channel'],[m['id']]);catalog.hold(store.snapshot('-1000000000123:m1'),'deleted')
    client.post('/api/admin/'+lid+'/unban',headers=signed(99),json={})
    assert s.getrow(lid)['status']=='source_deleted'
    assert client.post('/api/listings/'+lid+'/status',headers=signed(42),json={'status':'active'}).status_code==403

def test_edit_keeps_rented_and_changed_address_clears_verification(client,store,catalog):
    from test_server import signed
    lid,m=publish_source(store,catalog);store.set('history_complete',True);catalog.activate()
    client.post('/api/listings/'+lid+'/status',headers=signed(),json={'status':'rented'})
    with s.db() as c:
        d=__import__('json').loads(s.getrow(lid)['payload']);d['document_status']='owner_verified';d['verification']={'object_matches':True}
        c.execute('UPDATE listings SET payload=?,private=?,private_expires=? WHERE id=?',(s.dumps(d),b'private',time.time()+10,lid))
    store.ingest({**m,'text':'Другой адрес','edited':time.time()});snap=store.snapshot('-1000000000123:m1');catalog.hold(snap);catalog.apply(snap,fields(address='Улица 99'))
    r=s.getrow(lid);assert r['status']=='rented' and not r['private']
    d=s.listing(r);assert d['document_status']=='none' and 'verification' not in d

def test_import_api_auth_current_revision_and_explicit_paid_fee(client,store,catalog):
    from test_server import signed
    store.ingest(message(market='paid'));snap=store.snapshot('-1000000000123:m1');catalog.hold(snap)
    url='/api/admin/source/posts/'+snap['key']
    assert client.get('/api/admin/source').status_code==401
    assert client.get('/api/admin/source/posts',headers=signed(42)).status_code==403
    assert client.post(url,headers=signed(42),json={'revision':snap['revision'],'fields':fields()}).status_code==403
    assert client.post(url,headers=signed(99),json={'revision':snap['revision'],'fields':fields()}).status_code==400
    good={**fields(),'role':'agent','commission':50}
    assert client.post(url,headers=signed(99),json={'revision':'a'*64,'fields':good}).status_code==409
    assert client.post(url,headers=signed(99),json={'revision':snap['revision'],'fields':good}).status_code==200
    with s.db() as c:row=c.execute("SELECT * FROM listings WHERE status='source_staged'").fetchone()
    assert client.post('/api/admin/'+row['id']+'/decision',headers=signed(99),json={'decision':'approve'}).status_code==409

def test_photo_only_change_keeps_fields_and_deleted_photo_is_not_public(client,store,catalog):
    store.set('activated',True)
    lid,m=publish_source(store,catalog,photo={'id':'one','width':800,'height':600})
    old=s.listing(s.getrow(lid));pid=old['photos'][0]['id']
    store.ingest({**m,'photo':{'id':'two','width':900,'height':700},'edited':time.time()},now=time.time()-3)
    assert catalog.process_one()
    latest=s.listing(s.getrow(lid));assert latest['prices']==old['prices'] and latest['photos'][0]['id']!=pid
    assert client.get('/media/'+pid+'.jpg').status_code==404
