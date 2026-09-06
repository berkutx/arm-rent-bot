import pytest
import server as s
from test_server import agent_profile, body, client, signed


def agent_listing(client, uid=42, **fields):
    data=body();data['listing'].update(role='agent',**fields)
    return client.post('/api/listings',headers=signed(uid),json=data)


def test_agent_profile_required_in_both_markets(client):
    for commission in [0,50]:
        response=agent_listing(client,commission=commission)
        assert response.status_code==400 and 'профиль агента' in response.json()['detail']
    assert client.get('/api/listings').json()==[]


def test_agent_profile_is_private_encrypted_and_reusable(client):
    assert client.get('/api/agent-profile').status_code==401
    assert client.put('/api/agent-profile',json={}).status_code==401
    agent_profile(client)
    me=client.get('/api/me',headers=signed()).json()
    assert me['agent_profile_ready'] and me['agent_affiliation']=='Example Realty'
    own=client.get('/api/agent-profile',headers=signed()).json()
    assert own['phone']=='+37491654321' and own['full_name']=='Test Agent'
    assert client.get('/api/agent-profile',headers=signed(43)).json()=={}
    with s.db() as c:
        encrypted=c.execute('SELECT encrypted FROM agent_profiles').fetchone()[0]
    assert b'Test Agent' not in encrypted and b'654321' not in encrypted
    for address,commission in [('Профиль 10',0),('Профиль 20',50)]:
        response=agent_listing(client,address=address,commission=commission,agent_affiliation='Forged',phone='')
        assert response.status_code==200,response.text
        listing=response.json()
        assert listing['agent_affiliation']=='Example Realty' and listing['phone']==''
        assert listing['document_status']=='none'
        path='/api/admin/'+listing['id']+'/agent-profile'
        assert client.get(path,headers=signed()).status_code==403
        assert client.get(path,headers=signed(43)).status_code==403
        assert client.get(path,headers=signed(99)).json()==own
        for output in [response.text,client.get('/api/listings').text,client.get('/api/mine',headers=signed()).text,s.public_text(listing)]:
            assert 'Test Agent' not in output and '654321' not in output
        assert 'Example Realty' in s.public_text(listing)


@pytest.mark.parametrize('fields',[
    {'full_name':'Name'}, {'full_name':'   '}, {'phone':'091123456'},
    {'phone':'+374 letters'}, {'agency':' '},
])
def test_agent_profile_validation(client,fields):
    data={'full_name':'Test Agent','phone':'+37491654321','agency':'Example Realty',**fields}
    assert client.put('/api/agent-profile',headers=signed(),json=data).status_code==400
    assert not client.get('/api/me',headers=signed()).json()['agent_profile_ready']


def test_independent_agent_and_profile_changes(client):
    agent_profile(client,independent=True,agency='Ignored')
    assert client.get('/api/agent-profile',headers=signed()).json()['agency']==''
    first=agent_listing(client,address='Профиль 30').json()
    assert first['agent_affiliation']=='Частный агент'
    agent_profile(client,agency='New Realty')
    second=agent_listing(client,address='Профиль 40').json()
    assert second['agent_affiliation']=='New Realty'
    assert client.get('/api/listings/'+first['id']).json()['agent_affiliation']=='Частный агент'
    ordinary=client.post('/api/listings',headers=signed(),json=body(address='Профиль 50')).json()
    assert 'agent_affiliation' not in ordinary
    assert client.get('/api/admin/'+ordinary['id']+'/agent-profile',headers=signed(99)).status_code==404
