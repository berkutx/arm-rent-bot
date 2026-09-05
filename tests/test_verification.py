"""API integration tests; official site and Telegram are NOT contacted."""
import asyncio,json,time
from pathlib import Path
import server as s
from test_server import client,create,signed,body

V={'document_number':'DEMO-DOC-123','document_password':'DEMO-PASS-SECRET','applicant_name':'Вымышленный заявитель','consent':True}
D={'result':'owner_verified','document_valid':True,'object_matches':True,'identity_matches':True,'rights_current':True,'checked_on':'2026-09-05'}

def test_private_verification_workflow(client):
 l=create(client);lid=l['id'];before=l['created_at']
 url=f'/api/listings/{lid}/verification'
 assert client.post(url,json=V,headers=signed(100)).status_code==403
 assert client.post(url,json={**V,'consent':False},headers=signed()).status_code==400
 assert client.post(url,json=V,headers=signed()).status_code==200
 assert 'DEMO-PASS' not in client.get('/api/listings').text
 assert b'DEMO-PASS' not in s.DB.read_bytes()
 assert client.get(f'/api/admin/{lid}/private',headers=signed()).status_code==403
 assert client.get(f'/api/admin/{lid}/private',headers=signed(99)).json()['document_number']==V['document_number']
 dec=f'/api/admin/{lid}/verification'
 assert client.post(dec,json=D,headers=signed()).status_code==403
 assert client.post(dec,json={**D,'identity_matches':False},headers=signed(99)).status_code==400
 assert client.post(dec,json={**D,'document_valid':False},headers=signed(99)).status_code==400
 assert client.post(dec,json={**D,'checked_on':'2099-01-01'},headers=signed(99)).status_code==400
 assert client.post(dec,json=D,headers=signed(99)).status_code==200
 final=client.get('/api/listings/'+lid).json()
 assert final['document_status']=='owner_verified' and final['created_at']==before
 assert final['verification']['method']=='e-cadastre-manual'
 assert not client.get(f'/api/admin/{lid}/private',headers=signed(99)).json()['document_password']
 assert client.post(dec,json=D,headers=signed(99)).status_code==409
 with s.db() as c:
  audits=' '.join(x['action'] for x in c.execute('select action from audit'))
  assert 'DEMO-PASS' not in audits and 'Вымышленный' not in audits

def test_representative_requires_authority(client):
 l=create(client);lid=l['id']
 client.post(f'/api/listings/{lid}/verification',json=V,headers=signed())
 dec=f'/api/admin/{lid}/verification'
 assert client.post(dec,json={**D,'result':'representative_verified'},headers=signed(99)).status_code==400
 assert client.post(dec,json={**D,'result':'representative_verified','authority_checked':True},headers=signed(99)).status_code==200

def test_expired_credentials_purged_not_advert(client):
 l=create(client);lid=l['id'];client.post(f'/api/listings/{lid}/verification',json=V,headers=signed())
 with s.db() as c:c.execute('UPDATE listings SET private_expires=? WHERE id=?',(time.time()-1,lid))
 asyncio.run(s.maintenance())
 final=client.get('/api/listings/'+lid).json()
 assert final['status']=='active' and final['document_status']=='none'
 assert s.getrow(lid)['private'] is None

def test_phone_not_required_and_not_identity(client):
 l=create(client)
 assert not l['phone'] and l['contact']=='@test_user' and l['document_status']=='none'
 x=body('Другой адрес 2');x['listing']['phone']='+37499123456'
 l=client.post('/api/listings',json=x,headers=signed()).json()
 assert l['phone']=='+37499123456' and l['document_status']=='none'

def test_registration_price_condition():
 l=json.loads((s.ROOT/'tests/conditional_price.json').read_text(encoding='utf-8'))
 assert s.matches(l,{'period':'month','currency':'AMD','max':'420000'})
 assert not s.matches(l,{'period':'month','currency':'AMD','max':'420000','residence_registration':True})
 assert s.matches(l,{'period':'month','currency':'AMD','max':'450000','residence_registration':True})



def test_html_has_no_embedded_catalog(client):
 txt=client.get('/').text
 assert 'seed-data' not in txt and '/api/examples' not in txt
 assert 'Барбюса 66' not in txt


def test_validation_does_not_echo_private_input(client):
 l=create(client)
 secret='PRIVATE-VALIDATION-MARKER-'*6
 response=client.post('/api/listings/'+l['id']+'/verification',headers=signed(),json={**V,'document_password':secret})
 assert response.status_code==422
 assert secret not in response.text and 'PRIVATE-VALIDATION' not in response.text
 assert s.getrow(l['id'])['private'] is None
