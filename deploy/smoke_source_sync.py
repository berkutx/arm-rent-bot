"""Bounded source metadata work in the runtime image, without Telegram network."""
import json,resource,time
from telethon import TelegramClient
import server as s
s.setup();store=s.SOURCE_STORE;catalog=s.SOURCE_CATALOG;channel=-1000000000123;store.begin(channel,{55:'free'});store.set('source_username','source_fixture')
for n in range(1000):
    m={'channel':channel,'username':'source_fixture','id':n+1,'topic':55,'market':'free','date':time.time()-100,'edited':None,'group':str(n//10),'text':'Квартира на Улице 10' if n%10==0 else '', 'author':42,'contact':'author_fixture','photo':{'id':str(n),'width':1280,'height':960}}
    store.ingest(m)
    if n%10==9:
        catalog.apply(store.snapshot(f'{channel}:g{n//10}'),{'address':'Улица '+str(n),'city':'Ереван','kind':'apartment','rooms':2,'commission':0,'prices':[{'amount':300000,'currency':'AMD','period':'month'}]})
store.set('history_complete',True);assert catalog.activate()['activated']==100
with s.db() as c:
    assert c.execute('SELECT COUNT(*) FROM photos').fetchone()[0]==1000
    assert c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==0
assert not list(s.DATA.glob('*.jpg'))
print(json.dumps({'source_messages':1000,'source_albums':100,'outgoing_jobs':0,'disk_photo_bytes':0,'max_rss_mib':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1)}))
