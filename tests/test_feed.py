"""Paged catalog cards, complete facets, stable cursors and offer selection."""
import json

import pytest

import server as s
from test_server import client


def insert_rows(specs):
    with s.db() as c:
        for i, spec in enumerate(specs):
            spec=dict(spec)
            lid=spec.pop('id',f'feed{i:05d}')
            created=spec.pop('created',1700000000+i)
            status=spec.pop('status','active')
            uid=spec.pop('uid',42)
            data=s.ListingIn(address='Тестовая '+lid,prices=[{'amount':300000,'currency':'AMD','period':'month'}],commission=0,role='owner').model_dump()
            data.update(document_status='none',**spec)
            c.execute('INSERT INTO listings(id,uid,payload,status,created,phone_key,view_count) VALUES(?,?,?,?,?,?,?)',
                      (lid,uid,s.dumps(data),status,created,s.normalize_phone(data.get('phone')),0))


def page(c, filters=None, **params):
    response=c.get('/api/feed',params={'filters':json.dumps(filters or {},ensure_ascii=False),**params})
    assert response.status_code==200,response.text
    return response.json()


def test_empty_feed(client):
    assert page(client)=={'listings':[],'total':0,'next_cursor':None,'markets':{'free':0,'paid':0},'districts':{},'district_total':0,'cities':[]}
    assert client.get('/api/listings').json()==[]


def test_feed_pages_complete_catalog_without_legacy_500_limit(client):
    insert_rows([{'district':'Арабкир','created':1700000000} for _ in range(517)])
    result=page(client)
    assert len(result['listings'])==12 and result['total']==517
    assert result['markets']=={'free':517,'paid':0}
    assert result['districts']=={'Арабкир':517} and result['district_total']==517
    assert result['cities']==['Ереван']
    ids=[]
    while True:
        ids.extend(x['id'] for x in result['listings'])
        assert result['total']==517
        if result['next_cursor'] is None:break
        result=page(client,cursor=result['next_cursor'],limit=48)
    assert ids==[f'feed{i:05d}' for i in reversed(range(517))]
    # The existing endpoint keeps its old response contract for other clients.
    assert len(client.get('/api/listings').json())==500


def test_full_facets_respect_filters_and_global_markets(client):
    insert_rows([
        {'district':'Кентрон','prices':[{'amount':200000,'currency':'AMD','period':'month'}]},
        {'district':'Арабкир','prices':[{'amount':250000,'currency':'AMD','period':'month'}]},
        {'district':'Арабкир','prices':[{'amount':500000,'currency':'AMD','period':'month'}]},
        {'district':'','prices':[{'amount':100000,'currency':'AMD','period':'month'}]},
        {'city':'Дилижан','district':'','prices':[{'amount':100000,'currency':'AMD','period':'month'}]},
        {'district':'Кентрон','role':'agent','commission':50},
        {'district':'Кентрон','role':'owner','commission':50},
        {'district':'Кентрон','commission':None},
        {'district':'Кентрон','status':'rented'},
        {'district':'Кентрон','status':'review'},
    ])
    result=page(client,{'city':'Ереван','district':'Кентрон','currency':'AMD','period':'month','max':'300000'},limit=1)
    assert result['total']==1
    assert result['districts']=={'Кентрон':1,'Арабкир':1} and result['district_total']==3
    assert result['markets']=={'free':5,'paid':1}
    assert set(result['cities'])=={'Ереван','Дилижан'}
    other=page(client,{'city':'Дилижан','district':'','max':'300000'})
    assert other['total']==1 and other['district_total']==3
    paid=page(client,{'market':'paid'})
    assert paid['total']==1 and paid['listings'][0]['role']=='agent'


def test_compact_cards_keep_search_full_details_and_real_photo_count(client,monkeypatch):
    photos=[{'id':f'{i:032x}','url':f'/media/{i:032x}.jpg','thumb_url':f'/media/{i:032x}-thumb.jpg','width':800,'height':600} for i in range(10)]
    insert_rows([{'photos':photos,'photo_count':999,'description':'Большое описание, поиск_только_здесь','history':['old description'],
                  'contact':'@test_user','phone':'+37491123456','wishes':'DETAIL-ONLY','source_revision':'old','source_updated_at':'2026-09-01'}])
    def forbidden(*args,**kwargs):raise AssertionError('Feed must not resolve or download photos')
    monkeypatch.setattr(s,'photo_sizes',forbidden)
    monkeypatch.setattr(s,'tg',forbidden)
    statements=[]
    original=s.db
    def traced_db():
        c=original();c.set_trace_callback(statements.append);return c
    monkeypatch.setattr(s,'db',traced_db)
    result=page(client,{'q':'ПОИСК_только_здесь'})
    card=result['listings'][0]
    assert card['photos']==photos[:3] and card['photo_count']==10
    assert not {'description','history','phone','contact','wishes','source_revision','source_updated_at','uid','private','fingerprint'} & card.keys()
    selects=[sql for sql in statements if sql.lstrip().upper().startswith('SELECT')]
    assert len(selects)==1 and 'FROM listings' in selects[0]
    detail=client.get('/api/listings/'+card['id']).json()
    assert detail['photos']==photos and detail['description'].endswith('поиск_только_здесь')
    assert detail['contact']=='@test_user'


def test_price_sort_and_budget_use_selected_offer_not_first_offer(client):
    insert_rows([
        {'id':'a','created':1,'prices':[{'amount':1,'currency':'AMD','period':'day'}, {'amount':700,'currency':'USD','period':'month'}]},
        {'id':'b','created':1,'prices':[{'amount':100000,'currency':'AMD','period':'day'}, {'amount':500,'currency':'USD','period':'month'}]},
        {'id':'c','created':2,'prices':[{'amount':500,'currency':'USD','period':'month'}]},
        {'id':'d','created':2,'prices':[{'amount':500,'currency':'USD','period':'month'}]},
    ])
    result=page(client,{'currency':'USD','period':'month','max':'600'},sort='price',limit=2)
    assert result['total']==3 and [x['id'] for x in result['listings']]==['d','c']
    following=page(client,{'currency':'USD','period':'month','max':'600'},sort='price',cursor=result['next_cursor'])
    assert [x['id'] for x in following['listings']]==['b']
    result=page(client,sort='price')
    assert [x['id'] for x in result['listings']]==['a','b','d','c']


def test_price_sort_groups_currency_and_period_before_amount(client):
    insert_rows([
        {'id':'usd','prices':[{'amount':1,'currency':'USD','period':'day'}]},
        {'id':'month','prices':[{'amount':1,'currency':'AMD','period':'month'}]},
        {'id':'day','prices':[{'amount':9999,'currency':'AMD','period':'day'}]},
    ])
    assert [x['id'] for x in page(client,sort='price')['listings']]==['day','month','usd']


def test_pets_and_registration_choose_matching_offer(client):
    insert_rows([
        {'id':'mixed','pets':'yes','residence_registration':'yes','prices':[
            {'amount':100,'currency':'AMD','period':'month','pets':'no','registration':'no'},
            {'amount':500,'currency':'AMD','period':'month','pets':'yes','registration':'yes'}]},
        {'id':'single','pets':'yes','residence_registration':'ask','prices':[
            {'amount':400,'currency':'AMD','period':'month','pets':'unknown','registration':'unknown'}]},
    ])
    filters={'pets':True,'residence_registration':True}
    assert [x['id'] for x in page(client,filters,sort='price')['listings']]==['single','mixed']
    assert page(client,{**filters,'max':'450'})['total']==1


@pytest.mark.parametrize('change',['insert','rented','banned','delete','payload','created'])
def test_cursor_rejects_catalog_changes(client,change):
    insert_rows([{},{}])
    cursor=page(client,limit=1)['next_cursor']
    with s.db() as c:
        if change=='insert':
            c.execute("INSERT INTO listings(id,uid,payload,status,created,phone_key) SELECT 'new',uid,payload,status,created+1,phone_key FROM listings LIMIT 1")
        elif change in ('rented','banned'):c.execute('UPDATE listings SET status=? WHERE id=?',(change,'feed00000'))
        elif change=='delete':c.execute('DELETE FROM listings WHERE id=?',('feed00000',))
        elif change=='payload':c.execute("UPDATE listings SET payload=json_set(payload,'$.description','changed') WHERE id=?",('feed00000',))
        elif change=='created':c.execute('UPDATE listings SET created=created+1 WHERE id=?',('feed00000',))
    assert client.get('/api/feed',params={'cursor':cursor}).status_code==409


def test_cursor_survives_views_and_equivalent_filter_order(client):
    insert_rows([{},{}])
    filters={'market':'free','city':'Ереван'}
    cursor=page(client,filters,limit=1)['next_cursor']
    with s.db() as c:c.execute('UPDATE listings SET view_count=10')
    result=page(client,{'city':'Ереван','market':'free'},cursor=cursor)
    assert result['listings'][0]['id']=='feed00000' and result['listings'][0]['view_count']==10
    assert result['next_cursor'] is None
    assert client.get('/api/feed',params={'filters':json.dumps({'city':'Дилижан'}),'cursor':cursor}).status_code==409
    assert client.get('/api/feed',params={'filters':json.dumps(filters),'sort':'price','cursor':cursor}).status_code==409
    bad=cursor[:-1]+('0' if cursor[-1]!='0' else '1')
    assert client.get('/api/feed',params={'cursor':bad}).status_code==422


@pytest.mark.parametrize('filters',[
    'not json','[]','null','true','{"q":12}','{"city":[]}','{"owner":"false"}',
    '{"pets":1}','{"rooms":2}','{"max":300000}','{"max":"1.5"}',
    '{"market":"all"}','{"unknown":true}',json.dumps({'q':'x'*201}),
    json.dumps({'city':'x'*81}),json.dumps({'district':'x'*81}),' '*4097,
])
def test_feed_filter_validation(client,filters):
    assert client.get('/api/feed',params={'filters':filters}).status_code==422


@pytest.mark.parametrize('params',[{'limit':0},{'limit':-1},{'limit':49},{'limit':'no'}, {'limit':'1.5'},
                                  {'sort':'old'}, {'cursor':'broken'},{'cursor':'x'*513}])
def test_feed_paging_validation(client,params):
    assert client.get('/api/feed',params=params).status_code==422
