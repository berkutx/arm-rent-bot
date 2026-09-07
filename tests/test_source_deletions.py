import asyncio,json,time
from datetime import datetime,timezone
from types import SimpleNamespace

import pytest
from telethon.tl import types
import server as s
import source_sync as sync
from test_server import client,create,signed
from test_source_sync import store,catalog,message,telegram_message,fields


CHANNEL=-1000000000123


def entity():
    return types.Channel(id=123,title='Source',photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),username='source_fixture',left=False)


def publish(store,catalog,mid=1,**attrs):
    item=message(mid,**attrs);store.ingest(item)
    key=str(CHANNEL)+(':'+('g'+str(item['group']) if item.get('group') else 'm'+str(mid)))
    store.set('activated',True)
    return catalog.apply(store.snapshot(key),fields()),key


def full_channel(**values):
    fields={'id':123,'about':'','read_inbox_max_id':0,'read_outbox_max_id':0,'unread_count':0,
            'chat_photo':types.PhotoEmpty(id=0),'notify_settings':types.PeerNotifySettings(),'bot_info':[],'pts':0}
    fields.update(values)
    return types.ChannelFull(**fields)


class Fake:
    def __init__(self,missing=(),respond=None):self.missing=set(missing);self.respond=respond;self.requests=[]
    async def __call__(self,request):
        from telethon.tl.functions.channels import GetFullChannelRequest
        assert isinstance(request,GetFullChannelRequest) and request.channel.id==123
        return SimpleNamespace(full_chat=full_channel())
    async def get_messages(self,channel,ids):
        self.requests.append(list(ids))
        if self.respond:return self.respond(len(self.requests),ids)
        return [None if mid in self.missing else telegram_message(mid,message='Edited text must not be imported') for mid in ids]


def state():
    with s.db() as c:return '\n'.join(c.iterdump())


def run(catalog,fake,apply=True):
    return asyncio.run(sync.SourceDeletionChecker(catalog,fake,entity(),request_interval=0).check(apply=apply))


def test_dry_run_preserves_every_database_row(store,catalog):
    lid,key=publish(store,catalog);publish(store,catalog,2)
    before=state();fake=Fake({1})
    result=run(catalog,fake,apply=False)
    assert result['hidden_listings']==1 and result['deleted_messages']==1
    assert result['deleted_urls']==['https://t.me/source_fixture/1']
    assert fake.requests==[[1,2],[1,2]] and state()==before


def test_only_imported_known_ids_checked_no_edits_or_messages(client,store,catalog):
    native=create(client);lid,key=publish(store,catalog);other,other_key=publish(store,catalog,2)
    store.ingest(message(999),now=time.time()-600)
    before_native=dict(s.getrow(native['id']));before_other=dict(s.getrow(other));before_text=store.snapshot(other_key)['text']
    with s.db() as c:jobs=[tuple(r) for r in c.execute('SELECT * FROM jobs')]
    fake=Fake({1});result=run(catalog,fake)
    assert result['checked_messages']==2 and all(999 not in batch for batch in fake.requests)
    assert s.getrow(lid)['status']=='source_deleted' and client.get('/api/listings/'+lid).status_code==404
    assert dict(s.getrow(native['id']))==before_native and dict(s.getrow(other))==before_other
    assert store.snapshot(other_key)['text']==before_text
    with s.db() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM jobs')]==jobs
        assert c.execute('SELECT 1 FROM source_dirty WHERE post_key=?',(str(CHANNEL)+':m999',)).fetchone()


def test_photo_deletion_preserves_listing_fields_dates_and_rented_status(client,store,catalog):
    root=message(1,group='album',photo={'id':'root','width':800,'height':600})
    extra=message(2,group='album',text='',photo={'id':'extra','width':800,'height':600})
    store.ingest(root);store.ingest(extra);store.set('activated',True);key=str(CHANNEL)+':galbum'
    lid=catalog.apply(store.snapshot(key),fields());publish(store,catalog,3)
    with s.db() as c:c.execute("UPDATE listings SET status='rented',view_count=17 WHERE id=?",(lid,))
    before=dict(s.getrow(lid));payload=json.loads(before['payload']);removed=payload['photos'][1]['id']
    result=run(catalog,Fake({2}));after=dict(s.getrow(lid));actual=json.loads(after.pop('payload'));before.pop('payload')
    assert after==before and result['updated_albums']==1 and result['hidden_listings']==0
    for field in set(payload)-{'photos','photo_count','source_revision'}:assert actual[field]==payload[field]
    assert actual['photos']==payload['photos'][:1] and actual['photo_count']==1
    assert client.get('/media/'+removed+'.jpg').status_code==404
    assert catalog.previous(key)['state']=='published'
    unchanged=dict(s.getrow(lid));again=run(catalog,Fake({2}))
    assert again['deleted_messages']==0 and dict(s.getrow(lid))==unchanged


@pytest.mark.parametrize('status',['active','rented','banned','review'])
def test_caption_deletion_hides_even_with_remaining_album_photos_and_preserves_ban(client,store,catalog,status):
    store.ingest(message(1,group='album'));store.ingest(message(2,group='album',text='',photo={'id':'extra','width':800,'height':600}))
    store.set('activated',True);key=str(CHANNEL)+':galbum';lid=catalog.apply(store.snapshot(key),fields())
    with s.db() as c:c.execute('UPDATE listings SET status=?,reason=?,view_count=11 WHERE id=?',(status,'Original reason',lid))
    before=s.getrow(lid);run(catalog,Fake({1}));after=s.getrow(lid)
    assert after['status']==('banned' if status=='banned' else 'source_deleted')
    assert after['created']==before['created'] and after['view_count']==11
    assert json.loads(after['payload'])['_source_deleted'] is True
    if status=='banned':
        assert after['reason']=='Original reason'
        client.post('/api/admin/'+lid+'/unban',headers=signed(99),json={})
        assert s.getrow(lid)['status']=='source_deleted'


@pytest.mark.parametrize('failure',['all_empty','short','wrong_id','wrong_peer','exception','confirmation_exception','control_lost'])
def test_uncertain_results_never_mutate_catalog(store,catalog,failure):
    publish(store,catalog);publish(store,catalog,2);before=state()
    def respond(call,ids):
        if failure=='all_empty':return [None for _ in ids]
        if failure=='short':return [None]
        if failure=='wrong_id':return [None,telegram_message(987)]
        if failure=='wrong_peer':return [None,telegram_message(2,peer_id=types.PeerChannel(999))]
        if failure=='exception' or (failure=='confirmation_exception' and call==2):raise ConnectionError('Do not print session details')
        if failure=='control_lost' and call==2:return [None,None]
        return [None,telegram_message(2)]
    with pytest.raises((sync.SourceDeletionError,ConnectionError)):run(catalog,Fake(respond=respond))
    assert state()==before


def test_last_partial_batch_can_be_empty_when_same_channel_is_accessible(store,catalog):
    for mid in range(1,102):store.ingest(message(mid,group='album',text='Caption' if mid==1 else ''))
    store.set('activated',True);key=str(CHANNEL)+':galbum';catalog.apply(store.snapshot(key),fields())
    fake=Fake({101});result=run(catalog,fake)
    assert result['deleted_messages']==1 and result['hidden_listings']==0
    assert fake.requests[1]==[101] and fake.requests[2]==[101,1]


def test_message_empty_is_supported_and_transient_missing_does_not_delete(store,catalog):
    lid,key=publish(store,catalog);publish(store,catalog,2)
    def respond(call,ids):
        return [types.MessageEmpty(id=1,peer_id=types.PeerChannel(123)),telegram_message(2)] if call==1 else [telegram_message(1),telegram_message(2)]
    result=run(catalog,Fake(respond=respond))
    assert result['deleted_messages']==0 and result['unconfirmed_messages']==1 and s.getrow(lid)['status']=='active'


def test_concurrent_source_change_rolls_back_deletions(store,catalog):
    first,key=publish(store,catalog);second,key2=publish(store,catalog,2)
    def respond(call,ids):
        if call==2:store.ingest(message(2,text='Concurrent edit',edited=time.time()))
        return [None,telegram_message(2)]
    with pytest.raises(sync.SourceDeletionError):run(catalog,Fake(respond=respond))
    assert s.getrow(first)['status']=='active' and s.getrow(second)['status']=='active'
    assert not store.snapshot(key)['deleted']


def test_pending_unreviewed_changes_are_not_consumed(store,catalog):
    lid,key=publish(store,catalog);publish(store,catalog,2)
    store.ingest(message(1,text='Unreviewed edit',edited=time.time()));before=state()
    with pytest.raises(sync.SourceDeletionError):run(catalog,Fake({1}))
    assert state()==before


def configure(monkeypatch,store):
    for key,value in {'TELEGRAM_SYNC_ENABLED':'0','TELEGRAM_SOURCE':'source_fixture','TELEGRAM_TOPICS':'{"55":"free"}',
                      'TELEGRAM_READER_USER_ID':'42','TELEGRAM_API_ID':'123','TELEGRAM_API_HASH':'a'*32}.items():monkeypatch.setenv(key,value)
    (s.DATA/'telegram-reader.session').touch()


@pytest.mark.parametrize('failure',['wrong_account','bot_account','left','wrong_channel','unauthorized'])
def test_existing_session_identity_and_access_required(store,catalog,monkeypatch,failure):
    publish(store,catalog);configure(monkeypatch,store);before=state();instances=[]
    class Client(Fake):
        def __init__(self,*args,**kwargs):
            super().__init__();self.session=SimpleNamespace();self.disconnected=False;instances.append(self)
            assert kwargs['receive_updates'] is False and kwargs['catch_up'] is False
        async def connect(self):pass
        async def disconnect(self):self.disconnected=True
        async def is_user_authorized(self):return failure!='unauthorized'
        async def get_me(self):return SimpleNamespace(id=99 if failure=='wrong_account' else 42,bot=failure=='bot_account',username='reader')
        async def get_entity(self,source):
            value=entity()
            if failure=='left':value.left=True
            if failure=='wrong_channel':value.id=999
            return value
    monkeypatch.setattr('telethon.TelegramClient',Client)
    with pytest.raises(sync.SourceDeletionError):asyncio.run(sync.check_source_deletions(catalog,s.DATA,apply=True,request_interval=0))
    assert state()==before and instances[0].disconnected and not instances[0].requests


def test_periodic_checker_runs_immediately_and_records_safe_health(store,catalog,monkeypatch):
    calls=[];delays=[]
    async def check(*args,**kwargs):
        calls.append(kwargs);return {'checked_posts':2,'checked_messages':4,'deleted_messages':1,'account':{'id':42},'deleted_urls':['private-url']}
    async def sleep(delay):delays.append(delay);raise asyncio.CancelledError()
    monkeypatch.setattr(sync,'check_source_deletions',check);monkeypatch.setattr(sync.asyncio,'sleep',sleep)
    with pytest.raises(asyncio.CancelledError):asyncio.run(sync.run_deletion_checks(catalog,s.DATA,900))
    health=store.get('deletion_check')
    assert calls==[{'apply':True}] and delays==[900] and health['state']=='ok'
    assert health['deleted_messages']==1 and 'account' not in health and 'deleted_urls' not in health


def test_empty_message_with_wrong_peer_cannot_delete(store,catalog):
    publish(store,catalog);publish(store,catalog,2);before=state()
    fake=Fake(respond=lambda call,ids:[types.MessageEmpty(id=1,peer_id=types.PeerChannel(999)),telegram_message(2)])
    with pytest.raises(sync.SourceDeletionError):run(catalog,fake)
    assert state()==before


def test_other_source_import_is_never_requested_or_modified(store,catalog):
    lid,key=publish(store,catalog);publish(store,catalog,2)
    item=message(50,channel=-1000000000999,username='another_fixture');store.ingest(item)
    other_key='-1000000000999:m50';other=catalog.apply(store.snapshot(other_key),fields())
    before=dict(s.getrow(other));fake=Fake({1});run(catalog,fake)
    assert all(50 not in batch for batch in fake.requests) and dict(s.getrow(other))==before
    assert s.getrow(lid)['status']=='source_deleted'


def test_full_reader_or_busy_session_blocks_deletion_client(store,catalog,monkeypatch):
    publish(store,catalog);configure(monkeypatch,store)
    def forbidden(*args,**kwargs):raise AssertionError('A second Telegram client was created')
    monkeypatch.setattr('telethon.TelegramClient',forbidden)
    monkeypatch.setenv('TELEGRAM_SYNC_ENABLED','1')
    with pytest.raises(sync.SourceDeletionError):asyncio.run(sync.check_source_deletions(catalog,s.DATA,apply=True,request_interval=0))
    monkeypatch.setenv('TELEGRAM_SYNC_ENABLED','0')
    with sync.reader_lock(s.DATA/'telegram-reader.lock'):
        with pytest.raises(OSError):asyncio.run(sync.check_source_deletions(catalog,s.DATA,apply=True,request_interval=0))


def test_periodic_errors_keep_catalog_and_do_not_expose_error_details(store,catalog,monkeypatch):
    lid,key=publish(store,catalog);before=dict(s.getrow(lid));delays=[]
    async def check(*args,**kwargs):raise ConnectionError('secret-account-details')
    async def sleep(delay):delays.append(delay);raise asyncio.CancelledError()
    monkeypatch.setattr(sync,'check_source_deletions',check);monkeypatch.setattr(sync.asyncio,'sleep',sleep)
    with pytest.raises(asyncio.CancelledError):asyncio.run(sync.run_deletion_checks(catalog,s.DATA,900))
    health=store.get('deletion_check')
    assert health['state']=='error' and health['error']=='ConnectionError' and delays==[900]
    assert 'secret-account-details' not in json.dumps(health) and dict(s.getrow(lid))==before


@pytest.mark.parametrize('full',[
    full_channel(hidden_prehistory=True),
    full_channel(id=999),
    full_channel(available_min_id=10),
    full_channel(available_min_id=11),
    SimpleNamespace(id=123,hidden_prehistory=False,available_min_id=None),
])
def test_hidden_or_unavailable_old_history_cannot_look_like_deletion(store,catalog,full):
    publish(store,catalog,10);publish(store,catalog,20);before=state()
    class HistoryClient(Fake):
        async def __call__(self,request):return SimpleNamespace(full_chat=full)
    fake=HistoryClient({10})
    with pytest.raises(sync.SourceDeletionError):run(catalog,fake)
    assert state()==before and fake.requests==[]


@pytest.mark.parametrize('minimum',[None,0,9])
def test_available_minimum_is_inclusive_and_next_message_can_be_checked(store,catalog,minimum):
    lid,key=publish(store,catalog,10);publish(store,catalog,20)
    class HistoryClient(Fake):
        async def __call__(self,request):return SimpleNamespace(full_chat=full_channel(available_min_id=minimum))
    report=run(catalog,HistoryClient({10}))
    assert report['deleted_messages']==1 and s.getrow(lid)['status']=='source_deleted'


@pytest.mark.parametrize('failure',['hidden','minimum','exception'])
def test_history_access_change_during_confirmation_aborts_all_mutations(store,catalog,failure):
    publish(store,catalog,10);publish(store,catalog,20);before=state()
    class HistoryClient(Fake):
        full_requests=0
        async def __call__(self,request):
            self.full_requests+=1
            if self.full_requests==2:
                if failure=='exception':raise ConnectionError('Access check failed')
                return SimpleNamespace(full_chat=full_channel(hidden_prehistory=failure=='hidden',available_min_id=10 if failure=='minimum' else None))
            return SimpleNamespace(full_chat=full_channel())
    fake=HistoryClient({10})
    with pytest.raises((sync.SourceDeletionError,ConnectionError)):run(catalog,fake)
    assert fake.full_requests==2 and len(fake.requests)==2 and state()==before



def flood_wait(seconds=17,request_type='messages'):
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.channels import GetMessagesRequest
    from telethon.tl.functions.contacts import ResolveUsernameRequest
    request=(GetMessagesRequest(types.InputChannel(123,98765432123456789),[])
             if request_type=='messages' else ResolveUsernameRequest('private_source_name'))
    return FloodWaitError(request,capture=seconds)


def test_real_check_paces_metadata_batches_and_confirmation(store,catalog,monkeypatch):
    for mid in range(1,102):store.ingest(message(mid,group='album',text='Caption' if mid==1 else ''))
    store.set('activated',True);catalog.apply(store.snapshot(str(CHANNEL)+':galbum'),fields())
    clock=[0.0];delays=[];calls=[]
    async def sleep(delay):delays.append(delay);clock[0]+=delay
    monkeypatch.setattr(sync.time,'monotonic',lambda:clock[0]);monkeypatch.setattr(sync.asyncio,'sleep',sleep)
    class PacedClient(Fake):
        async def __call__(self,request):
            calls.append(clock[0]);return await super().__call__(request)
        async def get_messages(self,channel,ids):
            calls.append(clock[0]);return await super().get_messages(channel,ids)
    result=asyncio.run(sync.SourceDeletionChecker(catalog,PacedClient({101}),entity()).check())
    assert result['deleted_messages']==1 and calls==[0,1,2,3,4] and delays==[1,1,1,1]


@pytest.mark.parametrize('request_type',['messages','username'])
def test_flood_wait_persists_and_prevents_reconnecting_before_deadline(store,catalog,monkeypatch,request_type):
    lid,key=publish(store,catalog);configure(monkeypatch,store);before=dict(s.getrow(lid));instances=[]
    error=flood_wait(3600,request_type)
    class Client(Fake):
        def __init__(self,*args,**kwargs):
            super().__init__();self.session=SimpleNamespace();self.disconnected=False;instances.append(self)
        async def connect(self):pass
        async def disconnect(self):self.disconnected=True
        async def is_user_authorized(self):return True
        async def get_me(self):return SimpleNamespace(id=42,bot=False,username='reader')
        async def get_entity(self,source):
            if request_type=='username':raise error
            return entity()
        async def get_messages(self,channel,ids):raise error
    monkeypatch.setattr('telethon.TelegramClient',Client)
    with pytest.raises(type(error)):asyncio.run(sync.check_source_deletions(catalog,s.DATA,request_interval=0))
    health=store.get('deletion_check')
    assert health['retry_after']==3600 and health['retry_at']>time.time()+3599
    assert health['request_type']==type(error.request).__name__ and instances[0].disconnected
    def forbidden(*args,**kwargs):raise AssertionError('Telegram was contacted during FloodWait')
    monkeypatch.setattr('telethon.TelegramClient',forbidden)
    with pytest.raises(sync.SourceDeletionRetryLater) as caught:
        asyncio.run(sync.check_source_deletions(catalog,s.DATA,request_interval=0))
    assert caught.value.seconds>=3600 and store.get('deletion_check')==health
    assert dict(s.getrow(lid))==before and not store.snapshot(key)['deleted']
    assert 'private_source_name' not in json.dumps(health) and '98765432123456789' not in json.dumps(health)


def test_cli_reports_only_flood_seconds_and_request_class(store,catalog,monkeypatch):
    import io,sys
    from scripts import check_source_deletions as cli
    output=io.StringIO()
    async def check(*args,**kwargs):raise flood_wait()
    monkeypatch.setattr(sync,'check_source_deletions',check)
    monkeypatch.setattr(sys,'argv',['check_source_deletions.py','--dry-run']);monkeypatch.setattr(sys,'stdout',output)
    with pytest.raises(SystemExit) as caught:cli.main()
    assert caught.value.code==1
    assert json.loads(output.getvalue())=={'ok':False,'error':'FloodWaitError','reason':'Telegram source check failed',
                                         'retry_after':17,'request_type':'GetMessagesRequest'}


def test_periodic_restart_honors_persisted_wait_without_telegram_call(store,catalog,monkeypatch):
    health={'state':'error','at':time.time(),'retry_at':time.time()+1800,'error':'FloodWaitError','retry_after':1800}
    store.set('deletion_check',health);delays=[]
    async def forbidden(*args,**kwargs):raise AssertionError('Telegram was contacted before persisted deadline')
    async def sleep(delay):delays.append(delay);raise asyncio.CancelledError()
    monkeypatch.setattr(sync,'check_source_deletions',forbidden);monkeypatch.setattr(sync.asyncio,'sleep',sleep)
    with pytest.raises(asyncio.CancelledError):asyncio.run(sync.run_deletion_checks(catalog,s.DATA,900))
    assert len(delays)==1 and 1799<=delays[0]<=1800 and store.get('deletion_check')==health


def test_periodic_flood_wait_is_saved_and_longer_than_regular_interval(store,catalog,monkeypatch):
    delays=[]
    async def check(*args,**kwargs):raise flood_wait(3600)
    async def sleep(delay):delays.append(delay);raise asyncio.CancelledError()
    monkeypatch.setattr(sync,'check_source_deletions',check);monkeypatch.setattr(sync.asyncio,'sleep',sleep)
    with pytest.raises(asyncio.CancelledError):asyncio.run(sync.run_deletion_checks(catalog,s.DATA,900))
    health=store.get('deletion_check')
    assert delays==[3601] and health['retry_after']==3600 and health['request_type']=='GetMessagesRequest'
    assert health['retry_at']>time.time()+3599


def test_expired_wait_allows_periodic_check_and_clears_retry_state(store,catalog,monkeypatch):
    store.set('deletion_check',{'state':'error','retry_at':time.time()-1,'error':'FloodWaitError'});calls=[]
    async def check(*args,**kwargs):calls.append(True);return {'checked_messages':2,'deleted_messages':0}
    async def sleep(delay):raise asyncio.CancelledError()
    monkeypatch.setattr(sync,'check_source_deletions',check);monkeypatch.setattr(sync.asyncio,'sleep',sleep)
    with pytest.raises(asyncio.CancelledError):asyncio.run(sync.run_deletion_checks(catalog,s.DATA,900))
    assert calls==[True] and store.get('deletion_check')['state']=='ok' and 'retry_at' not in store.get('deletion_check')



@pytest.mark.parametrize('failure',['last_batch','final_metadata'])
def test_short_flood_retries_only_failed_rpc_and_saves_wait_first(store,catalog,monkeypatch,failure):
    for mid in range(1,102):store.ingest(message(mid,group='album',text='Caption' if mid==1 else ''))
    store.set('activated',True);catalog.apply(store.snapshot(str(CHANNEL)+':galbum'),fields())
    clock=[1000.0];delays=[]
    async def sleep(delay):
        if delay==29:
            assert store.get('deletion_check')['retry_at']==clock[0]+29
            assert store.get('deletion_check')['retry_after']==28
        delays.append(delay);clock[0]+=delay
    monkeypatch.setattr(sync.time,'monotonic',lambda:clock[0]);monkeypatch.setattr(sync.time,'time',lambda:clock[0])
    monkeypatch.setattr(sync.asyncio,'sleep',sleep)
    class RetryClient(Fake):
        metadata=0;failed=False
        async def __call__(self,request):
            self.metadata+=1
            if failure=='final_metadata' and self.metadata==2:
                from telethon.errors import FloodWaitError
                raise FloodWaitError(request,capture=28)
            return await super().__call__(request)
        async def get_messages(self,channel,ids):
            response=await super().get_messages(channel,ids)
            if failure=='last_batch' and ids==[101] and not self.failed:
                self.failed=True;raise flood_wait(28)
            return response
    fake=RetryClient({101})
    result=asyncio.run(sync.SourceDeletionChecker(catalog,fake,entity()).check())
    assert result['deleted_messages']==1 and delays.count(29)==1
    assert fake.requests==([list(range(1,101)),[101],[101],[101,1]] if failure=='last_batch'
                           else [list(range(1,101)),[101],[101,1]])
    assert fake.metadata==(2 if failure=='last_batch' else 3)
    # A successful individual RPC does not clear the whole check's health.
    assert store.get('deletion_check')['state']=='error'


@pytest.mark.parametrize('seconds,attempts',[(0,2),(60,2),(61,1)])
def test_flood_retry_limit_and_maximum_wait_boundary(store,monkeypatch,seconds,attempts):
    calls=[];delays=[]
    async def operation():calls.append(True);raise flood_wait(seconds)
    async def sleep(delay):delays.append(delay)
    monkeypatch.setattr(sync.asyncio,'sleep',sleep)
    with pytest.raises(type(flood_wait())):
        asyncio.run(sync._DeletionRPCPacer(0,store).call(operation))
    assert len(calls)==attempts and delays==([seconds+1] if attempts==2 else [])
    assert store.get('deletion_check')['retry_after']==seconds


def test_flood_error_unwraps_without_exposing_query_values():
    from telethon.errors import FloodWaitError
    from telethon.tl.functions import InvokeWithoutUpdatesRequest
    original=flood_wait()
    wrapped=FloodWaitError(InvokeWithoutUpdatesRequest(InvokeWithoutUpdatesRequest(original.request)),capture=28)
    details=sync.deletion_error_details(wrapped)
    assert details['request_type']=='GetMessagesRequest' and details['retry_after']==28
    assert '98765432123456789' not in json.dumps(details)


def test_wrapper_success_clears_its_recovered_flood_health(store,catalog,monkeypatch):
    publish(store,catalog);configure(monkeypatch,store);delays=[]
    class Client(Fake):
        failed=False
        def __init__(self,*args,**kwargs):super().__init__();self.session=SimpleNamespace()
        async def connect(self):pass
        async def disconnect(self):pass
        async def is_user_authorized(self):return True
        async def get_me(self):return SimpleNamespace(id=42,bot=False,username='reader')
        async def get_entity(self,source):return entity()
        async def get_messages(self,channel,ids):
            if not self.failed:self.failed=True;raise flood_wait(28)
            return await super().get_messages(channel,ids)
    async def sleep(delay):delays.append(delay);assert store.get('deletion_check')['retry_after']==28
    monkeypatch.setattr('telethon.TelegramClient',Client);monkeypatch.setattr(sync.asyncio,'sleep',sleep)
    report=asyncio.run(sync.check_source_deletions(catalog,s.DATA,request_interval=0))
    assert report['checked_messages']==1 and delays==[29]
    health=store.get('deletion_check')
    assert health['state']=='ok' and 'retry_at' not in health and 'account' not in health
