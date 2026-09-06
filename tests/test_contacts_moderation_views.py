"""Phone masks, isolated matching, moderation and private unique-view counting."""
import json,sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
import server as s
from test_server import agent_profile, client,body,signed,create

CASES=json.loads((Path(__file__).parent/'phone_cases.json').read_text(encoding='utf-8-sig'))
@pytest.mark.parametrize('text,expected',CASES)
def test_phone_masks(text,expected):
    assert s.phone_from_text(text)==expected


def post(client,address='Тестовая 1',uid=42,**fields):
    payload=body(address=address);payload['listing'].update(fields)
    r=client.post('/api/listings',json=payload,headers=signed(uid));assert r.status_code==200,r.text
    return r.json()


def test_phone_autofill_preserves_description_and_manual_choice(client):
    text='Дом с 15 числа до мая. Тел: +374 (91) 123456, цена 600.000'
    auto=post(client,description=text)
    assert auto['phone']=='+37491123456' and auto['description']==text
    assert auto['available'] is None and auto['prices'][0]['amount']==300000
    empty=post(client,address='Тестовая 2',description=text,phone='')
    manual=post(client,address='Тестовая 3',description=text,phone='+7 (999) 123-45-67')
    assert empty['phone']=='' and manual['phone']=='+79991234567'
    assert s.getrow(auto['id'])['phone_key']=='+37491123456'


def test_phone_matches_are_distinct_from_author_and_exclude_agents(client):
    agent_profile(client,44)
    first=post(client,phone='+374 (91) 123456')
    match=post(client,address='Тестовая 2',uid=43,phone='+37491123456')
    agent=post(client,address='Тестовая 3',uid=44,role='agent',commission=0,phone='+37491123456')
    different=post(client,address='Тестовая 4',phone='+79991234567')
    rented=post(client,address='Тестовая 5',phone='+37491123456')
    client.post('/api/listings/'+rented['id']+'/status',json={'status':'rented'},headers=signed())
    data=client.get('/api/listings/'+first['id']+'/phone-listings').json()
    assert data['available'] and [x['id'] for x in data['listings']]==[match['id']]
    assert not agent['phone_listings_available']
    assert client.get('/api/listings/'+agent['id']+'/phone-listings').json()=={'available':False,'listings':[]}
    author=client.get('/api/listings/'+first['id']+'/author-listings').json()
    assert [x['id'] for x in author['listings']]==[different['id']]


def test_phone_missing_or_untrusted_override(client):
    first=post(client,phone='',phone_key='+37491123456',phone_listings_available=True)
    assert not first['phone_listings_available']
    assert client.get('/api/listings/'+first['id']+'/phone-listings').json()=={'available':False,'listings':[]}


def ban(client,lid,reason='В объявлении скрытая комиссия',uid=99):
    return client.post('/api/admin/'+lid+'/ban',json={'reason':reason},headers=signed(uid))


def test_ban_requires_admin_reason_and_hides_every_public_path(client):
    first=post(client,phone='+37491123456');second=post(client,address='Тестовая 2',phone='+37491123456')
    assert ban(client,first['id'],uid=42).status_code==403
    for reason in ('',' ','xx'):
        assert ban(client,first['id'],reason=reason).status_code in (400,422)
    assert client.get('/api/admin/listings',headers=signed()).status_code==403
    response=ban(client,first['id']);assert response.status_code==200
    assert response.json()['status']=='banned' and response.json()['ban_reason']=='В объявлении скрытая комиссия'
    assert client.get('/api/listings/'+first['id']).status_code==404
    for route in ('author-listings','phone-listings'):
        assert client.get('/api/listings/'+first['id']+'/'+route).status_code==404
        assert client.get('/api/listings/'+second['id']+'/'+route).json()['listings']==[]
    assert client.post('/api/listings/'+first['id']+'/contact',json={},headers=signed()).status_code==404
    assert client.post('/api/listings/'+first['id']+'/view',json={},headers=signed(51)).status_code==404
    assert client.post('/api/listings/'+first['id']+'/status',json={'status':'active'},headers=signed()).status_code==403
    assert client.post('/api/admin/'+first['id']+'/decision',json={'decision':'approve'},headers=signed(99)).status_code==409
    own=client.get('/api/mine',headers=signed()).json();banned=next(x for x in own if x['id']==first['id'])
    assert banned['ban_reason']==response.json()['ban_reason']
    assert client.get('/api/mine',headers=signed(43)).json()==[]
    assert len(client.get('/api/admin/listings?status=banned',headers=signed(99)).json())==1
    assert 'СНЯТО МОДЕРАТОРОМ' in s.public_text(banned)
    assert s.keyboard(banned)=={'inline_keyboard':[]}
    assert banned['created_at']==first['created_at']


@pytest.mark.parametrize('previous',['active','rented','review'])
def test_unban_restores_previous_status_without_redating(client,previous):
    row=post(client)
    with s.db() as c:c.execute('UPDATE listings SET status=? WHERE id=?',(previous,row['id']))
    assert ban(client,row['id']).status_code==200
    assert ban(client,row['id'],reason='Уточнённая причина').status_code==200
    assert client.post('/api/admin/'+row['id']+'/unban',json={},headers=signed()).status_code==403
    r=client.post('/api/admin/'+row['id']+'/unban',json={},headers=signed(99))
    assert r.status_code==200 and r.json()['status']==previous
    assert r.json()['created_at']==row['created_at'] and r.json()['ban_reason']==''
    assert '_status_before_ban' not in r.text


def test_banned_repost_cannot_bypass_moderation_and_private_data_removed(client):
    row=create(client,private={'document_number':'TEST-DOC','document_password':'TEST-PASSWORD'})
    assert ban(client,row['id']).status_code==200
    assert s.getrow(row['id'])['private'] is None
    again=create(client,private={'document_number':'TEST-DOC','document_password':'TEST-PASSWORD'})
    assert again['id']==row['id'] and again['status']=='banned'
    changed=post(client,address='Тестовая 12',description='Изменённый текст')
    assert changed['status']=='review'


def test_views_authenticated_unique_public_and_private_ids(client):
    first=post(client,view_count=1000,community_post_count=777);second=post(client,address='Тестовая 2')
    path='/api/listings/'+first['id']+'/view'
    assert first['view_count']==0 and 'community_post_count' not in first
    assert client.post(path,json={'uid':1000}).status_code==401
    assert client.get('/api/listings/'+first['id']).json()['view_count']==0
    for _ in range(3):assert client.post(path,json={'uid':1000},headers=signed(42)).json()=={'view_count':1}
    assert client.post(path,json={},headers=signed(43)).json()=={'view_count':2}
    assert client.get('/api/listings/'+first['id']).json()['view_count']==2
    assert client.get('/api/listings/'+second['id']).json()['view_count']==0
    assert client.get('/api/mine',headers=signed()).json()[-1]['view_count']==2
    with s.db() as c:
        rows=c.execute('SELECT * FROM listing_views').fetchall()
        assert len(rows)==2 and all(len(x['viewer_key'])==32 for x in rows)
        assert 'uid' not in rows[0].keys()
    public=client.get('/api/listings').text
    assert 'viewer_key' not in public and '"uid"' not in public and 'ban_reason' not in public
    assert client.get('/api/listings/'+first['id']).json()['created_at']==first['created_at']


def test_concurrent_views_count_once_per_id_and_survive_setup(client):
    row=post(client);path='/api/listings/'+row['id']+'/view'
    users=[50,51,52,53]*3
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses=list(pool.map(lambda uid:client.post(path,json={},headers=signed(uid)).status_code,users))
    assert responses==[200]*len(users)
    assert client.get('/api/listings/'+row['id']).json()['view_count']==4
    s.setup()
    assert client.post(path,json={},headers=signed(50)).json()['view_count']==4
