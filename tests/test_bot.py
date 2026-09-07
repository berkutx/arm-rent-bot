import asyncio,json,sqlite3
import pytest
import server as s
from test_server import body,client,create,signed
from test_reports import incoming,photo,telegram


@pytest.fixture
def events(monkeypatch):
    seen=[]
    real=s.bot_stats.record
    def record(c,uid,stage,**kwargs):
        seen.append((uid,stage))
        return real(c,uid,stage,**kwargs)
    monkeypatch.setattr(s.bot_stats,'record',record)
    return seen


@pytest.mark.parametrize('command',['/start','/help'])
def test_bot_welcome_opens_three_app_routes(client,telegram,events,command):
    asyncio.run(s.receive(incoming(42,text=command)))
    method,message=telegram[-1]
    assert method=='sendMessage' and message['text']=='Поиск жилья и подача объявлений — в приложении.'
    assert message['reply_markup']['inline_keyboard']==[
        [s.app_button('Открыть приложение')],
        [s.app_button('Сдать жильё','add')],
        [s.app_button('Мои объявления','mine')]]
    assert events==[(42,'bot')]
    with s.db() as c:assert c.execute('SELECT COUNT(*) FROM drafts').fetchone()[0]==0


def test_bot_new_and_photo_handoff_keep_app_navigation(client,telegram):
    asyncio.run(s.receive(incoming(42,photo=photo())))
    assert telegram[-1][1]['reply_markup']['inline_keyboard'][0][0]['web_app']['url']=='https://rent.test?start=draft'
    assert client.get('/api/draft',headers=signed()).json()['photos']
    asyncio.run(s.receive(incoming(42,text='/new')))
    assert client.get('/api/draft',headers=signed()).json()=={'text':'','photos':[]}
    assert telegram[-1][1]['reply_markup']['inline_keyboard']==[[s.app_button('Сдать жильё','add')]]
    asyncio.run(s.receive(incoming(42,text='/start mine')))
    assert telegram[-1][1]['reply_markup']['inline_keyboard']==[[s.app_button('Открыть','mine')]]


def test_activity_requires_auth_and_cannot_forge_server_events(client,events):
    assert client.post('/api/activity',json={'stage':'housing'}).status_code==401
    for data in [{'stage':'submitted'},{'stage':'view'},{'stage':'housing','uid':99},{'stage':'unknown'}]:
        assert client.post('/api/activity',headers=signed(),json=data).status_code==422
    assert not events
    for stage in ['app','housing','address','price','photos','contact']:
        assert client.post('/api/activity',headers=signed(),json={'stage':stage}).status_code==200
    assert events==[(42,x) for x in ['app','housing','address','price','photos','contact']]


def test_real_app_view_and_submit_events_only_after_success(client,events):
    assert client.get('/api/me',headers=signed()).status_code==200
    assert events==[(42,'app')]
    assert client.post('/api/listings/no-such-listing/view',headers=signed(),json={}).status_code==404
    bad=body();bad['consent']=False
    assert client.post('/api/listings',headers=signed(),json=bad).status_code==400
    assert events==[(42,'app')]
    listing=create(client)
    assert events[-1]==(42,'submitted')
    create(client)
    assert events.count((42,'submitted'))==1
    assert client.post('/api/listings/'+listing['id']+'/view',headers=signed(43),json={}).status_code==200
    assert events[-1]==(43,'view')
    before=len(events)
    client.get('/api/feed');client.get('/api/listings/'+listing['id'])
    assert len(events)==before


def test_admin_and_channel_messages_are_excluded(client,events,telegram):
    client.get('/api/me',headers=signed(99))
    client.post('/api/activity',headers=signed(99),json={'stage':'contact'})
    asyncio.run(s.receive(incoming(99,text='/start')))
    update=incoming(42,text='/start');update['message']['chat']['type']='supergroup'
    asyncio.run(s.receive(update))
    assert not events


def test_report_delivery_only_to_current_admins_plain_text(client,telegram):
    for uid in [42,99]:
        asyncio.run(s.process_job({'kind':'admin_stats','payload':json.dumps({'uid':uid,'text':'За час: 2\n<name> @visitor'})}))
    assert len(telegram)==1
    method,message=telegram[0]
    assert method=='sendMessage' and message['chat_id']==99
    assert 'parse_mode' not in message and message['link_preview_options']['is_disabled']


def test_metrics_failure_does_not_block_app_or_submission(client,monkeypatch):
    def broken(*args,**kwargs):raise sqlite3.OperationalError('metrics unavailable')
    monkeypatch.setattr(s.bot_stats,'record',broken)
    assert client.get('/api/me',headers=signed()).status_code==200
    assert client.post('/api/activity',headers=signed(),json={'stage':'housing'}).status_code==200
    assert create(client)['status']=='active'


def test_metrics_schedule_failure_does_not_block_maintenance(client,monkeypatch):
    def broken(*args,**kwargs):raise sqlite3.OperationalError('metrics unavailable')
    monkeypatch.setattr(s.bot_stats,'schedule',broken)
    asyncio.run(s.maintenance())
