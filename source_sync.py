"""Telegram source snapshots, durable update queue and compact listing changes."""
from __future__ import annotations
import asyncio,contextlib,hashlib,json,logging,math,os,re,time
from datetime import datetime,timezone
from pathlib import Path

log=logging.getLogger('rent.source')

def packed(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True)
def digest(value):return hashlib.sha256(packed(value).encode()).hexdigest()

class SourceStore:
    def __init__(self,db):self.db=db

    def setup(self):
        with self.db() as c:c.executescript('''
        CREATE TABLE IF NOT EXISTS source_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_messages(channel INTEGER NOT NULL,mid INTEGER NOT NULL,topic INTEGER,post_key TEXT,payload TEXT,version REAL NOT NULL DEFAULT 0,observed REAL NOT NULL,checked REAL NOT NULL DEFAULT 0,deleted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(channel,mid));
        CREATE INDEX IF NOT EXISTS source_album ON source_messages(post_key,mid);
        CREATE INDEX IF NOT EXISTS source_recheck ON source_messages(deleted,checked);
        CREATE TABLE IF NOT EXISTS source_posts(post_key TEXT PRIMARY KEY,lid TEXT UNIQUE,revision TEXT NOT NULL DEFAULT '',payload TEXT NOT NULL DEFAULT '{}',state TEXT NOT NULL DEFAULT 'pending',note TEXT NOT NULL DEFAULT '',updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS source_dirty(post_key TEXT PRIMARY KEY,due REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS listing_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,lid TEXT NOT NULL,changed REAL NOT NULL,fields TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS listing_changes_lid ON listing_changes(lid,id);
        ''')

    def get(self,key,default=None):
        with self.db() as c:r=c.execute('SELECT value FROM source_state WHERE key=?',(key,)).fetchone()
        return json.loads(r['value']) if r else default

    def set(self,key,value):
        with self.db() as c:c.execute('INSERT OR REPLACE INTO source_state VALUES(?,?)',(key,packed(value)))

    def begin(self,channel,topics,now=None):
        old=self.get('scope');scope={'channel':channel,'topics':{str(k):v for k,v in topics.items()}}
        if old and old!=scope:raise ValueError('Source configuration differs from the initialized catalog')
        self.set('scope',scope)
        if self.get('cutoff') is None:
            anchor=now or time.time();self.set('cutoff',anchor-10*86400);self.set('anchor',anchor)

    def ingest(self,message,mode='event',requested_at=None,now=None):
        now=now or time.time();channel=message['channel'];mid=message['id']
        key=f"{channel}:"+('g'+str(message['group']) if message.get('group') else 'm'+str(mid))
        version=message.get('edited') or message['date']
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM source_messages WHERE channel=? AND mid=?',(channel,mid)).fetchone()
            if old and (old['deleted'] or mode=='backfill' or version<old['version'] or (requested_at is not None and old['observed']>requested_at)):return False
            body=packed(message)
            if old and body==old['payload']:
                c.execute('UPDATE source_messages SET checked=? WHERE channel=? AND mid=?',(now,channel,mid));return False
            c.execute('INSERT INTO source_messages(channel,mid,topic,post_key,payload,version,observed,checked) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(channel,mid) DO UPDATE SET topic=excluded.topic,post_key=excluded.post_key,payload=excluded.payload,version=excluded.version,observed=excluded.observed,checked=excluded.checked',(channel,mid,message['topic'],key,body,version,now,now))
            for dirty in {key,old['post_key'] if old else key}:
                c.execute('INSERT OR REPLACE INTO source_dirty VALUES(?,?)',(dirty,now+2))
            return True

    def delete(self,channel,ids,requested_at=None,now=None):
        now=now or time.time();changed=False
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            for mid in ids:
                old=c.execute('SELECT * FROM source_messages WHERE channel=? AND mid=?',(channel,mid)).fetchone()
                if old and (old['deleted'] or (requested_at is not None and old['observed']>requested_at)):continue
                if old:
                    c.execute('UPDATE source_messages SET deleted=1,observed=?,checked=? WHERE channel=? AND mid=?',(now,now,channel,mid))
                    c.execute('INSERT OR REPLACE INTO source_dirty VALUES(?,?)',(old['post_key'],now));changed=True
                else:
                    c.execute('INSERT OR IGNORE INTO source_messages(channel,mid,observed,checked,deleted) VALUES(?,?,?,?,1)',(channel,mid,now,now))
        return changed

    def snapshot(self,key):
        with self.db() as c:rows=c.execute('SELECT * FROM source_messages WHERE post_key=? ORDER BY mid',(key,)).fetchall()
        messages=[json.loads(r['payload']) for r in rows if not r['deleted']]
        original=[json.loads(r['payload']) for r in rows]
        captions=[m for m in messages if m['text'].strip()]
        root=next((m for m in original if m['text'].strip()),original[0] if original else None)
        deleted=not messages or (root is not None and not any(m['id']==root['id'] for m in messages))
        discussion=None
        if root and 'reply_to' in root:
            target=root['reply_to']
            discussion=bool(root['topic'] and target and target!=root['topic'] and target not in {m['id'] for m in original})
        revision=digest([(r['mid'],r['payload'],r['deleted']) for r in rows])
        return {'key':key,'revision':revision,'messages':messages,'root':root,'text':'\n\n'.join(m['text'] for m in captions),'deleted':deleted,'discussion':discussion}

    def pending(self,now=None):
        with self.db() as c:r=c.execute('SELECT post_key FROM source_dirty WHERE due<=? ORDER BY due LIMIT 1',(now or time.time(),)).fetchone()
        return self.snapshot(r['post_key']) if r else None

    def acknowledge(self,snapshot,state,payload=None,note='',lid=None):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if self.snapshot(snapshot['key'])['revision']!=snapshot['revision']:return False
            c.execute('INSERT INTO source_posts(post_key,lid,revision,payload,state,note,updated) VALUES(?,?,?,?,?,?,?) ON CONFLICT(post_key) DO UPDATE SET lid=COALESCE(excluded.lid,source_posts.lid),revision=excluded.revision,payload=excluded.payload,state=excluded.state,note=excluded.note,updated=excluded.updated',(snapshot['key'],lid,snapshot['revision'],packed(payload or {}),state,note,time.time()))
            c.execute('DELETE FROM source_dirty WHERE post_key=?',(snapshot['key'],))
        return True

    def recheck_batch(self,channel,now=None):
        with self.db() as c:return [r['mid'] for r in c.execute('SELECT mid FROM source_messages WHERE channel=? AND deleted=0 AND checked<? ORDER BY checked,mid LIMIT 100',(channel,(now or time.time())-300))]

    def record_changes(self,c,lid,before,after,when):
        fields={k:{'before':before.get(k),'after':after.get(k)} for k in ('address','city','prices') if before.get(k)!=after.get(k)}
        if not fields:return
        c.execute('INSERT INTO listing_changes(lid,changed,fields) VALUES(?,?,?)',(lid,when,packed(fields)))
        c.execute('DELETE FROM listing_changes WHERE lid=? AND id NOT IN (SELECT id FROM listing_changes WHERE lid=? ORDER BY id DESC LIMIT 5)',(lid,lid))


def topic_of(message):
    reply=message.reply_to
    return (reply.reply_to_top_id or reply.reply_to_msg_id or 0) if reply and reply.forum_topic else 0

def normalize_message(message,channel,username,topics):
    topic=topic_of(message)
    if topic not in topics or not getattr(message,'date',None) or getattr(message,'action',None):return None
    sender=getattr(message,'sender',None);author=getattr(getattr(message,'from_id',None),'user_id',0)
    if author<=0:author=0
    photo=message.photo;size=max((p for p in getattr(photo,'sizes',[]) if getattr(p,'w',0)>0 and getattr(p,'h',0)>0),key=lambda p:p.w*p.h,default=None)
    media={'id':str(photo.id),'width':size.w,'height':size.h} if photo and size else None
    return {'channel':channel,'username':username,'id':message.id,'topic':topic,'market':topics[topic],
            'reply_to':getattr(message.reply_to,'reply_to_msg_id',0) or 0,'reply_top':getattr(message.reply_to,'reply_to_top_id',0) or 0,
            'date':message.date.timestamp(),'edited':message.edit_date.timestamp() if message.edit_date else None,
            'group':str(message.grouped_id) if message.grouped_id else '', 'text':message.raw_text or '',
            'author':author,'contact':(getattr(sender,'username','') or '') if author and getattr(sender,'id',0)==author else '', 'photo':media}

class SourceReader:
    def __init__(self,store,client,entity,topics):
        from telethon import utils
        self.store=store;self.client=client;self.entity=entity;self.topics=topics
        self.channel=utils.get_peer_id(entity);self.username=entity.username

    def accept(self,message,mode='event',requested_at=None):
        data=normalize_message(message,self.channel,self.username,self.topics)
        if not data:return False
        with self.store.db() as c:known=c.execute('SELECT 1 FROM source_messages WHERE channel=? AND mid=?',(self.channel,data['id'])).fetchone()
        if not known and data['date']<self.store.get('cutoff',0):return False
        return self.store.ingest(data,mode,requested_at)

    async def message_event(self,event):
        if event.chat_id==self.channel:self.accept(event.message)

    async def delete_event(self,event):
        if event.chat_id==self.channel:self.store.delete(self.channel,event.deleted_ids)

    async def history(self,initial=False):
        started=time.time();cursor=0 if initial else self.store.get('history_cursor',0);top=cursor
        async for message in self.client.iter_messages(self.entity,min_id=cursor,limit=None,wait_time=1):
            top=max(top,message.id)
            if message.date.timestamp()<self.store.get('cutoff'):break
            self.accept(message,'backfill' if initial else 'history',requested_at=started)
        self.store.set('history_cursor',top);self.store.set('history_checked',time.time())
        if initial:self.store.set('history_complete',True)

    async def reconcile(self):
        ids=self.store.recheck_batch(self.channel)
        if not ids:return
        started=time.time();messages=await self.client.get_messages(self.entity,ids=ids)
        if len(messages)!=len(ids):raise ValueError('Incomplete response while reconciling source messages')
        deleted=[]
        for mid,message in zip(ids,messages):
            if message is not None and message.id!=mid:raise ValueError('Message order mismatch while reconciling source messages')
            if message is None or type(message).__name__=='MessageEmpty':deleted.append(mid)
        for message in messages:
            if message is not None and type(message).__name__!='MessageEmpty':self.accept(message,'reconcile',requested_at=started)
        self.store.delete(self.channel,deleted,requested_at=started)
        self.store.set('reconciled_at',time.time())

    async def run(self):
        from telethon import events,errors
        self.store.begin(self.channel,self.topics)
        self.store.set('source_username',self.username)
        self.client.add_event_handler(self.message_event,events.NewMessage(chats=[self.channel]))
        self.client.add_event_handler(self.message_event,events.MessageEdited(chats=[self.channel]))
        self.client.add_event_handler(self.delete_event,events.MessageDeleted(chats=[self.channel]))
        await self.client.catch_up()
        self.store.set('connection',{'state':'syncing','at':time.time()})
        while True:
            try:
                if not self.client.is_connected():raise ConnectionError('Telegram disconnected')
                if not self.store.get('history_complete'):await self.history(initial=True)
                elif time.time()-self.store.get('history_checked',0)>=60:await self.history()
                await self.reconcile()
                self.store.set('connection',{'state':'connected','at':time.time()})
                await asyncio.sleep(15)
            except errors.FloodWaitError as error:
                self.store.set('connection',{'state':'rate_limited','retry_at':time.time()+error.seconds+1})
                await asyncio.sleep(error.seconds+1)


def source_config():
    source=os.getenv('TELEGRAM_SOURCE','').strip().lstrip('@');raw=os.getenv('TELEGRAM_TOPICS','{}')
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}',source):raise ValueError('TELEGRAM_SOURCE must be a public channel username')
    mapping=json.loads(raw)
    if not isinstance(mapping,dict):raise ValueError('TELEGRAM_TOPICS must be an object')
    topics={int(k):v for k,v in mapping.items()}
    if not topics or len(topics)>8 or any(k<0 or v not in ('free','paid') for k,v in topics.items()):raise ValueError('TELEGRAM_TOPICS must map topic IDs to free or paid')
    if 0 in topics and len(topics)>1:raise ValueError('A whole-channel source cannot be combined with topic sources')
    return source,topics


@contextlib.contextmanager
def reader_lock(path):
    handle=open(path,'a+b');handle.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            if path.stat().st_size==0:handle.write(b'0');handle.flush();handle.seek(0)
            msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:handle.close()


async def run_reader(store,data_dir):
    from telethon import TelegramClient
    api_id=os.getenv('TELEGRAM_API_ID','');api_hash=os.getenv('TELEGRAM_API_HASH','')
    try:source,topics=source_config()
    except (ValueError,TypeError,json.JSONDecodeError):
        store.set('connection',{'state':'configuration_required'});return
    if not api_id.isdigit() or not re.fullmatch('[a-fA-F0-9]{32}',api_hash):
        store.set('connection',{'state':'credentials_required'});return
    session=Path(data_dir)/'telegram-reader.session'
    if not session.exists():store.set('connection',{'state':'login_required'});return
    with reader_lock(Path(data_dir)/'telegram-reader.lock'):
        while True:
            client=TelegramClient(str(session),int(api_id),api_hash,entity_cache_limit=128,device_model='Armenia Rent Reader',app_version='0.5.0',sequential_updates=True,catch_up=False,request_retries=2,connection_retries=3,flood_sleep_threshold=0)
            client.session.save_entities=False
            try:
                store.set('connection',{'state':'connecting','at':time.time()})
                await client.connect();os.chmod(session,0o600)
                if not await client.is_user_authorized():store.set('connection',{'state':'login_required'});return
                account=await client.get_me()
                if account.bot:store.set('connection',{'state':'user_account_required'});return
                entity=await client.get_entity(source)
                if not entity.username or getattr(entity,'left',True):store.set('connection',{'state':'membership_required'});return
                await SourceReader(store,client,entity,topics).run()
            except asyncio.CancelledError:raise
            except Exception as error:
                log.warning('Source connection unavailable (%s)',type(error).__name__)
                delay=max(30,getattr(error,'seconds',0)+1)
                store.set('connection',{'state':'disconnected','retry_at':time.time()+delay})
            finally:await client.disconnect()
            await asyncio.sleep(delay)

class SourceCatalog:
    def __init__(self,backend,store):self.s=backend;self.store=store

    def previous(self,key):
        with self.store.db() as c:return c.execute('SELECT * FROM source_posts WHERE post_key=?',(key,)).fetchone()

    def assets(self,snapshot):
        root=snapshot['root'];url=f"https://t.me/{root['username']}/{root['id']}";photos=[]
        for message in snapshot['messages']:
            photo=message.get('photo')
            if not photo:continue
            pid=digest([root['channel'],message['id'],photo['id']])[:32]
            metadata={'source_key':snapshot['key'],'source_post':url,'source_message':f"https://t.me/{root['username']}/{message['id']}",'width':photo['width'],'height':photo['height']}
            photos.append({'id':pid,'metadata':metadata})
        return photos[:10]

    def view(self,row):
        snap=self.store.snapshot(row['post_key']);data=json.loads(row['payload']);root=snap['root']
        return {'key':row['post_key'],'revision':snap['revision'],'state':row['state'],'note':row['note'],
                'text':snap['text'],'market':root['market'] if root else '', 'created_at':self.s.stamp(root['date']) if root else '',
                'source_url':f"https://t.me/{root['username']}/{root['id']}?thread={root['topic']}" if root else '',
                'photo_count':sum(bool(m['photo']) for m in snap['messages']), 'fields':data.get('fields',{}),'listing_id':row['lid']}

    def changed(self):
        self.store.set('catalog_revision',str(time.time_ns()))
        event=getattr(self.s,'CATALOG_CHANGED',None)
        if event:event.set()

    def hold(self,snapshot,state='review',note='Проверьте поля объявления'):
        old=self.previous(snapshot['key']);data=json.loads(old['payload']) if old else {}
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if self.store.snapshot(snapshot['key'])['revision']!=snapshot['revision']:raise ValueError('Пост изменился. Откройте последнюю версию')
            if old and old['lid']:
                row=c.execute('SELECT * FROM listings WHERE id=?',(old['lid'],)).fetchone()
                if row:
                    payload=json.loads(row['payload'])
                    payload.setdefault('_source_previous_status',row['status'])
                    payload['_source_deleted']=snapshot['deleted'];payload['_source_review']=state in ('review','ignored')
                    status=row['status'] if row['status']=='banned' else ('source_deleted' if snapshot['deleted'] else 'rejected' if state=='ignored' else 'review')
                    c.execute('UPDATE listings SET status=?,payload=?,reason=? WHERE id=?',(status,self.s.dumps(payload),row['reason'] if status=='banned' else note,old['lid']))
            data['text']=snapshot['text']
            c.execute('INSERT INTO source_posts(post_key,revision,payload,state,note,updated) VALUES(?,?,?,?,?,?) ON CONFLICT(post_key) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,state=excluded.state,note=excluded.note,updated=excluded.updated',(snapshot['key'],snapshot['revision'],packed(data),state,note,time.time()))
            c.execute('DELETE FROM source_dirty WHERE post_key=?',(snapshot['key'],))
        self.changed()

    def validate(self,fields,snapshot):
        d=self.s.ListingIn.model_validate(fields).model_dump();root=snapshot['root']
        d['city']=d['city'].strip();d['address']=d['address'].strip()
        if not d['city'] or len(d['address'])<3:raise ValueError('Укажите город и адрес из поста')
        if d['kind']=='apartment' and d['rooms'] is None:raise ValueError('Укажите число комнат')
        if root['market']=='paid' and (d['role']!='agent' or not d['commission']):raise ValueError('В агентской теме нужны роль агента и явная комиссия')
        if root['market']=='free' and d['commission']!=0:raise ValueError('Для темы без комиссии укажите комиссию 0')
        if d['commission_type']=='percent' and d['commission']>100:raise ValueError('Процент комиссии больше 100')
        if d['city']!='Ереван':d['district']='';d['metro_walk_minutes']=None
        elif d['district'] and d['district'] not in {r['name'] for r in json.loads((self.s.ROOT/'web/districts.json').read_text(encoding='utf-8'))}:raise ValueError('Выберите район из списка')
        for price in d['prices']:
            if price.get('amount_max') and price['amount_max']<price['amount']:raise ValueError('Неверный диапазон цены')
        for key in ('available','available_until'):
            if d[key]:datetime.strptime(d[key],'%Y-%m-%d')
        if d['available'] and d['available_until'] and d['available_until']<d['available']:raise ValueError('Неверный период аренды')
        d['description']=snapshot['text'][:12000];d['phone']=self.s.phone_from_text(snapshot['text'])
        d['contact']='@'+root['contact'] if re.fullmatch(r'[A-Za-z0-9_]{5,32}',root['contact']) else ''
        d['document_status']='none';d['photos']=[]
        return d

    def apply(self,snapshot,fields):
        if snapshot['deleted']:raise ValueError('Пост удалён из Telegram')
        d=self.validate(fields,snapshot);root=snapshot['root'];lid='t'+digest(snapshot['key'])[:20]
        d['_source_key']=snapshot['key'];d['source_revision']=snapshot['revision']
        d['source_updated_at']=self.s.stamp(max(m.get('edited') or m['date'] for m in snapshot['messages']))
        d['_source_post']={'chat':{'id':root['channel'],'username':root['username'],'type':'supergroup' if root['topic'] else 'channel'},'message_id':root['id'],'message_thread_id':root['topic']}
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if self.store.snapshot(snapshot['key'])['revision']!=snapshot['revision']:raise ValueError('Пост изменился. Откройте последнюю версию')
            old=c.execute('SELECT * FROM listings WHERE id=?',(lid,)).fetchone()
            previous=json.loads(old['payload']) if old else {}
            for p in self.assets(snapshot):
                c.execute('INSERT OR IGNORE INTO photos(id,uid,sha,tg_file_id,created,sizes) VALUES(?,?,?,?,?,?)',(p['id'],root['author'],p['id'],'',root['date'],packed({'full':p['metadata'],'thumb':p['metadata']})))
                d['photos'].append({'id':p['id'],'url':'/media/'+p['id']+'.jpg','thumb_url':'/media/'+p['id']+'-thumb.jpg','width':p['metadata']['width'],'height':p['metadata']['height']})
            d['photo_count']=len(d['photos'])
            status='active' if self.store.get('activated',False) else 'source_staged'
            if old:
                status=previous.get('_source_previous_status',old['status'])
                if old['status']=='banned':status='banned';d['_status_before_ban']=previous.get('_status_before_ban','active')
                elif status not in ('active','rented','rejected','source_staged'):status='active' if self.store.get('activated',False) else 'source_staged'
                for k in ('document_status','verification','document_checked_at'):
                    if previous.get('address')==d['address'] and previous.get('city')==d['city'] and k in previous:d[k]=previous[k]
                self.store.record_changes(c,lid,previous,d,max(m.get('edited') or m['date'] for m in snapshot['messages']))
            d['history']=[{'at':self.s.stamp(r['changed']),'fields':json.loads(r['fields'])} for r in c.execute('SELECT * FROM listing_changes WHERE lid=? ORDER BY id DESC LIMIT 5',(lid,))]
            c.execute('INSERT INTO listings(id,uid,payload,status,created,fingerprint,phone_key) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET uid=excluded.uid,payload=excluded.payload,status=excluded.status,phone_key=excluded.phone_key,reason=CASE WHEN listings.status=\'banned\' OR excluded.status=\'rejected\' THEN listings.reason ELSE \'\' END',(lid,root['author'],self.s.dumps(d),status,root['date'],digest(['telegram',snapshot['key']]),self.s.normalize_phone(d['phone'])))
            if old and (previous.get('address')!=d['address'] or previous.get('city')!=d['city']):
                c.execute('UPDATE listings SET private=NULL,private_expires=NULL WHERE id=?',(lid,))
            stored={'fields':fields,'text':snapshot['text']}
            c.execute("INSERT INTO source_posts(post_key,lid,revision,payload,state,updated) VALUES(?,?,?,?,'published',?) ON CONFLICT(post_key) DO UPDATE SET lid=excluded.lid,revision=excluded.revision,payload=excluded.payload,state='published',note='',updated=excluded.updated",(snapshot['key'],lid,snapshot['revision'],packed(stored),time.time()))
            c.execute('DELETE FROM source_dirty WHERE post_key=?',(snapshot['key'],))
        self.changed();return lid

    def process_one(self):
        snapshot=self.store.pending()
        if not snapshot:return False
        previous=self.previous(snapshot['key']);data=json.loads(previous['payload']) if previous else {}
        if snapshot['deleted']:self.hold(snapshot,'deleted','Пост удалён в Telegram')
        elif previous and previous['state']=='ignored' and data.get('text')==snapshot['text']:self.hold(snapshot,'ignored',previous['note'])
        elif previous and previous['state']=='published' and data.get('text')==snapshot['text']:
            self.apply(snapshot,data['fields'])
        else:self.hold(snapshot)
        return True

    def activate(self):
        if not self.store.get('history_complete'):raise ValueError('Первичная загрузка ещё не завершена')
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM source_dirty LIMIT 1').fetchone():raise ValueError('Обрабатываются новые изменения')
            staged=c.execute("SELECT COUNT(*) FROM listings WHERE status='source_staged'").fetchone()[0]
            if not staged:raise ValueError('Для переключения нужны готовые объявления из Telegram')
            legacy=c.execute("SELECT id,payload FROM listings WHERE json_extract(payload,'$._source_post') IS NOT NULL AND json_extract(payload,'$._source_key') IS NULL AND json_extract(payload,'$._source_post.chat.username')=?",(self.store.get('source_username',''),)).fetchall()
            for row in legacy:
                lid=row['id']
                c.execute('DELETE FROM report_uploads WHERE rid IN (SELECT id FROM reports WHERE lid=?)',(lid,))
                for table in ('reports','listing_views','deliveries','audit','listing_changes','listing_locations'):c.execute('DELETE FROM '+table+' WHERE lid=?',(lid,))
                c.execute("DELETE FROM jobs WHERE json_extract(payload,'$.id')=?",(lid,))
                c.execute('DELETE FROM listings WHERE id=?',(lid,))
                for photo in json.loads(row['payload']).get('photos',[]):
                    c.execute("DELETE FROM photos WHERE id=? AND NOT EXISTS(SELECT 1 FROM listings l,json_each(l.payload,'$.photos') p WHERE json_extract(p.value,'$.id')=?)",(photo['id'],photo['id']))
            c.execute("UPDATE listings SET status='active' WHERE status='source_staged'")
            c.execute("INSERT OR REPLACE INTO source_state VALUES('activated','true')")
        self.changed();return {'removed':len(legacy),'activated':staged}


class SourceDeletionError(ValueError):
    """A source check could not establish safe, authoritative deletion evidence."""


class SourceDeletionRetryLater(SourceDeletionError):
    def __init__(self,seconds,request_type=None):
        super().__init__('Stored Telegram retry deadline has not expired')
        self.seconds=seconds;self.request_type=request_type


def deletion_error_details(error):
    details={'error':type(error).__name__,'reason':str(error) if isinstance(error,SourceDeletionError) else 'Telegram source check failed'}
    seconds=getattr(error,'seconds',None)
    if type(seconds) is int and seconds>=0:details['retry_after']=seconds
    request=getattr(error,'request',None)
    for _ in range(8):
        if type(request).__name__!='InvokeWithoutUpdatesRequest':break
        request=getattr(request,'query',None)
    request_type=type(request).__name__ if request is not None else getattr(error,'request_type',None)
    if isinstance(request_type,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',request_type):details['request_type']=request_type
    return details


def deletion_retry_delay(store):
    health=store.get('deletion_check',{})
    deadline=health.get('retry_at',0) if isinstance(health,dict) else 0
    return max(0,math.ceil(deadline-time.time())) if type(deadline) in (int,float) and math.isfinite(deadline) else 0


def _record_deletion_flood_wait(store,error):
    now=time.time()
    store.set('deletion_check',{'state':'error','at':now,'retry_at':now+error.seconds+1,**deletion_error_details(error)})


class _DeletionRPCPacer:
    def __init__(self,interval=1.0,store=None):
        if interval<0:raise ValueError('RPC interval must not be negative')
        self.interval=0 if interval==0 else max(1.0,float(interval));self.completed=None;self.store=store

    async def call(self,operation):
        from telethon.errors import FloodWaitError
        for attempt in range(2):
            if self.completed is not None:
                delay=self.interval-(time.monotonic()-self.completed)
                if delay>0:await asyncio.sleep(delay)
            try:
                try:return await operation()
                finally:self.completed=time.monotonic()
            except FloodWaitError as error:
                if self.store is not None:_record_deletion_flood_wait(self.store,error)
                if attempt or not 0<=error.seconds<=60:raise
                # Retry this request only; a restart must observe the saved deadline.
                await asyncio.sleep(error.seconds+1)


class SourceDeletionChecker:
    """Inspect only messages already linked to imported listings; never ingest edits."""
    def __init__(self,catalog,client,entity,request_interval=1.0,requests=None):
        from telethon import utils
        self.catalog=catalog;self.store=catalog.store;self.client=client;self.entity=entity
        self.requests=requests or _DeletionRPCPacer(request_interval,self.store)
        self.channel=utils.get_peer_id(entity)
        self.scope=self.store.get('scope')
        if not self.scope or self.scope.get('channel')!=self.channel:
            raise SourceDeletionError('Source channel differs from the imported catalog')

    def tracked(self):
        posts={}
        with self.store.db() as c:
            rows=c.execute('SELECT p.post_key,p.lid,p.revision,l.payload FROM source_posts p JOIN listings l ON l.id=p.lid').fetchall()
        for row in rows:
            key=row['post_key']
            if not key.startswith(str(self.channel)+':'):continue
            payload=json.loads(row['payload']);source=payload.get('_source_post') or {}
            if payload.get('_source_key')!=key or source.get('chat',{}).get('id')!=self.channel:
                raise SourceDeletionError('Imported listing source linkage is inconsistent')
            snapshot=self.store.snapshot(key)
            if not snapshot['root'] or source.get('message_id')!=snapshot['root']['id']:
                raise SourceDeletionError('Imported listing root is inconsistent')
            if not snapshot['messages']:continue
            posts[key]={'lid':row['lid'],'snapshot':snapshot,'published_revision':row['revision']}
        return posts

    async def verify_history_access(self,ids):
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.types import ChannelFull
        response=await self.requests.call(lambda:self.client(GetFullChannelRequest(self.entity)))
        full=getattr(response,'full_chat',None)
        if not isinstance(full,ChannelFull) or full.id!=self.entity.id:
            raise SourceDeletionError('Telegram returned full history information for another source')
        if full.hidden_prehistory:
            raise SourceDeletionError('Source history before joining is hidden; no changes applied')
        minimum=full.available_min_id
        if minimum is not None and (type(minimum) is not int or minimum<0):
            raise SourceDeletionError('Source history availability is invalid')
        # Telegram defines available_min_id as the maximum UNAVAILABLE ID, inclusive.
        if minimum is not None and any(mid<=minimum for mid in ids):
            raise SourceDeletionError('Tracked source messages are outside accessible history; no changes applied')

    async def fetch_missing(self,ids):
        from telethon import utils
        from telethon.tl.types import Message,MessageEmpty
        messages=await self.requests.call(lambda:self.client.get_messages(self.entity,ids=ids))
        if not isinstance(messages,(list,tuple)) or len(messages)!=len(ids):
            raise SourceDeletionError('Incomplete Telegram response')
        missing=set()
        for mid,message in zip(ids,messages):
            if message is None:
                missing.add(mid);continue
            if message.id!=mid:raise SourceDeletionError('Telegram message order mismatch')
            if isinstance(message,MessageEmpty):
                if message.peer_id is not None and utils.get_peer_id(message.peer_id)!=self.channel:
                    raise SourceDeletionError('Telegram returned an empty message from another source')
                missing.add(mid);continue
            if not isinstance(message,Message) or utils.get_peer_id(message.peer_id)!=self.channel:
                raise SourceDeletionError('Telegram returned another source or an unexpected message')
        return missing

    async def check(self,apply=False):
        posts=self.tracked()
        ids=sorted({m['id'] for post in posts.values() for m in post['snapshot']['messages']})
        if ids:await self.verify_history_access(ids)
        missing=set();live=set()
        for offset in range(0,len(ids),100):
            batch=ids[offset:offset+100];absent=await self.fetch_missing(batch)
            missing.update(absent);live.update(set(batch)-absent)
        # A lost permission or an empty source must never erase the imported catalog.
        if ids and not live:raise SourceDeletionError('All tracked Telegram messages are unavailable; no changes applied')
        confirmed=set()
        if missing:
            control=min(live)
            candidates=sorted(missing)
            for offset in range(0,len(candidates),99):
                batch=candidates[offset:offset+99]
                absent=await self.fetch_missing(batch+[control])
                if control in absent:raise SourceDeletionError('Known source message became unavailable; no changes applied')
                confirmed.update(set(batch)&absent)
        if confirmed:await self.verify_history_access(ids)
        report={'mode':'apply' if apply else 'dry-run','checked_posts':len(posts),'checked_messages':len(ids),
                'deleted_messages':len(confirmed),'hidden_listings':0,'updated_albums':0,
                'unconfirmed_messages':len(missing-confirmed),'deleted_urls':[]}
        for post in posts.values():
            gone=[m for m in post['snapshot']['messages'] if m['id'] in confirmed]
            if not gone:continue
            # Losing any reviewed caption can invalidate the manually reviewed fields.
            hide=post['snapshot']['root']['id'] in confirmed or any(m['text'].strip() for m in gone)
            report['hidden_listings' if hide else 'updated_albums']+=1
            report['deleted_urls'].extend(f"https://t.me/{m['username']}/{m['id']}" for m in gone)
        if apply:self.apply(posts,ids,confirmed)
        return report

    def apply(self,posts,ids,confirmed):
        now=time.time();changed=False
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            scope=c.execute("SELECT value FROM source_state WHERE key='scope'").fetchone()
            if not scope or json.loads(scope['value'])!=self.scope:raise SourceDeletionError('Source scope changed during the check')
            for key,post in posts.items():
                snapshot=post['snapshot']
                rows=c.execute('SELECT mid,payload,deleted FROM source_messages WHERE post_key=? ORDER BY mid',(key,)).fetchall()
                if digest([(r['mid'],r['payload'],r['deleted']) for r in rows])!=snapshot['revision']:
                    raise SourceDeletionError('Source changed during the check; retry required')
                linked=c.execute('SELECT lid,revision FROM source_posts WHERE post_key=?',(key,)).fetchone()
                if not linked or linked['lid']!=post['lid'] or linked['revision']!=post['published_revision']:
                    raise SourceDeletionError('Imported listing changed during the check; retry required')
                row=c.execute('SELECT * FROM listings WHERE id=?',(post['lid'],)).fetchone()
                if not row:raise SourceDeletionError('Imported listing disappeared during the check')
                payload=json.loads(row['payload'])
                source=payload.get('_source_post') or {}
                if payload.get('_source_key')!=key or source.get('chat',{}).get('id')!=self.channel or source.get('message_id')!=snapshot['root']['id']:
                    raise SourceDeletionError('Imported listing source changed during the check')
                gone=[m for m in snapshot['messages'] if m['id'] in confirmed]
                if not gone:continue
                if post['published_revision']!=snapshot['revision']:
                    raise SourceDeletionError('Source has pending reviewed changes; no deletion applied')
                for m in gone:
                    c.execute('UPDATE source_messages SET deleted=1,observed=?,checked=? WHERE channel=? AND mid=?',(now,now,self.channel,m['id']))
                revision=digest([(r['mid'],r['payload'],1 if r['mid'] in confirmed else r['deleted']) for r in rows])
                hide=snapshot['root']['id'] in confirmed or any(m['text'].strip() for m in gone)
                photo_ids={digest([self.channel,m['id'],m['photo']['id']])[:32] for m in gone if m.get('photo')}
                payload['photos']=[p for p in payload.get('photos',[]) if p['id'] not in photo_ids]
                payload['photo_count']=len(payload['photos']);payload['source_revision']=revision
                status=row['status'];reason=row['reason']
                if hide:
                    payload.setdefault('_source_previous_status',status);payload['_source_deleted']=True
                    if status!='banned':status='source_deleted';reason='Пост удалён в Telegram'
                c.execute('UPDATE listings SET payload=?,status=?,reason=? WHERE id=?',(self.catalog.s.dumps(payload),status,reason,row['id']))
                if hide:
                    c.execute("UPDATE source_posts SET revision=?,state='deleted',note='Пост удалён в Telegram',updated=? WHERE post_key=?",(revision,now,key))
                else:c.execute('UPDATE source_posts SET revision=?,updated=? WHERE post_key=?',(revision,now,key))
                # Only consume this deletion; unrelated pending import work is untouched.
                c.execute('DELETE FROM source_dirty WHERE post_key=?',(key,));changed=True
            for mid in ids:
                c.execute('UPDATE source_messages SET checked=? WHERE channel=? AND mid=?',(now,self.channel,mid))
            if changed:c.execute("INSERT OR REPLACE INTO source_state VALUES('catalog_revision',?)",(packed(str(time.time_ns())),))
        if changed:
            event=getattr(self.catalog.s,'CATALOG_CHANGED',None)
            if event:event.set()


async def check_source_deletions(catalog,data_dir,apply=False,expected_account_id=None,request_interval=1.0):
    """One bounded check using the existing server session; no login or new messages."""
    from telethon import TelegramClient,utils,errors
    delay=deletion_retry_delay(catalog.store)
    if delay:raise SourceDeletionRetryLater(delay,catalog.store.get('deletion_check',{}).get('request_type'))
    if os.getenv('TELEGRAM_SYNC_ENABLED','0')!='0':raise SourceDeletionError('Full Telegram reader must be disabled')
    source,topics=source_config();scope=catalog.store.get('scope')
    if not scope or scope.get('topics')!={str(k):v for k,v in topics.items()}:
        raise SourceDeletionError('Source topic configuration differs from the imported catalog')
    expected=str(expected_account_id or os.getenv('TELEGRAM_READER_USER_ID',''))
    if not expected.isdigit() or int(expected)<=0:raise SourceDeletionError('TELEGRAM_READER_USER_ID is required')
    api_id=os.getenv('TELEGRAM_API_ID','');api_hash=os.getenv('TELEGRAM_API_HASH','')
    if not api_id.isdigit() or not re.fullmatch('[a-fA-F0-9]{32}',api_hash):raise SourceDeletionError('Telegram reader credentials are missing')
    data_dir=Path(data_dir);session=data_dir/'telegram-reader.session'
    if not session.is_file():raise SourceDeletionError('Existing Telegram reader session is required')
    with reader_lock(data_dir/'telegram-reader.lock'):
        client=TelegramClient(str(session),int(api_id),api_hash,receive_updates=False,catch_up=False,
                              entity_cache_limit=128,request_retries=2,connection_retries=3,flood_sleep_threshold=0)
        client.session.save_entities=False;requests=_DeletionRPCPacer(request_interval,catalog.store)
        try:
            await client.connect()
            if not await requests.call(client.is_user_authorized):raise SourceDeletionError('Telegram reader login is required')
            account=await requests.call(client.get_me)
            if not account or account.bot or account.id!=int(expected):raise SourceDeletionError('Unexpected Telegram reader account')
            # get_entity fetches channel information; do not trust the session's cached username mapping.
            entity=await requests.call(lambda:client.get_entity(source))
            if not entity.username or entity.username.lower()!=source.lower() or getattr(entity,'left',True) or getattr(entity,'restricted',False):
                raise SourceDeletionError('Source membership or access is unavailable')
            if utils.get_peer_id(entity)!=scope.get('channel'):raise SourceDeletionError('Source channel differs from the imported catalog')
            checker=SourceDeletionChecker(catalog,client,entity,requests=requests)
            report=await checker.check(apply=apply)
            report['account']={'id':account.id,'username':account.username or ''}
            counts={k:v for k,v in report.items() if isinstance(v,int)}
            catalog.store.set('deletion_check',{'state':'ok','at':time.time(),**counts})
            return report
        except errors.FloodWaitError as error:
            _record_deletion_flood_wait(catalog.store,error)
            raise
        finally:await client.disconnect()


async def run_deletion_checks(catalog,data_dir,interval=900):
    """Check immediately, then on a fixed interval. The full reader stays disabled."""
    if interval<=0:raise ValueError('Deletion check interval must be positive')
    while True:
        waiting=deletion_retry_delay(catalog.store)
        if waiting:
            await asyncio.sleep(waiting);continue
        delay=interval
        try:
            report=await check_source_deletions(catalog,data_dir,apply=True)
            counts={k:v for k,v in report.items() if isinstance(v,int)}
            catalog.store.set('deletion_check',{'state':'ok','at':time.time(),**counts})
        except asyncio.CancelledError:raise
        except SourceDeletionRetryLater as error:delay=error.seconds
        except Exception as error:
            delay=max(interval,getattr(error,'seconds',0)+1)
            catalog.store.set('deletion_check',{'state':'error','at':time.time(),'retry_at':time.time()+delay,**deletion_error_details(error)})
            log.warning('Source deletion check unavailable (%s)',type(error).__name__)
        await asyncio.sleep(delay)
