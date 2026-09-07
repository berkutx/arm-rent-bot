"""Telegram Mini App. One process; network writes require LIVE=1."""
from __future__ import annotations
import asyncio, base64, binascii, contextlib, hashlib, hmac, json, logging, os, re, secrets, sqlite3, time
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl
import httpx
from source_sync import SourceStore,SourceCatalog,run_reader,run_deletion_checks
from cryptography.fernet import Fernet
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse, FileResponse
from pydantic import BaseModel, Field, ConfigDict, ValidationError, model_validator

ROOT=Path(__file__).resolve().parent
# Read a local .env without an additional dependency. Never include this file in source control.
if (ROOT/'.env').exists():
    for line in (ROOT/'.env').read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            k,v=line.split('=',1);os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
LIVE=os.getenv('LIVE','0')=='1'
TOKEN=os.getenv('BOT_TOKEN','')
BOT=os.getenv('BOT_USERNAME','').lstrip('@')
PUBLIC_URL=os.getenv('PUBLIC_URL','').rstrip('/')
CHAT=os.getenv('PUBLISH_CHAT_ID','')
THREAD=int(os.getenv('PUBLISH_THREAD_ID','0')) or None
PAID_CHAT=os.getenv('PUBLISH_PAID_CHAT_ID','')
PAID_THREAD=int(os.getenv('PUBLISH_PAID_THREAD_ID','0')) or None
ADMINS={int(v) for v in os.getenv('ADMIN_IDS','').split(',') if v.strip().isdigit()}
DATA=Path(os.getenv('DATA_DIR',str(ROOT/'data')))
DB=DATA/'rent.sqlite3'
CIPHER=None
VIEW_SECRET=None
log=logging.getLogger('rent')
FILE_PATHS={}
SOURCE_STORE=SourceStore(lambda:db())
SOURCE_CATALOG=SourceCatalog(__import__("sys").modules[__name__],SOURCE_STORE)
CATALOG_CHANGED=asyncio.Event()
GEOCODE_LOCK=asyncio.Lock()
GEOCODER_URL=os.getenv('GEOCODER_URL','https://nominatim.openstreetmap.org/search').strip()

def db():
    c=sqlite3.connect(DB,timeout=15);c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=15000');c.execute('PRAGMA secure_delete=ON')
    return c

def setup():
    global CIPHER,VIEW_SECRET
    DATA.mkdir(parents=True,exist_ok=True);os.chmod(DATA,0o700)
    kp=DATA/'private.key'
    if not kp.exists():kp.write_bytes(Fernet.generate_key());os.chmod(kp,0o600)
    CIPHER=Fernet(kp.read_bytes())
    VIEW_SECRET=hmac.new(kp.read_bytes(),b'rent-unique-views-v1',hashlib.sha256).digest()
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS listing_views(lid TEXT NOT NULL, viewer_key BLOB NOT NULL, PRIMARY KEY(lid,viewer_key)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY,lid TEXT NOT NULL,uid INTEGER NOT NULL,payload BLOB NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,resolved REAL,resolved_by INTEGER,resolution TEXT DEFAULT '',outcome TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS reports_queue ON reports(status,created);
        CREATE UNIQUE INDEX IF NOT EXISTS report_draft ON reports(uid,lid) WHERE status='draft';
        CREATE TABLE IF NOT EXISTS report_uploads(uid INTEGER PRIMARY KEY,rid TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_profiles(uid INTEGER PRIMARY KEY, encrypted BLOB NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, username TEXT, name TEXT, started INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS listings(id TEXT PRIMARY KEY, uid INTEGER, payload TEXT, status TEXT, created REAL, confirmed REAL, fingerprint TEXT, private BLOB, private_expires REAL, reason TEXT, channel_message INTEGER, channel_kind TEXT, reminded REAL DEFAULT 0, UNIQUE(uid,fingerprint));
        CREATE INDEX IF NOT EXISTS listings_author ON listings(uid,status,created);
        CREATE INDEX IF NOT EXISTS listings_feed ON listings(status,created DESC,id DESC);
        CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY, uid INTEGER, sha TEXT, tg_file_id TEXT, created REAL, sizes TEXT);
        CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,uid INTEGER,name TEXT,filters TEXT,frequency TEXT,active INTEGER,created REAL,last_digest TEXT);
        CREATE TABLE IF NOT EXISTS deliveries(uid INTEGER,lid TEXT,PRIMARY KEY(uid,lid));
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,jobkey TEXT UNIQUE,status TEXT DEFAULT 'pending',run_at REAL,attempts INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER,lid TEXT,action TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS drafts(uid INTEGER PRIMARY KEY,text TEXT,photos TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS geocode_cache(key TEXT PRIMARY KEY,payload TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS listing_locations(lid TEXT PRIMARY KEY,address_key TEXT NOT NULL,payload TEXT NOT NULL,updated REAL NOT NULL);
        ''')
        if not c.execute("SELECT 1 FROM meta WHERE key='reports_v1'").fetchone():
            for row in c.execute("SELECT a.*,u.username,u.name FROM audit a LEFT JOIN users u ON u.uid=a.uid WHERE action='report'").fetchall():
                rid=hashlib.sha256(('report:'+str(row['id'])).encode()).hexdigest()[:16]
                payload={'reason':'','details':'','evidence':'','photos':[],'reporter':{'name':row['name'] or '', 'username':row['username'] or ''}}
                c.execute('INSERT OR IGNORE INTO reports(id,lid,uid,payload,status,created) VALUES(?,?,?,?,?,?)',(rid,row['lid'],row['uid'],CIPHER.encrypt(dumps(payload).encode()),'pending',row['created']))
            c.execute("UPDATE jobs SET status='cancelled' WHERE kind='admin' AND json_extract(payload,'$.report')=1 AND status='pending'")
            c.execute("INSERT INTO meta VALUES('reports_v1','1')")
        if 'sizes' not in {r['name'] for r in c.execute('PRAGMA table_info(photos)')}:c.execute('ALTER TABLE photos ADD COLUMN sizes TEXT')
        columns={row['name'] for row in c.execute('PRAGMA table_info(listings)')}
        if 'view_count' not in columns:c.execute('ALTER TABLE listings ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0')
        if 'phone_key' not in columns:
            c.execute("ALTER TABLE listings ADD COLUMN phone_key TEXT NOT NULL DEFAULT ''")
            for row in c.execute('SELECT id,payload FROM listings').fetchall():
                phone=normalize_phone(json.loads(row['payload']).get('phone',''))
                c.execute('UPDATE listings SET phone_key=? WHERE id=?',(phone,row['id']))
        c.execute('CREATE INDEX IF NOT EXISTS listings_phone ON listings(phone_key,status)')
        c.execute("UPDATE jobs SET status='uncertain' WHERE status='sending'")
        # One-time compatibility migration: undo v3 automatic hiding; never bump dates.
        if not c.execute("SELECT 1 FROM meta WHERE key='age_only_v4'").fetchone():
            hidden=c.execute("SELECT id FROM listings WHERE status='stale' AND channel_message IS NOT NULL").fetchall()
            c.execute("UPDATE listings SET status='active' WHERE status='stale'")
            c.execute("UPDATE jobs SET status='cancelled' WHERE kind='reminder' AND status IN ('pending','sending')")
            for row in hidden:
                c.execute('INSERT OR IGNORE INTO jobs(kind,payload,jobkey,run_at) VALUES(?,?,?,?)',('edit',json.dumps({'id':row['id']}),'v4-restore:'+row['id'],time.time()))
            c.execute("INSERT INTO meta(key,value) VALUES('age_only_v4','1')")

    SOURCE_STORE.setup()


def normalize_phone(value):
    phone=re.sub(r'[ ()\-\u00a0]','',str(value or ''))
    return phone if re.fullmatch(r'\+[1-9][0-9]{7,14}',phone) else ''

def phone_from_text(text):
    """Conservative masks only: explicit +374/8 digits or +7/10 digits.
    Unknown country, local numbers, extensions and multiple numbers stay manual.
    This validates a format, not ownership or whether the number is in service.
    """
    text=str(text or '')[:12000];found=set()
    for m in re.finditer(r'(?<![\w+])\+[0-9](?:[0-9 ()\-\u00a0]*[0-9])?',text):
        raw=m.group();tail=text[m.end():]
        if tail and (tail[0].isalnum() or tail[0]=='_'):continue
        if re.match(r'\s*(?:доб\.?|ext\.?|#)\s*[0-9]',tail,re.I):continue
        # A closing parenthesis directly after the final digit belongs to this mask.
        if raw.count('(')>raw.count(')') and tail.startswith(')'):raw+=')'
        if raw.count('(')!=raw.count(')') or raw.count('(')>1:continue
        if '(' in raw and raw.index('(')>raw.index(')'):continue
        number=normalize_phone(raw)
        if number:found.add(number)
    number=next(iter(found)) if len(found)==1 else ''
    return number if re.fullmatch(r'(?:\+374[1-9][0-9]{7}|\+7[0-9]{10})',number) else ''

def dumps(v):return json.dumps(v,ensure_ascii=False,separators=(',',':'))
def norm(v):return re.sub(r'\s+',' ',str(v or '').lower().replace('ё','е')).strip()
def stamp(v):return datetime.fromtimestamp(v,timezone.utc).isoformat()
def enqueue(kind,payload,key):
    with db() as c:c.execute('INSERT OR IGNORE INTO jobs(kind,payload,jobkey,run_at) VALUES(?,?,?,?)',(kind,dumps(payload),key,time.time()))
def audit(uid,lid,action):
    with db() as c:c.execute('INSERT INTO audit(uid,lid,action,created) VALUES(?,?,?,?)',(uid,lid,action,time.time()))

def validate_init_data(raw:str,token:str,now:float|None=None)->dict:
    """HMAC verified on the server. Client-supplied user IDs/roles are never trusted."""
    if not raw or len(raw)>20000:raise ValueError('Нет Telegram-авторизации')
    pairs=parse_qsl(raw,keep_blank_values=True)
    if len({k for k,v in pairs})!=len(pairs):raise ValueError('Повторяющиеся поля авторизации')
    data=dict(pairs);given=data.pop('hash','')
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    check='\n'.join(f'{k}={v}' for k,v in sorted(data.items()))
    if not hmac.compare_digest(hmac.new(secret,check.encode(),hashlib.sha256).hexdigest(),given):raise ValueError('Неверная подпись Telegram')
    age=(now or time.time())-int(data.get('auth_date','0'))
    if age< -60 or age>86400:raise ValueError('Сессия устарела. Переоткройте приложение в Telegram.')
    user=json.loads(data.get('user','{}'))
    if type(user.get('id')) is not int or user['id']<=0:raise ValueError('Неверный пользователь')
    return user

def user(request:Request):
    if not LIVE:raise HTTPException(503,'Запись на сервере отключена')
    try:u=validate_init_data(request.headers.get('X-Telegram-Init-Data',''),TOKEN)
    except (ValueError,TypeError,json.JSONDecodeError) as e:raise HTTPException(401,str(e))
    with db() as c:c.execute('INSERT INTO users(uid,username,name) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET username=excluded.username,name=excluded.name',(u['id'],u.get('username',''),u.get('first_name','')))
    return u

def admin_user(u=Depends(user)):
    if u['id'] not in ADMINS:raise HTTPException(403,'Только для администраторов')
    return u

limits={}
def rate(uid,action,limit=20,window=3600):
    now=time.time();key=(uid,action);hits=[v for v in limits.get(key,[]) if now-v<window]
    if len(hits)>=limit:raise HTTPException(429,'Слишком часто. Попробуйте позже.')
    hits.append(now);limits[key]=hits
    if len(limits)>10000:
        for k in list(limits):
            if not limits[k] or now-limits[k][-1]>86400:limits.pop(k,None)

class Offer(BaseModel):
    amount:int=Field(gt=0,le=1_000_000_000)
    amount_max:int|None=Field(default=None,gt=0,le=1_000_000_000)
    condition:str=Field(default='',max_length=200)
    registration:Literal['yes','no','unknown']='unknown'
    pets:Literal['yes','no','unknown']='unknown'
    currency:Literal['AMD','USD']='AMD'
    period:Literal['month','day']
class PhotoRef(BaseModel):
    id:str=Field(pattern=r'^[a-f0-9]{32}$')
class ListingIn(BaseModel):
    model_config=ConfigDict(extra='ignore')
    address:str=Field(min_length=3,max_length=180)
    city:str=Field(default='Ереван',max_length=80)
    district:str=Field(default='',max_length=80)
    metro_walk_minutes:int|None=Field(default=None,ge=1,le=180)
    center_drive_minutes:int|None=Field(default=None,ge=1,le=360)
    kind:Literal['apartment','room','house','aparthotel']='apartment'
    rooms:int|None=Field(default=None,ge=0,le=20)
    area:float|None=Field(default=None,gt=0,le=5000)
    floor:str|None=Field(default=None,max_length=30)
    role:Literal['unknown','owner','tenant','agent']='unknown'
    commission:int|None=Field(default=None,ge=0,le=1_000_000_000)
    commission_max:int|None=Field(default=None,ge=0,le=1_000_000_000)
    commission_type:Literal['percent','fixed']='percent'
    commission_currency:Literal['AMD','USD']='AMD'
    commission_basis:Literal['month','day']='month'
    pets:Literal['yes','no','ask','unknown']='unknown'
    deposit:int|None=Field(default=None,ge=0,le=1_000_000_000)
    available:str|None=Field(default=None,max_length=10)
    available_until:str|None=Field(default=None,max_length=10)
    description:str=Field(default='',max_length=12000)
    contact:str=Field(default='',max_length=40)
    phone:str|None=Field(default=None,max_length=40)
    contract:Literal['yes','no','ask','unknown']='unknown'
    residence_registration:Literal['yes','no','ask','unknown']='unknown'
    lease_registration:Literal['yes','no','ask','unknown']='unknown'
    wishes:str=Field(default='',max_length=1200)
    prices:list[Offer]=Field(min_length=1,max_length=2)
    photos:list[PhotoRef]=Field(default_factory=list,max_length=10)

    @model_validator(mode='after')
    def commission_range(self):
        if self.commission_max is not None:
            if self.commission is None or self.commission_max<self.commission:raise ValueError('Верхняя граница комиссии должна быть не меньше нижней')
            if self.commission==0 and self.commission_max>0:raise ValueError('Для диапазона укажите положительную нижнюю границу комиссии')
        if self.commission_type=='percent' and max(self.commission or 0,self.commission_max or 0)>100:raise ValueError('Процент комиссии должен быть от 0 до 100')
        return self

class PrivateIn(BaseModel):
    note:str=Field(default='',max_length=500)
    applicant_name:str=Field(default='',max_length=140)
    document_number:str=Field(default='',max_length=80)
    document_password:str=Field(default='',max_length=100)
class Submission(BaseModel):
    listing:ListingIn
    private:PrivateIn=Field(default_factory=PrivateIn)
    consent:bool
class Filters(BaseModel):
    q:str=Field(default='',max_length=200)
    period:Literal['','month','day']=''
    currency:Literal['','AMD','USD']=''
    city:str=Field(default='',max_length=80)
    district:str=Field(default='',max_length=80)
    kind:Literal['','apartment','room','house','aparthotel']=''
    rooms:str=Field(default='',pattern=r'^(|0|1|2|3|4\+|4|5|6)$')
    max:str=Field(default='',pattern=r'^\d{0,10}$')
    market:Literal['free','paid']='free'
    owner:bool=False
    pets:bool=False
    contract:bool=False
    residence_registration:bool=False
    verified:bool=False
class FeedFilters(Filters):
    model_config=ConfigDict(extra='forbid',strict=True)
class SubscriptionIn(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    frequency:Literal['instant','daily']='instant'
    filters:Filters
class ActiveIn(BaseModel):active:bool
class StatusIn(BaseModel):status:Literal['active','rented']
class BanIn(BaseModel):reason:str=Field(min_length=3,max_length=500)
class DecisionIn(BaseModel):decision:Literal['approve','reject','document_checked']

def getrow(lid):
    with db() as c:r=c.execute('SELECT * FROM listings WHERE id=?',(lid,)).fetchone()
    if r is None:raise HTTPException(404,'Объявление не найдено')
    return r

def telegram_post_url(post):
    """Build links only from an actual Bot API publication result, never listing input.
    Syntax: https://core.telegram.org/api/links#message-links
    """
    if not isinstance(post,dict):return ''
    chat=post.get('chat') or {};message=post.get('message_id');thread=post.get('message_thread_id')
    if chat.get('type') not in ('channel','supergroup') or type(message) is not int or message<=0:return ''
    username=chat.get('username','');chat_id=str(chat.get('id',''))
    if re.fullmatch(r'[A-Za-z0-9_]{5,32}',username):base='https://t.me/'+username
    elif re.fullmatch(r'-100[1-9]\d*',chat_id):base='https://t.me/c/'+chat_id[4:]
    else:return ''
    return base+'/'+str(message)+(('?thread='+str(thread)) if type(thread) is int and thread>0 else '')

def listing(r,mine=False):
    d=json.loads(r['payload'])
    source=d.pop('_source_post',None)
    post=d.pop('_telegram_post',None) or source
    d.pop('_status_before_ban',None)
    for key in ('_source_key','_source_review','_source_deleted','_source_previous_status'):d.pop(key,None)
    d['view_count']=r['view_count']
    d['phone_listings_available']=bool(r['phone_key'] and d.get('role')!='agent')
    d['telegram_post_url']=telegram_post_url(post)
    d['telegram_discussion_url']=telegram_post_url(post) if isinstance(post,dict) and post.get('chat',{}).get('type')=='supergroup' and post.get('message_thread_id') else ''
    d['author_listings_available']=bool(r['uid'])
    for old_key in ('confirmed_at','expires_at','confirmation_by','source_author_id','moderator_note'):
        d.pop(old_key,None)
    d.update(id=r['id'],status=r['status'],created_at=stamp(r['created']),is_mine=mine,photo_count=d.get('photo_count') or len(d.get('photos',[])))
    if mine:
        d['review_reason']=r['reason']
        d['ban_reason']=r['reason'] if r['status']=='banned' else ''
    # Explicit public projection: never merge the private ciphertext, credentials, uid or fingerprint.
    return d

def selected_offer(l,f):
    return next((p for p in l['prices'] if (not f.get('period') or p['period']==f['period']) and (not f.get('currency') or p['currency']==f['currency']) and (not f.get('residence_registration') or p.get('registration')!='no') and (not f.get('pets') or (p.get('pets') if p.get('pets') in ('yes','no') else l.get('pets')) in ('yes','ask'))),None)

def matches(l,f):
    if l.get('status')!='active':return False
    for key in ['city','district','kind']:
        if f.get(key) and l.get(key)!=f[key]:return False
    rooms=f.get('rooms','')
    if rooms=='4+' and (l.get('rooms') is None or l['rooms']<4):return False
    if rooms not in ('','4+') and str(l.get('rooms'))!=rooms:return False
    market=f.get('market','free')
    if market=='free' and l.get('commission')!=0:return False
    if market=='paid' and not (l.get('role')=='agent' and (l.get('commission') or 0)>0):return False
    if f.get('owner') and l.get('role')!='owner':return False
    if f.get('contract') and l.get('contract')!='yes':return False
    if f.get('residence_registration') and l.get('residence_registration') not in ('yes','ask'):return False
    if f.get('verified') and l.get('document_status') not in ('owner_verified','representative_verified'):return False
    offer=selected_offer(l,f)
    if not offer:return False
    if f.get('max') and offer['amount']>int(f['max']):return False
    if f.get('q') and norm(f['q']) not in norm(' '.join(l.get(k,'') or '' for k in ['address','city','district','description'])):return False
    return True

def active_effects(lid):
    r=getrow(lid);l=listing(r)
    if json.loads(r['payload']).get('_source_post'):return
    enqueue('publish',{'id':lid},'publish:'+lid)
    with db() as c:subs=c.execute("SELECT * FROM subscriptions WHERE active=1 AND frequency='instant' AND created<=?",(time.time(),)).fetchall()
    for s in subs:
        if matches(l,json.loads(s['filters'])):enqueue('match',{'uid':s['uid'],'id':lid},f"match:{s['uid']}:{lid}")

def deletion_check_interval():
    return max(0,int(os.getenv('TELEGRAM_DELETION_CHECK_INTERVAL','0')))

@asynccontextmanager
async def lifespan(app):
    setup()
    if LIVE and (not TOKEN or not re.fullmatch(r'[A-Za-z0-9_]{5,32}',BOT) or not PUBLIC_URL.startswith('https://') or not ADMINS):
        raise RuntimeError('For LIVE=1 set BOT_TOKEN, BOT_USERNAME, HTTPS PUBLIC_URL and ADMIN_IDS in .env.')
    tasks=[]
    if LIVE:
        tasks=[asyncio.create_task(poll()),asyncio.create_task(worker())]
        if os.getenv('TELEGRAM_SYNC_ENABLED','0')=='1':
            tasks.extend([asyncio.create_task(source_reader_loop()),asyncio.create_task(source_worker())])
        elif deletion_check_interval():
            tasks.append(asyncio.create_task(run_deletion_checks(SOURCE_CATALOG,DATA,interval=deletion_check_interval())))
    try:yield
    finally:
        for t in tasks:t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):await t
app=FastAPI(title='Аренда в Армении',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.middleware('http')
async def headers(req,call_next):
    try:length=int(req.headers.get('content-length','0'))
    except ValueError:return JSONResponse({'detail':'Invalid length'},400)
    if length>11_000_000:return JSONResponse({'detail':'Максимум 10 МБ'},413)
    r=await call_next(req)
    r.headers['X-Content-Type-Options']='nosniff';r.headers['Referrer-Policy']='strict-origin-when-cross-origin' if req.url.path=='/' else 'no-referrer'
    if req.url.path.startswith('/api'):r.headers['Cache-Control']='no-store'
    return r

@app.exception_handler(RequestValidationError)
async def invalid_request(req:Request,exc:RequestValidationError):
    return JSONResponse({'detail':'Проверьте заполнение полей'},status_code=422)

@app.get('/')
async def index():
    s=(ROOT/'web/index.html').read_text(encoding='utf-8')
    return HTMLResponse(s)
@app.get('/api/config')
async def config():return {'live':LIVE,'bot_username':BOT if LIVE else '', 'version':'0.5.0','channel_configured':bool(LIVE and CHAT),'paid_channel_configured':bool(LIVE and PAID_CHAT),'source_enabled':os.getenv('TELEGRAM_SYNC_ENABLED','0')=='1','deletion_checks_enabled':bool(LIVE and deletion_check_interval()),'maps_enabled':bool(GEOCODER_URL)}

@app.get('/assets/maplibre-6.7.0/{filename}')
async def map_asset(filename:str):
    if filename not in ('maplibre-gl.mjs','maplibre-gl-shared.mjs','maplibre-gl-worker.mjs','maplibre-gl.css'):raise HTTPException(404)
    media_type='text/css' if filename.endswith('.css') else 'text/javascript'
    return FileResponse(ROOT/'web/vendor/maplibre-6.7.0'/filename,media_type=media_type,headers={'Cache-Control':'public, max-age=31536000, immutable'})


def map_candidates(rows):
    if not isinstance(rows,list):raise ValueError('Invalid geocoder response')
    result=[];seen=set()
    for row in rows[:3]:
        if not isinstance(row,dict) or not isinstance(row.get('address'),dict):continue
        try:
            lat=float(row['lat']);lon=float(row['lon']);address=row.get('address',{})
            if not (40.02<=lat<=40.30 and 44.36<=lon<=44.65) or address.get('country_code')!='am':continue
            if address.get('ISO3166-2-lvl4')!='AM-ER' and str(address.get('city','')).casefold() not in ('ереван','yerevan','երևան','երեւան'):continue
            label=str(row['display_name'])[:400];key=(round(lat,6),round(lon,6))
            if key in seen:continue
            seen.add(key)
            result.append({'lat':lat,'lon':lon,'label':label,'precision':'building' if address.get('house_number') else 'street' if row.get('addresstype') in ('road','street','pedestrian') else 'area'})
        except (KeyError,ValueError,TypeError):continue
    return result


async def geocode_address(address,area=False):
    key=hashlib.sha256((GEOCODER_URL+('|район|' if area else '|адрес|')+'Ереван|'+re.sub(r'\s+',' ',address).strip().casefold()).encode()).hexdigest()
    def cached():
        with db() as c:row=c.execute('SELECT payload FROM geocode_cache WHERE key=? AND expires>?',(key,time.time())).fetchone()
        return json.loads(row['payload']) if row else None
    value=cached()
    if value is not None:return value
    if GEOCODE_LOCK.locked():raise HTTPException(429,'Карта загружается. Попробуйте через секунду.',headers={'Retry-After':'1'})
    async with GEOCODE_LOCK:
        value=cached()
        if value is not None:return value
        with db() as c:
            row=c.execute("SELECT value FROM meta WHERE key='geocoder_next'").fetchone();wait=float(row['value'])-time.time() if row else 0
        if wait>2:raise HTTPException(503,'Сервис карты временно недоступен.',headers={'Retry-After':str(int(wait)+1)})
        if wait>0:await asyncio.sleep(wait)
        with db() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('geocoder_next',?)",(str(time.time()+1.1),))
        params={'street':address,'city':'Ереван','country':'Армения','countrycodes':'am','format':'jsonv2','addressdetails':1,'limit':3,'accept-language':'ru,hy,en','viewbox':'44.36,40.30,44.65,40.02','bounded':1}
        if area:
            for field in ('street','city','country'):params.pop(field)
            params['q']=address+', Ереван, Армения'
        try:
            async with httpx.AsyncClient(timeout=10,follow_redirects=False) as client:
                response=await client.get(GEOCODER_URL,params=params,headers={'User-Agent':'ArmeniaRent/0.5 (+https://github.com/berkutx/arm-rent-bot)'})
            if response.status_code==429:
                retry=response.headers.get('Retry-After','60');delay=max(60,int(retry) if retry.isdigit() else 60)
                with db() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('geocoder_next',?)",(str(time.time()+delay),))
            response.raise_for_status();value=map_candidates(response.json())
        except (httpx.HTTPError,ValueError,TypeError):raise HTTPException(503,'Сервис карты временно недоступен. Попробуйте позже.') from None
        with db() as c:
            c.execute('DELETE FROM geocode_cache WHERE expires<?',(time.time(),))
            c.execute('INSERT OR REPLACE INTO geocode_cache VALUES(?,?,?)',(key,dumps(value),time.time()+(30 if value else 1)*86400))
        return value


def location_key(d):return hashlib.sha256(dumps([d.get('city'),d.get('address'),d.get('district')]).encode()).hexdigest()


@app.get('/api/listings/{lid}/map')
async def listing_map(lid:str):
    r=getrow(lid);d=json.loads(r['payload']);key=location_key(d)
    if r['status'] not in ('active','rented') or d.get('city')!='Ереван':raise HTTPException(404,'Карта доступна для объявлений Еревана')
    if not GEOCODER_URL:raise HTTPException(503,'Карта временно недоступна')
    with db() as c:stored=c.execute('SELECT payload FROM listing_locations WHERE lid=? AND address_key=?',(lid,key)).fetchone()
    if stored:point=json.loads(stored['payload'])
    else:
        points=await geocode_address(d['address']);point=points[0] if points else None
        if point and len(points)>1:point={**point,'precision':'area'}
        if not point and d.get('district'):
            points=await geocode_address(d['district'],area=True)
            if points:point={**points[0],'precision':'district','district':d['district']}
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            latest=c.execute('SELECT * FROM listings WHERE id=?',(lid,)).fetchone()
            if not latest or latest['status'] not in ('active','rented') or location_key(json.loads(latest['payload']))!=key:raise HTTPException(409,'Объявление изменилось. Откройте карточку заново.')
            c.execute('INSERT OR REPLACE INTO listing_locations VALUES(?,?,?,?)',(lid,key,dumps(point),time.time()))
    return {'state':'resolved' if point else 'not_found','points':[point] if point else [],'style_url':os.getenv('MAP_STYLE_URL','https://tiles.openfreemap.org/styles/liberty')}

@app.get('/healthz')
async def health():
    with db() as c:c.execute('SELECT 1')
    return {'ok':True,'mode':'live' if LIVE else 'local','version':'0.5.0'}
@app.get('/api/me')
async def me(u=Depends(user)):
    profile=get_agent_profile(u['id'])
    return {'id':u['id'],'username':u.get('username',''),'first_name':u.get('first_name',''),'last_name':u.get('last_name',''),'is_admin':u['id'] in ADMINS,'agent_profile_ready':bool(profile),'agent_affiliation':agent_affiliation(profile)}
class AgentProfileIn(BaseModel):
    full_name:str=Field(min_length=3,max_length=120)
    phone:str=Field(min_length=8,max_length=40)
    independent:bool=False
    agency:str=Field(default='',max_length=120)

def get_agent_profile(uid):
    with db() as c:row=c.execute('SELECT encrypted FROM agent_profiles WHERE uid=?',(uid,)).fetchone()
    return json.loads(CIPHER.decrypt(row['encrypted'])) if row else None

def agent_affiliation(profile):
    return ('Частный агент' if profile['independent'] else profile['agency']) if profile else ''

@app.get('/api/agent-profile')
async def my_agent_profile(u=Depends(user)):
    return get_agent_profile(u['id']) or {}

@app.put('/api/agent-profile')
async def save_agent_profile(x:AgentProfileIn,u=Depends(user)):
    rate(u['id'],'agent-profile',20)
    profile=x.model_dump();profile['full_name']=' '.join(x.full_name.split());profile['phone']=normalize_phone(x.phone)
    profile['agency']='' if x.independent else x.agency.strip()
    if len(profile['full_name'].split())<2:raise HTTPException(400,'Укажите имя и фамилию')
    if not profile['phone']:raise HTTPException(400,'Укажите телефон с кодом страны')
    if not x.independent and len(profile['agency'])<2:raise HTTPException(400,'Укажите агентство или выберите частного агента')
    with db() as c:c.execute('INSERT INTO agent_profiles VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET encrypted=excluded.encrypted,updated=excluded.updated',(u['id'],CIPHER.encrypt(dumps(profile).encode()),time.time()))
    return {'ready':True,'affiliation':agent_affiliation(profile)}

@app.get('/api/admin/{lid}/agent-profile')
async def admin_agent_profile(lid:str,u=Depends(admin_user)):
    row=getrow(lid);profile=get_agent_profile(row['uid'])
    if json.loads(row['payload']).get('role')!='agent' or not profile:raise HTTPException(404,'Профиль агента отсутствует')
    return profile

@app.get('/api/listings')
async def listings():
    with db() as c:rs=c.execute("SELECT * FROM listings WHERE status='active' ORDER BY created DESC LIMIT 500").fetchall()
    return [listing(r) for r in rs]
FEED_FIELDS=('id','status','created_at','is_mine','city','district','address','kind','subtype','rooms','area',
    'prices','pets','role','commission','commission_max','commission_type','commission_currency','commission_basis',
    'available','available_until','residence_registration','contract','document_status',
    'metro_walk_minutes','center_drive_minutes','view_count')

def feed_card(d):
    card={key:d[key] for key in FEED_FIELDS if key in d}
    photos=d.get('photos') or []
    card.update(photos=photos[:3],photo_count=len(photos))
    return card

def feed_cursor(revision,query,offset):
    raw=dumps([1,revision,query,offset]).encode()
    encoded=base64.urlsafe_b64encode(raw).decode().rstrip('=')
    return encoded+'.'+hmac.new(VIEW_SECRET,b'feed-cursor:'+raw,hashlib.sha256).hexdigest()

def feed_offset(cursor,revision,query):
    if not cursor:return 0
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.[a-f0-9]{64}',cursor):raise ValueError()
        encoded,signature=cursor.split('.')
        raw=base64.b64decode(encoded+'='*(-len(encoded)%4),altchars=b'-_',validate=True)
        expected=hmac.new(VIEW_SECRET,b'feed-cursor:'+raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature,expected):raise ValueError()
        value=json.loads(raw)
        if not isinstance(value,list) or len(value)!=4 or type(value[0]) is not int or value[0]!=1:raise ValueError()
        if any(not isinstance(v,str) or not re.fullmatch(r'[a-f0-9]{64}',v) for v in value[1:3]):raise ValueError()
        if type(value[3]) is not int or value[3]<1:raise ValueError()
    except (ValueError,binascii.Error,UnicodeError):
        raise HTTPException(422,'Некорректная страница каталога') from None
    if value[1]!=revision or value[2]!=query:
        raise HTTPException(409,'Каталог изменился. Обновите ленту.')
    return value[3]

@app.get('/api/feed')
async def feed(filters:str=Query(default='{}',max_length=4096),sort:Literal['new','price']='new',
               limit:int=Query(default=12,ge=1,le=48),cursor:str|None=Query(default=None,max_length=512)):
    try:f=FeedFilters.model_validate_json(filters).model_dump()
    except ValidationError:raise HTTPException(422,'Проверьте фильтры каталога') from None
    # Filter and facet the complete catalog in one read; no photo queries or network calls.
    with db() as c:
        rows=c.execute("SELECT id,uid,payload,status,created,phone_key,view_count FROM listings WHERE status='active' ORDER BY created DESC,id DESC").fetchall()
    digest=hashlib.sha256()
    for row in rows:
        # Views change independently of catalog contents and must not invalidate scrolling.
        digest.update(dumps([row[key] for key in ('id','uid','payload','status','created','phone_key')]).encode())
        digest.update(b'\n')
    revision=digest.hexdigest()
    query=hashlib.sha256(dumps([f,sort]).encode()).hexdigest()
    offset=feed_offset(cursor,revision,query)
    records=[listing(row) for row in rows]
    matched=[d for d in records if matches(d,f)]
    if sort=='price':
        def price_key(d):
            offer=selected_offer(d,f)
            return offer['currency'],offer['period'],offer['amount']
        # Python's stable sort keeps created DESC,id DESC for equal selected prices.
        matched.sort(key=price_key)
    if offset>len(matched):raise HTTPException(422,'Некорректная страница каталога')
    district_records=[d for d in records if matches(d,{**f,'city':'Ереван','district':''})]
    districts={}
    for d in district_records:
        if d.get('district'):districts[d['district']]=districts.get(d['district'],0)+1
    end=offset+limit
    return {'listings':[feed_card(d) for d in matched[offset:end]],'total':len(matched),
        'next_cursor':feed_cursor(revision,query,end) if end<len(matched) else None,
        'markets':{market:sum(matches(d,{'market':market}) for d in records) for market in ('free','paid')},
        'districts':districts,'district_total':len(district_records),
        'cities':sorted({d['city'] for d in records if d.get('city')})}

@app.get('/api/listings/{lid}')
async def public_listing(lid:str):
    r=getrow(lid)
    if r['status'] not in ('active','rented'):raise HTTPException(404,'Объявление недоступно')
    return listing(r)

@app.get('/api/listings/{lid}/author-listings')
async def author_listings(lid:str):
    r=getrow(lid)
    if r['status'] not in ('active','rented'):raise HTTPException(404,'Объявление недоступно')
    if not listing(r)['author_listings_available']:return {'available':False,'listings':[]}
    with db() as c:
        rows=c.execute("SELECT * FROM listings WHERE uid=? AND id<>? AND status='active' ORDER BY created DESC,id DESC LIMIT 500",(r['uid'],lid)).fetchall()
    return {'available':True,'listings':[listing(row) for row in rows]}

@app.get('/api/listings/{lid}/phone-listings')
async def phone_listings(lid:str):
    r=getrow(lid)
    if r['status'] not in ('active','rented'):raise HTTPException(404,'Объявление недоступно')
    if not listing(r)['phone_listings_available']:return {'available':False,'listings':[]}
    with db() as c:
        rows=c.execute("SELECT * FROM listings WHERE phone_key=? AND id<>? AND status='active' AND COALESCE(json_extract(payload,'$.role'),'unknown')<>'agent' ORDER BY created DESC,id DESC LIMIT 500",(r['phone_key'],lid)).fetchall()
    return {'available':True,'listings':[listing(row) for row in rows]}

@app.post('/api/listings/{lid}/view')
async def record_view(lid:str,u=Depends(user)):
    # Only server-authenticated Telegram IDs count. Store a per-listing HMAC, not the ID.
    rate(u['id'],'views',240,60)
    key=hmac.new(VIEW_SECRET,(lid+':'+str(u['id'])).encode(),hashlib.sha256).digest()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM listings WHERE id=?',(lid,)).fetchone()
        if not r or r['status'] not in ('active','rented'):raise HTTPException(404,'Объявление недоступно')
        added=c.execute('INSERT OR IGNORE INTO listing_views(lid,viewer_key) VALUES(?,?)',(lid,key)).rowcount
        if added:c.execute('UPDATE listings SET view_count=view_count+1 WHERE id=?',(lid,))
        count=c.execute('SELECT view_count FROM listings WHERE id=?',(lid,)).fetchone()[0]
    return {'view_count':count}

@app.get('/api/mine')
async def mine(u=Depends(user)):
    with db() as c:rs=c.execute('SELECT * FROM listings WHERE uid=? ORDER BY created DESC',(u['id'],)).fetchall()
    return [listing(r,True) for r in rs]

@app.post('/api/listings')
async def submit(s:Submission,u=Depends(user)):
    if not s.consent:raise HTTPException(400,'Подтвердите право публикации')
    rate(u['id'],'submit',10)
    d=s.listing.model_dump();d['address']=d['address'].strip()
    d['city']=d['city'].strip() or 'Ереван'
    if d['city']!='Ереван':
        d['district']=''
        d['metro_walk_minutes']=None
    if len(d['address'])<3:raise HTTPException(400,'Укажите адрес')
    if d['kind']!='house' and not re.search(r'\d',d['address']):raise HTTPException(400,'Укажите улицу и номер дома. Номер квартиры не нужен.')
    if d['kind']=='apartment' and d['rooms'] is None:raise HTTPException(400,'Укажите число комнат или студию')
    username=u.get('username','')
    d['contact']='@'+username if re.fullmatch(r'[A-Za-z0-9_]{5,32}',username) else ''
    d['phone']=phone_from_text(d['description']) if d['phone'] is None else re.sub(r'[ ()\-\u00a0]','',d['phone'])
    if d['phone'] and not re.fullmatch(r'\+[1-9]\d{7,14}',d['phone']):raise HTTPException(400,'Номер нужен в международном формате: +374…')
    if d['role']=='agent':
        profile=get_agent_profile(u['id'])
        if not profile:raise HTTPException(400,'Заполните профиль агента один раз: имя, телефон и агентство')
        d['agent_affiliation']=agent_affiliation(profile)
    if d['city']=='Ереван' and d['district'] and d['district'] not in {x['name'] for x in json.loads((ROOT/'web/districts.json').read_text(encoding='utf-8'))}:raise HTTPException(400,'Выберите район из списка')
    for price in d['prices']:
        if price.get('amount_max') and price['amount_max']<price['amount']:raise HTTPException(400,'Верхняя цена меньше нижней')
    for field in ('available','available_until'):
        if not d[field]:
            d[field]=None
            continue
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',d[field]):raise ValueError
            datetime.strptime(d[field],'%Y-%m-%d')
        except ValueError:raise HTTPException(400,'Укажите полную корректную дату: день, месяц и год')
    if d['available'] and d['available_until'] and d['available_until']<d['available']:
        raise HTTPException(400,'Дата окончания должна быть не раньше начала')
    if s.private.document_number or s.private.document_password:raise HTTPException(409,'Проверка через сервис пока недоступна. Откройте официальный e-cadastre.')
    with db() as c:
        photos=[]
        for p in d['photos']:
            r=c.execute('SELECT * FROM photos WHERE id=? AND uid=?',(p['id'],u['id'])).fetchone()
            if not r:raise HTTPException(400,'Одна из фотографий недоступна')
            photos.append(photo_details(r['id']))
        d['photos']=photos
        fp=hashlib.sha256(dumps(d).encode()).hexdigest()
        old=c.execute('SELECT * FROM listings WHERE uid=? AND fingerprint=?',(u['id'],fp)).fetchone()
        if old:return listing(old,True)
        reasons=[]
        if d['commission'] is None:raise HTTPException(400,'Укажите комиссию: 0 — без комиссии')
        if d['commission_type']=='percent' and d['commission']>100:raise HTTPException(400,'Процент комиссии должен быть от 0 до 100')
        if d['commission']>0 and d['role']!='agent':raise HTTPException(400,'С комиссией могут размещать только агенты')
        for r in c.execute("SELECT payload FROM listings WHERE status IN ('active','review','banned')"):
            p=json.loads(r['payload'])
            if norm(p['address'])==norm(d['address']) and p['city']==d['city'] and p['kind']==d['kind'] and p['rooms']==d['rooms']:
                reasons.append('Похожий адрес, тип жилья и комнаты уже есть');break
        if photos:
            hashes={r['sha'] for r in c.execute('SELECT sha FROM photos WHERE id IN ('+','.join('?'*len(photos))+')',[p['id'] for p in photos])}
            used=c.execute('SELECT DISTINCT p.sha FROM photos p JOIN listings l ON p.uid=l.uid WHERE l.status=\'active\' AND p.uid<>?',(u['id'],)).fetchall()
            if hashes.intersection(r['sha'] for r in used):reasons.append('Такая фотография есть у другого автора')
        lid=secrets.token_hex(8);now=time.time();status='review' if reasons else 'active'
        d['document_status']='none'
        private=CIPHER.encrypt(dumps(s.private.model_dump()).encode()) if any(s.private.model_dump().values()) else None
        c.execute('INSERT INTO listings(id,uid,payload,status,created,confirmed,fingerprint,private,private_expires,reason,phone_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(lid,u['id'],dumps(d),status,now,None,fp,private,now+7*86400,'; '.join(reasons),normalize_phone(d['phone'])))
    with db() as c:c.execute('DELETE FROM drafts WHERE uid=?',(u['id'],))
    if status=='active':active_effects(lid)
    if reasons or private:enqueue('admin',{'id':lid},'admin:'+lid)
    return listing(getrow(lid),True)

def photo_sizes(pid):
    with db() as c:r=c.execute('SELECT sizes FROM photos WHERE id=?',(pid,)).fetchone()
    if not r or not r['sizes']:raise HTTPException(404,'Фото недоступно')
    return json.loads(r['sizes'])

def photo_details(pid):
    sizes=photo_sizes(pid);full=sizes['full']
    return {'id':pid,'url':f'/media/{pid}.jpg','thumb_url':f'/media/{pid}-thumb.jpg','width':full['width'],'height':full['height']}

def store_telegram_photo(sizes,uid):
    sizes=[{k:p[k] for k in ('file_id','file_unique_id','width','height','file_size') if k in p} for p in sizes if p.get('file_id') and p.get('file_unique_id') and p.get('width',0)>0 and p.get('height',0)>0 and p.get('file_size',0)<=20_000_000]
    if not sizes:raise HTTPException(400,'Telegram не предоставил фотографию')
    full=max(sizes,key=lambda p:p['width']*p['height'])
    thumb=min(sizes,key=lambda p:abs(max(p['width'],p['height'])-640))
    pid=secrets.token_hex(16)
    with db() as c:c.execute('INSERT INTO photos(id,uid,sha,tg_file_id,created,sizes) VALUES(?,?,?,?,?,?)',(pid,uid,full['file_unique_id'],full['file_id'],time.time(),dumps({'full':full,'thumb':thumb})))
    return photo_details(pid)

async def telegram_file_path(file_id):
    cached=FILE_PATHS.get(file_id)
    if cached and cached[1]>time.monotonic():return cached[0]
    file=await tg('getFile',{'file_id':file_id});path=file.get('file_path','')
    if not re.fullmatch(r'photos/[A-Za-z0-9_.-]+',path) or '..' in path:raise HTTPException(502,'Фото временно недоступно')
    if len(FILE_PATHS)>=512:FILE_PATHS.pop(next(iter(FILE_PATHS)))
    FILE_PATHS[file_id]=(path,time.monotonic()+3000)
    return path

def public_photo_url(url):
    return bool(re.fullmatch(r'https://cdn[0-9]+\.(?:telesco\.pe|cdn-telegram\.org)/file/[A-Za-z0-9_./?=&%-]+',url or ''))

class TelegramAlbum(HTMLParser):
    def __init__(self):
        super().__init__();self.posts=set();self.photos={}
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if attrs.get('data-post'):self.posts.add(attrs['data-post'])
        if tag!='a' or 'tgme_widget_message_photo_wrap' not in attrs.get('class','').split():return
        match=re.search(r"background-image:url\(['\"]([^'\"]+)['\"]\)",attrs.get('style',''))
        if match and public_photo_url(match[1]):self.photos[attrs.get('href','').split('?')[0]]=match[1]

SOURCE_PHOTO_REQUESTS=asyncio.Semaphore(4)
SOURCE_PHOTO_INFLIGHT={}

async def load_source_album(source):
    try:
        async with SOURCE_PHOTO_REQUESTS:
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    async with client.stream('GET',source+'?embed=1') as response:
                        response.raise_for_status();raw=bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw)>1_000_000:raise ValueError()
                album=TelegramAlbum();album.feed(raw.decode('utf-8'))
                if source.removeprefix('https://t.me/') not in album.posts or not album.photos:raise ValueError()
            except (httpx.HTTPError,ValueError):raise HTTPException(502,'Фото временно недоступно') from None
        if len(FILE_PATHS)>=512:FILE_PATHS.pop(next(iter(FILE_PATHS)))
        FILE_PATHS['source:'+source]=(album.photos,time.monotonic()+3000)
        return album.photos
    finally:
        SOURCE_PHOTO_INFLIGHT.pop(source,None)

async def source_photo_url(photo):
    source=photo.get('source_post','');message=photo.get('source_message','')
    if not re.fullmatch(r'https://t.me/[A-Za-z0-9_]{5,32}/[1-9][0-9]*',source):raise HTTPException(404)
    if public_photo_url(photo.get('url')) and 0<=time.time()-photo.get('resolved_at',0)<3000:return photo['url']
    key='source:'+source;cached=FILE_PATHS.get(key)
    if cached and cached[1]>time.monotonic():photos=cached[0]
    else:
        task=SOURCE_PHOTO_INFLIGHT.get(source)
        if task is None:
            task=asyncio.create_task(load_source_album(source))
            SOURCE_PHOTO_INFLIGHT[source]=task
            # Consume errors even if every request awaiting this shared load disconnects.
            task.add_done_callback(lambda done:None if done.cancelled() else done.exception())
        photos=await asyncio.shield(task)
    if message not in photos:
        FILE_PATHS.pop(key,None)
        raise HTTPException(404,'Фото отсутствует в исходном посте')
    return photos[message]

@app.get('/media/{filename}')
async def media(filename:str):
    match=re.fullmatch(r'([a-f0-9]{32})(-thumb)?\.jpg',filename)
    if not match:raise HTTPException(404)
    photo=photo_sizes(match[1])['thumb' if match[2] else 'full']
    if photo.get('source_key'):
        with db() as c:visible=c.execute("SELECT 1 FROM listings l,json_each(l.payload,'$.photos') p WHERE l.status IN ('active','rented') AND json_extract(l.payload,'$._source_key')=? AND json_extract(p.value,'$.id')=?",(photo['source_key'],match[1])).fetchone()
        if not visible:raise HTTPException(404,'Фото удалено из объявления')
    if photo.get('source_post'):
        return RedirectResponse(await source_photo_url(photo),status_code=302,headers={'Cache-Control':'public, max-age=300'})
    return await stream_telegram_photo(photo)

async def stream_telegram_photo(photo,private=False):
    client=httpx.AsyncClient(timeout=30)
    try:
        for attempt in range(2):
            path=await telegram_file_path(photo['file_id'])
            response=await client.send(client.build_request('GET',f'https://api.telegram.org/file/bot{TOKEN}/'+path),stream=True)
            if response.status_code!=404 or attempt:break
            await response.aclose();FILE_PATHS.pop(photo['file_id'],None)
        if response.status_code!=200 or int(response.headers.get('content-length','0'))>20_000_000:
            await response.aclose();raise HTTPException(502,'Фото временно недоступно')
    except (TelegramError,httpx.HTTPError,HTTPException,ValueError):
        await client.aclose();raise HTTPException(502,'Фото временно недоступно') from None
    async def content():
        size=0
        try:
            async for chunk in response.aiter_bytes(65536):
                size+=len(chunk)
                if size>20_000_000:raise RuntimeError('Photo transfer exceeded size limit')
                yield chunk
        except httpx.HTTPError:raise RuntimeError('Photo transfer interrupted') from None
        finally:
            await response.aclose();await client.aclose()
    headers={'Cache-Control':'no-store' if private else 'public, max-age=86400','X-Content-Type-Options':'nosniff'}
    if response.headers.get('content-length'):headers['Content-Length']=response.headers['content-length']
    return StreamingResponse(content(),media_type='image/jpeg',headers=headers)

@app.get('/api/subscriptions')
async def subscriptions(u=Depends(user)):
    with db() as c:ss=[dict(r) for r in c.execute('SELECT id,name,filters,frequency,active FROM subscriptions WHERE uid=?',(u['id'],))]
    for sub in ss:sub['filters']=json.loads(sub['filters']);sub['active']=bool(sub['active'])
    return ss
@app.post('/api/subscriptions')
async def subscribe(s:SubscriptionIn,u=Depends(user)):
    rate(u['id'],'subscriptions',20)
    f=dumps(s.filters.model_dump())
    with db() as c:
        old=c.execute('SELECT id FROM subscriptions WHERE uid=? AND filters=?',(u['id'],f)).fetchone()
        if old:return {'id':old['id']}
        sid=secrets.token_hex(8);c.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?)',(sid,u['id'],s.name,f,s.frequency,1,time.time(),''))
    return {'id':sid}
@app.patch('/api/subscriptions/{sid}')
async def sub_toggle(sid:str,x:ActiveIn,u=Depends(user)):
    with db() as c:c.execute('UPDATE subscriptions SET active=? WHERE id=? AND uid=?',(int(x.active),sid,u['id']))
    return {'ok':True}
@app.delete('/api/subscriptions/{sid}')
async def sub_delete(sid:str,u=Depends(user)):
    with db() as c:c.execute('DELETE FROM subscriptions WHERE id=? AND uid=?',(sid,u['id']))
    return {'ok':True}

def status_change(lid,uid,status):
    rate(uid,'status',30,60)
    r=getrow(lid)
    if uid!=r['uid'] and uid not in ADMINS:raise HTTPException(403,'Это не ваше объявление')
    if r['status'] not in ('active','rented'):raise HTTPException(403,'Сначала нужно решение модератора')
    if status not in ('active','rented'):raise HTTPException(400,'Неизвестный статус')
    if status==r['status']:return listing(r,True)
    # Manual status change only. Publication timestamp remains immutable.
    with db() as c:c.execute('UPDATE listings SET status=? WHERE id=?',(status,lid))
    if r['channel_message']:enqueue('edit',{'id':lid},f'edit:{lid}:{secrets.token_hex(4)}')
    audit(uid,lid,'status:'+status)
    return listing(getrow(lid),True)
@app.post('/api/listings/{lid}/status')
async def set_status(lid:str,s:StatusIn,u=Depends(user)):return status_change(lid,u['id'],s.status)
class ReportIn(BaseModel):
    reason:str=Field(default='',max_length=200)
    details:str=Field(default='',max_length=2000)
    evidence:str=Field(default='',max_length=2000)

class ReportSubmit(ReportIn):
    report_id:str=Field(pattern=r'^[a-f0-9]{16}$')

class ReportDecision(BaseModel):
    outcome:Literal['dismiss','ban']
    reason:str=Field(min_length=3,max_length=500)

def report_row(rid,uid):
    with db() as c:r=c.execute('SELECT * FROM reports WHERE id=?',(rid,)).fetchone()
    if not r or (r['uid']!=uid and uid not in ADMINS):raise HTTPException(404,'Жалоба не найдена')
    return r

def report_data(r):return json.loads(CIPHER.decrypt(r['payload']))

def report_view(r):
    d=report_data(r);l=listing(getrow(r['lid']))
    return {'id':r['id'],'listing':l,'status':r['status'],'created_at':stamp(r['created']),
            'reason':d['reason'],'details':d['details'],'evidence':d['evidence'],
            'reporter':{**d['reporter'],'id':r['uid']},'resolution':r['resolution'],'outcome':r['outcome'],
            'photos':[{'id':p['id'],'url':f"/api/reports/{r['id']}/photos/{p['id']}"} for p in d['photos']]}

@app.post('/api/listings/{lid}/report-draft')
async def report_draft(lid:str,u=Depends(user)):
    if getrow(lid)['status']!='active':raise HTTPException(404,'Объявление недоступно')
    rate(u['id'],'report-draft',30)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute("SELECT * FROM reports WHERE uid=? AND lid=? AND status='draft'",(u['id'],lid)).fetchone()
        if not r:
            rid=secrets.token_hex(8)
            d={'reason':'','details':'','evidence':'','photos':[],'reporter':{'name':u.get('first_name',''),'username':u.get('username','')}}
            c.execute('INSERT INTO reports(id,lid,uid,payload,status,created) VALUES(?,?,?,?,?,?)',(rid,lid,u['id'],CIPHER.encrypt(dumps(d).encode()),'draft',time.time()))
            r=c.execute('SELECT * FROM reports WHERE id=?',(rid,)).fetchone()
    return report_view(r)

@app.get('/api/reports/{rid}')
async def get_report(rid:str,u=Depends(user)):return report_view(report_row(rid,u['id']))

@app.put('/api/reports/{rid}')
async def save_report(rid:str,x:ReportIn,u=Depends(user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=report_row(rid,u['id'])
        if r['uid']!=u['id'] or r['status']!='draft':raise HTTPException(409,'Жалоба уже отправлена')
        d=report_data(r);d.update(x.model_dump())
        c.execute('UPDATE reports SET payload=? WHERE id=?',(CIPHER.encrypt(dumps(d).encode()),rid))
    return report_view(report_row(rid,u['id']))

@app.delete('/api/reports/{rid}/photos/{pid}')
async def remove_report_photo(rid:str,pid:str,u=Depends(user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=report_row(rid,u['id'])
        if r['uid']!=u['id'] or r['status']!='draft':raise HTTPException(409,'Жалоба уже отправлена')
        d=report_data(r);d['photos']=[p for p in d['photos'] if p['id']!=pid]
        c.execute('UPDATE reports SET payload=? WHERE id=?',(CIPHER.encrypt(dumps(d).encode()),rid))
    return {'ok':True}

@app.get('/api/reports/{rid}/photos/{pid}')
async def report_photo(rid:str,pid:str,u=Depends(user)):
    d=report_data(report_row(rid,u['id']))
    photo=next((p for p in d['photos'] if p['id']==pid),None)
    if not photo:raise HTTPException(404,'Фото не найдено')
    return await stream_telegram_photo(photo,private=True)

@app.post('/api/listings/{lid}/report')
async def report(lid:str,x:ReportSubmit,u=Depends(user)):
    if len(x.reason.strip())<3 or len(x.details.strip())<10:raise HTTPException(400,'Укажите причину и опишите, что произошло')
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=report_row(x.report_id,u['id'])
        if r['uid']!=u['id'] or r['lid']!=lid:raise HTTPException(404,'Жалоба не найдена')
        if r['status']!='draft':return {'ok':True,'id':r['id']}
        rate(u['id'],'report',10)
        d=report_data(r);d.update({k:getattr(x,k).strip() for k in ('reason','details','evidence')})
        d['reporter']={'name':u.get('first_name',''),'username':u.get('username','')}
        now=time.time()
        c.execute("UPDATE reports SET status='pending',payload=?,created=? WHERE id=?",(CIPHER.encrypt(dumps(d).encode()),now,r['id']))
        c.execute('INSERT INTO audit(uid,lid,action,created) VALUES(?,?,?,?)',(u['id'],lid,'report_submitted',now))
        c.execute('INSERT INTO jobs(kind,payload,jobkey,run_at) VALUES(?,?,?,?)',('report',dumps({'id':lid,'report_id':r['id']}),'report:'+r['id'],now))
    return {'ok':True,'id':r['id']}

@app.get('/api/admin/reports')
async def admin_reports(u=Depends(admin_user)):
    with db() as c:rs=c.execute("SELECT * FROM reports WHERE status!='draft' ORDER BY status='pending' DESC,created DESC LIMIT 200").fetchall()
    return [report_view(r) for r in rs]

@app.post('/api/admin/reports/{rid}/resolve')
async def resolve_report(rid:str,x:ReportDecision,u=Depends(admin_user)):
    reason=x.reason.strip()
    if len(reason)<3:raise HTTPException(400,'Укажите причину решения')
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=report_row(rid,u['id'])
        if r['status']!='pending':raise HTTPException(409,'Жалоба уже рассмотрена или ещё не отправлена')
        if x.outcome=='ban':ban_in_db(c,r['lid'],reason)
        now=time.time()
        c.execute("UPDATE reports SET status='resolved',resolved=?,resolved_by=?,resolution=?,outcome=? WHERE id=?",(now,u['id'],reason,x.outcome,rid))
        c.execute('INSERT INTO audit(uid,lid,action,created) VALUES(?,?,?,?)',(u['id'],r['lid'],'report_'+x.outcome,now))
    if x.outcome=='ban' and getrow(r['lid'])['channel_message']:enqueue('edit',{'id':r['lid']},'report-ban:'+rid)
    return report_view(report_row(rid,u['id']))

@app.get('/api/admin/source')
async def source_status(u=Depends(admin_user)):
    with db() as c:
        counts={r['state']:r['n'] for r in c.execute('SELECT state,COUNT(*) n FROM source_posts GROUP BY state')}
        staged=c.execute("SELECT COUNT(*) FROM listings WHERE status='source_staged'").fetchone()[0]
    return {'enabled':os.getenv('TELEGRAM_SYNC_ENABLED','0')=='1','connection':SOURCE_STORE.get('connection',{'state':'disabled'}),
            'history_complete':SOURCE_STORE.get('history_complete',False),'activated':SOURCE_STORE.get('activated',False),
            'deletion_check':SOURCE_STORE.get('deletion_check'),'history_checked':SOURCE_STORE.get('history_checked'),'reconciled_at':SOURCE_STORE.get('reconciled_at'),'counts':counts,'staged':staged}

@app.get('/api/admin/source/posts')
async def source_posts(u=Depends(admin_user)):
    with db() as c:rows=c.execute("SELECT * FROM source_posts WHERE state='review' ORDER BY updated DESC LIMIT 200").fetchall()
    return [SOURCE_CATALOG.view(r) for r in rows]

class SourceDecision(BaseModel):
    revision:str=Field(pattern='^[a-f0-9]{64}$')
    fields:ListingIn|None=None
    ignore:bool=False

@app.post('/api/admin/source/posts/{key}')
async def source_decision(key:str,x:SourceDecision,u=Depends(admin_user)):
    snapshot=SOURCE_STORE.snapshot(key)
    if not snapshot['root']:raise HTTPException(404,'Пост не найден')
    if snapshot['revision']!=x.revision:raise HTTPException(409,'Пост изменился. Откройте последнюю версию')
    try:
        if x.ignore:SOURCE_CATALOG.hold(snapshot,'ignored','Пропущено администратором')
        elif x.fields:SOURCE_CATALOG.apply(snapshot,x.fields.model_dump())
        else:raise HTTPException(400,'Заполните поля объявления')
    except ValueError as error:raise HTTPException(400,str(error)) from None
    audit(u['id'],'','source_review')
    return {'ok':True}

@app.post('/api/admin/source/activate')
async def source_activate(u=Depends(admin_user)):
    try:return SOURCE_CATALOG.activate()
    except ValueError as error:raise HTTPException(409,str(error)) from None

@app.get('/api/catalog-events')
async def catalog_events(request:Request):
    async def changes():
        last=None
        while not await request.is_disconnected():
            CATALOG_CHANGED.clear()
            revision=SOURCE_STORE.get('catalog_revision','0')
            if revision!=last:
                last=revision;yield 'data: '+dumps({'revision':revision})+'\n\n'
            else:yield ': keepalive\n\n'
            with contextlib.suppress(asyncio.TimeoutError):await asyncio.wait_for(CATALOG_CHANGED.wait(),15)
    return StreamingResponse(changes(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

async def source_reader_loop():
    while True:
        try:await run_reader(SOURCE_STORE,DATA)
        except asyncio.CancelledError:raise
        except OSError:
            SOURCE_STORE.set('connection',{'state':'reader_busy'})
        except Exception as error:
            log.warning('Source reader failed (%s)',type(error).__name__)
            SOURCE_STORE.set('connection',{'state':'disconnected'})
        await asyncio.sleep(30)

async def source_worker():
    while True:
        try:
            if not SOURCE_CATALOG.process_one():await asyncio.sleep(1)
            else:await asyncio.sleep(0)
        except asyncio.CancelledError:raise
        except Exception as error:
            log.warning('Source processing failed (%s)',type(error).__name__);await asyncio.sleep(5)

@app.get('/api/admin/queue')
async def queue(u=Depends(admin_user)):
    with db() as c:rs=c.execute("SELECT * FROM listings WHERE status='review' OR private IS NOT NULL ORDER BY created").fetchall()
    return [listing(r,True) for r in rs]
@app.get('/api/admin/listings')
async def admin_listings(status:Literal['all','banned']='all',u=Depends(admin_user)):
    with db() as c:
        rows=c.execute("SELECT * FROM listings WHERE (?='all' OR status=?) ORDER BY created DESC LIMIT 500",(status,status)).fetchall()
    return [listing(r,True) for r in rows]

def ban_in_db(c,lid,reason):
    r=c.execute('SELECT * FROM listings WHERE id=?',(lid,)).fetchone()
    if not r:raise HTTPException(404,'Объявление не найдено')
    d=json.loads(r['payload'])
    if r['status']!='banned':d['_status_before_ban']=r['status']
    if d.get('document_status')=='pending':d['document_status']='none'
    c.execute("UPDATE listings SET status='banned',reason=?,payload=?,private=NULL,private_expires=NULL WHERE id=?",(reason,dumps(d),lid))
    return r

@app.post('/api/admin/{lid}/ban')
async def ban_listing(lid:str,x:BanIn,u=Depends(admin_user)):
    reason=x.reason.strip()
    if len(reason)<3:raise HTTPException(400,'Укажите причину блокировки')
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=ban_in_db(c,lid,reason)
    audit(u['id'],lid,'ban')
    if r['channel_message']:enqueue('edit',{'id':lid},f'ban-edit:{lid}:{secrets.token_hex(4)}')
    return listing(getrow(lid),True)

@app.post('/api/admin/{lid}/unban')
async def unban_listing(lid:str,u=Depends(admin_user)):
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM listings WHERE id=?',(lid,)).fetchone()
        if not r:raise HTTPException(404,'Объявление не найдено')
        if r['status']!='banned':raise HTTPException(409,'Объявление не заблокировано')
        d=json.loads(r['payload']);status=d.pop('_status_before_ban','review')
        if status not in ('active','rented','review','rejected'):status='review'
        if d.get('_source_deleted'):status='source_deleted'
        elif d.get('_source_review'):status='review'
        c.execute("UPDATE listings SET status=?,reason='',payload=? WHERE id=?",(status,dumps(d),lid))
    audit(u['id'],lid,'unban')
    if r['channel_message']:enqueue('edit',{'id':lid},f'unban-edit:{lid}:{secrets.token_hex(4)}')
    elif publication_target(d)[0] and status=='active':enqueue('publish',{'id':lid},f'unban-publish:{lid}:{secrets.token_hex(4)}')
    return listing(getrow(lid),True)

@app.get('/api/admin/{lid}/private')
async def private_data(lid:str,u=Depends(admin_user)):
    r=getrow(lid);audit(u['id'],lid,'private_view')
    if r['private'] and r['private_expires']>time.time():return json.loads(CIPHER.decrypt(r['private']))
    return {'note':'Реквизиты не предоставлены или уже удалены','document_number':'','document_password':''}

def decision(lid,uid,what):
    if uid not in ADMINS:raise HTTPException(403,'Только для администраторов')
    r=getrow(lid);d=json.loads(r['payload']);status=r['status']
    if d.get('_source_key') and not SOURCE_STORE.get('activated',False):raise HTTPException(409,'Сначала переключите каталог в разделе Импорт')
    if d.get('_source_review') or d.get('_source_deleted'):raise HTTPException(409,'Откройте актуальную версию в разделе Импорт')
    if status=='banned':raise HTTPException(409,'Сначала снимите блокировку в форме администратора')
    if what=='document_checked':
        raise HTTPException(400,'Используйте отдельную форму проверки: нужны результат и критерии сверки')
    elif what=='approve':status='active'
    elif what=='reject':status='rejected';d['document_status']='none'
    else:raise HTTPException(400,'Неизвестное решение')
    wipe=what in ('document_checked','reject') or not (d.get('document_status')=='pending')
    with db() as c:
        c.execute('UPDATE listings SET payload=?,status=?,private=?,reason=? WHERE id=?',(dumps(d),status,None if wipe else r['private'],'' if what=='approve' else r['reason'],lid))
    audit(uid,lid,what)
    if status=='active' and r['status']!='active':active_effects(lid)
    elif r['channel_message']:enqueue('edit',{'id':lid},f'edit:{lid}:{secrets.token_hex(4)}')
    return {'ok':True}
@app.post('/api/admin/{lid}/decision')
async def make_decision(lid:str,x:DecisionIn,u=Depends(admin_user)):return decision(lid,u['id'],x.decision)
@app.get('/api/draft')
async def draft(u=Depends(user)):
    with db() as c:r=c.execute('SELECT * FROM drafts WHERE uid=?',(u['id'],)).fetchone()
    return {'text':r['text'],'photos':json.loads(r['photos'])} if r else {'text':'','photos':[]}

class DraftIn(BaseModel):
    text:str=Field(default='',max_length=12000)
    photos:list[str]=Field(default_factory=list,max_length=10)

@app.post('/api/draft')
async def save_draft(x:DraftIn,u=Depends(user)):
    rate(u['id'],'draft',100)
    photos=[]
    with db() as c:
        for pid in dict.fromkeys(x.photos):
            if not c.execute('SELECT 1 FROM photos WHERE id=? AND uid=?',(pid,u['id'])).fetchone():raise HTTPException(400,'Фото не принадлежит автору')
            photos.append(photo_details(pid))
        c.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?,?)',(u['id'],x.text,dumps(photos),time.time()))
    return {'ok':True}

class VerificationDecision(BaseModel):
    result:Literal['document_checked','owner_verified','representative_verified','not_confirmed']
    document_valid:bool=False
    object_matches:bool=False
    identity_matches:bool=False
    rights_current:bool=False
    authority_checked:bool=False
    checked_on:str=Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    note:str=Field(default='',max_length=500)

@app.post('/api/listings/{lid}/verification')
async def request_verification(lid:str,u=Depends(user)):
    if getrow(lid)['uid']!=u['id']:raise HTTPException(403,'Подтвердить может только автор объявления')
    raise HTTPException(409,'Проверка через сервис пока недоступна. Откройте официальный e-cadastre.')

@app.post('/api/admin/{lid}/verification')
async def verification_decision(lid:str,x:VerificationDecision,u=Depends(admin_user)):
    r=getrow(lid);d=json.loads(r['payload'])
    if d.get('document_status')!='pending' or not r['private'] or r['private_expires']<time.time():
        raise HTTPException(409,'Нет действующей заявки на проверку')
    today=datetime.now(timezone(timedelta(hours=4))).date()
    try:checked=datetime.strptime(x.checked_on,'%Y-%m-%d').date()
    except ValueError:raise HTTPException(400,'Неверная дата проверки')
    if checked>today:raise HTTPException(400,'Дата проверки не может быть в будущем')
    if x.result!='not_confirmed' and not (x.document_valid and x.object_matches):
        raise HTTPException(400,'Нужно проверить действительность документа и совпадение объекта на официальном сайте')
    if x.result in ('owner_verified','representative_verified') and not (x.identity_matches and x.rights_current):
        raise HTTPException(400,'Для этой отметки дополнительно сверьте личность и текущие права')
    if x.result=='representative_verified' and not x.authority_checked:
        raise HTTPException(400,'Проверьте полномочия представителя')
    # Public result never contains names, passwords, document numbers, or the private review note.
    d.update(document_status=x.result,document_checked_at=stamp(time.time()),
             verification={'method':'e-cadastre-manual','checked_on':x.checked_on,
                           'document_valid':x.document_valid,'object_matches':x.object_matches,
                           'identity_matches':x.identity_matches,'rights_current':x.rights_current,
                           'authority_checked':x.authority_checked})
    with db() as c:c.execute('UPDATE listings SET payload=?,private=NULL,private_expires=NULL WHERE id=?',(dumps(d),lid))
    # No secret values in audit logs. A boolean checklist records exactly what was checked.
    audit(u['id'],lid,'verification:'+dumps(x.model_dump(exclude={'note'})))
    if r['channel_message']:enqueue('edit',{'id':lid},'verified:'+lid+':'+secrets.token_hex(4))
    enqueue('verification_result',{'id':lid},'verification_result:'+lid+':'+secrets.token_hex(4))
    return {'ok':True,'document_status':x.result}

class TelegramError(Exception):
    def __init__(self,code,retry=0):self.code=code;self.retry=retry;super().__init__(f'Telegram error {code}')
async def tg(method,payload):
    async with httpx.AsyncClient(timeout=40) as client:
        res=await client.post(f'https://api.telegram.org/bot{TOKEN}/{method}',json=payload)
        data=res.json()
    if not data.get('ok'):raise TelegramError(data.get('error_code',res.status_code),data.get('parameters',{}).get('retry_after',0))
    return data.get('result')
def link(start=''):return f'https://t.me/{BOT}?start='+start

def app_button(text,start=''):
    return {'text':text,'web_app':{'url':PUBLIC_URL+('?start='+start if start else '')}}

def commission_text(l):
    amount=l.get('commission')
    if amount==0:return 'Без комиссии'
    if amount is None:return 'Комиссия не указана'
    upper=l.get('commission_max');value=f"{amount:,}"+(f"–{upper:,}" if upper is not None and upper>amount else '')
    if l.get('commission_type')=='fixed':fee=(value+' '+l.get('commission_currency','AMD')).replace(',',' ')
    else:fee=f"{value}% от аренды за {'сутки' if l.get('commission_basis')=='day' else 'месяц'}"
    return 'Комиссия агенту: '+fee+' · разово'

def public_text(l):
    title='Комната' if l['kind']=='room' else ('Дом' if l['kind']=='house' else 'Квартира')
    if l.get('rooms') is not None and l['kind']!='room':title+=f" · {l['rooms']} комн."
    prices=' / '.join((f"{p['amount']:,}"+(f"–{p['amount_max']:,}" if p.get('amount_max') else '')+f" {p['currency']} за {'месяц' if p['period']=='month' else 'сутки'}"+(f" ({p['condition']})" if p.get('condition') else '')).replace(',',' ') for p in l['prices'])
    lines=[title,prices,l['city']+', '+l['address'],commission_text(l)]
    if l.get('area'):lines.append(f"{l['area']:g} м²")
    for field,label in [('available','Сдаётся с'),('available_until','Сдаётся до')]:
        if l.get(field):lines.append(label+' '+datetime.strptime(l[field],'%Y-%m-%d').strftime('%d.%m.%Y'))
    role={'owner':'Собственник — со слов автора','tenant':'Съезжающий жилец','agent':'Представитель / агент'}.get(l['role'])
    if role:lines.append(role)
    if l.get('role')=='agent' and l.get('agent_affiliation'):lines.append(l['agent_affiliation'])
    badge={'document_checked':'Документ и объект сверены через e-cadastre','owner_verified':'Собственник сверён по e-cadastre и личности','representative_verified':'Представитель: документ и полномочия сверены'}.get(l.get('document_status'))
    if badge:lines.append(badge+' · '+str(l.get('document_checked_at',''))[:10])
    if l.get('contract')=='yes':lines.append('Письменный договор: автор согласен')
    if l.get('residence_registration') in ('yes','ask'):lines.append('Регистрация проживания: '+('автор согласен' if l['residence_registration']=='yes' else 'по договорённости'))
    if l.get('phone'):lines.append('Телефон: '+l['phone'])
    if l.get('contact'):lines.append('Telegram: '+l['contact'])
    if l.get('created_at'):
        dt=datetime.fromisoformat(l['created_at']).astimezone(timezone(timedelta(hours=4)))
        lines.append('Опубликовано '+dt.strftime('%d.%m.%Y %H:%M')+' (Ереван)')
    if l['status']!='active':lines.insert(0,{'rented':'СДАНО','rejected':'СНЯТО','banned':'СНЯТО МОДЕРАТОРОМ'}.get(l['status'],l['status']))
    return '\n'.join(lines)
def contact_url(l):
    contact=l.get('contact','')
    return 'https://t.me/'+contact[1:] if re.fullmatch(r'@[A-Za-z0-9_]{5,32}',contact) else link('l_'+l['id'])
def keyboard(l):
    return {'inline_keyboard':[[{'text':'Фото и описание','url':link('l_'+l['id'])},{'text':'Связаться','url':contact_url(l)}]]} if l['status']=='active' else {'inline_keyboard':[]}
def publication_target(l):
    return (PAID_CHAT,PAID_THREAD) if (l.get('commission') or 0)>0 else (CHAT,THREAD)

async def send_listing(l):
    chat,thread=publication_target(l)
    kw={'chat_id':chat}
    if thread:kw['message_thread_id']=thread
    if l.get('photos'):return await tg('sendPhoto',{**kw,'photo':photo_sizes(l['photos'][0]['id'])['full']['file_id'],'caption':public_text(l)[:1000],'reply_markup':keyboard(l)}),'photo'
    return await tg('sendMessage',{**kw,'text':public_text(l),'reply_markup':keyboard(l)}),'text'

async def process_job(j):
    kind=j['kind'];p=json.loads(j['payload']);uid=p.get('uid');lid=p.get('id')
    if lid:
        r=getrow(lid);l=listing(r)
    if kind in ('publish','edit') and json.loads(r['payload']).get('_source_post'):return
    if kind=='publish':
        if not publication_target(l)[0] or r['channel_message'] or l['status']!='active':return
        sent,mode=await send_listing(l)
        with db() as c:
            # Re-read after the network call so a concurrent moderation change is retained.
            payload=json.loads(c.execute('SELECT payload FROM listings WHERE id=?',(lid,)).fetchone()['payload'])
            payload['_telegram_post']={k:sent[k] for k in ('chat','message_id','message_thread_id') if k in sent}
            c.execute('UPDATE listings SET channel_message=?,channel_kind=?,payload=? WHERE id=?',(sent['message_id'],mode,dumps(payload),lid))
    elif kind=='edit':
        chat=json.loads(r['payload']).get('_telegram_post',{}).get('chat',{}).get('id') or publication_target(l)[0]
        if not chat or not r['channel_message']:return
        kw={'chat_id':chat,'message_id':r['channel_message']}
        if r['channel_kind']=='photo':await tg('editMessageCaption',{**kw,'caption':public_text(l)[:1000],'reply_markup':keyboard(l)})
        else:await tg('editMessageText',{**kw,'text':public_text(l),'reply_markup':keyboard(l)})
    elif kind=='report':
        report=report_row(p['report_id'],next(iter(ADMINS),0))
        if report['status']!='pending':return
        d=report_data(report);person=d['reporter']
        who=(person['name']+' · '+('@'+person['username']+' · ' if person['username'] else '')+'ID '+str(report['uid'])).strip()
        text='Жалоба на объявление\n'+l['address']+'\nОт: '+who+'\nПричина: '+d['reason']+'\n'+d['details'][:1400]
        text+='\nДоказательства: '+('ссылки / пояснение; ' if d['evidence'] else '')+str(len(d['photos']))+' фото. Полностью — в жалобе.'
        for admin in ADMINS:
            await tg('sendMessage',{'chat_id':admin,'text':text,'link_preview_options':{'is_disabled':True},'reply_markup':{'inline_keyboard':[[app_button('Разобрать жалобу','report_'+report['id'])]]}})
            await asyncio.sleep(1.05)
    elif kind=='admin':
        for admin in ADMINS:
            label='Нужно решение: '+(r['reason'] or 'проверка документа')
            await tg('sendMessage',{'chat_id':admin,'text':label+'\n'+l['address'],'reply_markup':{'inline_keyboard':[[app_button('Открыть задачу','review_'+lid)]]}})
            await asyncio.sleep(1.05)
    elif kind=='match':
        with db() as c:
            if c.execute('SELECT 1 FROM deliveries WHERE uid=? AND lid=?',(uid,lid)).fetchone():return
            subs=c.execute("SELECT * FROM subscriptions WHERE uid=? AND active=1 AND frequency='instant'",(uid,)).fetchall()
        eligible=[s for s in subs if matches(l,json.loads(s['filters']))]
        if not eligible:return
        await tg('sendMessage',{'chat_id':uid,'text':'Новое совпадение · '+eligible[0]['name']+'\n\n'+public_text(l),'reply_markup':keyboard(l)})
        with db() as c:c.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?)',(uid,lid))
    elif kind=='digest':
        with db() as c:
            ss=c.execute("SELECT * FROM subscriptions WHERE uid=? AND active=1 AND frequency='daily'",(uid,)).fetchall()
            delivered={x['lid'] for x in c.execute('SELECT lid FROM deliveries WHERE uid=?',(uid,))}
            rs=c.execute("SELECT * FROM listings WHERE status='active' AND json_extract(payload,'$._source_post') IS NULL ORDER BY created DESC").fetchall()
        ls=[listing(x) for x in rs if x['id'] not in delivered and any(x['created']>=s['created'] and matches(listing(x),json.loads(s['filters'])) for s in ss)]
        selected=[];txt='Новые варианты по вашим фильтрам'
        for x in ls[:8]:
            candidate=txt+'\n\n'+public_text(x)+'\n'+link('l_'+x['id'])
            if len(candidate.encode('utf-16-le'))//2>4000:break
            selected.append(x);txt=candidate
        if not selected:return
        await tg('sendMessage',{'chat_id':uid,'text':txt,'link_preview_options':{'is_disabled':True}})
        with db() as c:
            for x in selected:c.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?)',(uid,x['id']))
    elif kind=='verification_result':
        await tg('sendMessage',{'chat_id':r['uid'],'text':'Проверка объявления завершена: '+l['address']+'. Результат и дата — в карточке.','reply_markup':{'inline_keyboard':[[app_button('Открыть карточку','l_'+lid)]]}})
    elif kind=='reminder':
        return  # Obsolete v3 jobs must never send a renewal prompt.

async def maintenance():
    now=time.time();today=datetime.now(timezone(timedelta(hours=4)))
    with db() as c:
        c.execute('UPDATE listings SET private=NULL WHERE private_expires<?',(now,))
        for r in c.execute('SELECT id,payload FROM listings WHERE private IS NULL'):
            d=json.loads(r['payload'])
            if d.get('document_status')=='pending':d['document_status']='none';c.execute('UPDATE listings SET payload=? WHERE id=?',(dumps(d),r['id']))
        daily=c.execute("SELECT DISTINCT uid FROM subscriptions WHERE active=1 AND frequency='daily'").fetchall()
        c.execute('DELETE FROM drafts WHERE updated<?',(now-7*86400,))
    if today.hour>=19:
        for r in daily:enqueue('digest',{'uid':r['uid']},f"digest:{r['uid']}:{today.date()}")

async def worker():
    last=0
    while True:
        try:
            if time.time()-last>60:await maintenance();last=time.time()
            with db() as c:j=c.execute("SELECT * FROM jobs WHERE status='pending' AND run_at<=? ORDER BY id LIMIT 1",(time.time(),)).fetchone()
            if not j:await asyncio.sleep(1);continue
            with db() as c:c.execute("UPDATE jobs SET status='sending' WHERE id=?",(j['id'],))
            try:await process_job(j);status='done'
            except TelegramError as e:
                if e.retry:
                    with db() as c:c.execute("UPDATE jobs SET status='pending',run_at=?,attempts=attempts+1 WHERE id=?",(time.time()+e.retry+1,j['id']))
                    continue
                status='failed';log.warning('Job %s rejected: code %s',j['id'],e.code)
            except (httpx.TimeoutException,httpx.NetworkError):
                # Retrying an uncertain send may duplicate the Telegram post.
                status='uncertain';log.warning('Job %s: uncertain delivery; inspect before retry',j['id'])
            except Exception as e:status='failed';log.warning('Job %s failed (%s)',j['id'],type(e).__name__)
            with db() as c:c.execute('UPDATE jobs SET status=? WHERE id=?',(status,j['id']))
            await asyncio.sleep(1.1)
        except asyncio.CancelledError:raise
        except Exception as e:log.warning('Worker issue (%s)',type(e).__name__);await asyncio.sleep(5)

async def receive_report_photo(m,uid,binding):
    rid=binding['rid'];r=report_row(rid,uid)
    if r['status']!='draft' or time.time()-binding['updated']>3600:
        await tg('sendMessage',{'chat_id':uid,'text':'Приём доказательств закрыт. Откройте жалобу в приложении. Для фото жилья используйте /new.'});return
    if not m.get('photo'):
        await tg('sendMessage',{'chat_id':uid,'text':'Пришлите скриншоты как фото. Пояснения и ссылки добавьте в форме жалобы. /cancel — закончить приём.'});return
    rate(uid,'report-photo',50)
    sizes=[p for p in m['photo'] if p.get('file_id') and p.get('file_unique_id') and p.get('width',0)>0 and p.get('height',0)>0 and p.get('file_size',0)<=20_000_000]
    if not sizes:raise HTTPException(400,'Telegram не предоставил фото')
    photo=max(sizes,key=lambda p:p['width']*p['height'])
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM reports WHERE id=?',(rid,)).fetchone()
        if r['status']!='draft':return
        d=report_data(r)
        if len(d['photos'])>=5:
            full=True
        else:
            full=False
            if not any(p['unique_id']==photo['file_unique_id'] for p in d['photos']):
                d['photos'].append({'id':secrets.token_hex(16),'file_id':photo['file_id'],'unique_id':photo['file_unique_id']})
                c.execute('UPDATE reports SET payload=? WHERE id=?',(CIPHER.encrypt(dumps(d).encode()),rid))
    if full:await tg('sendMessage',{'chat_id':uid,'text':'В жалобе уже 5 фото. Удалить лишние можно в форме.'})

async def receive(update):
    cb=update.get('callback_query')
    if cb:
        uid=cb['from']['id'];parts=cb.get('data','').split(':',1)
        try:
            if len(parts)!=2:raise HTTPException(400,'Неизвестная кнопка')
            a,lid=parts;r=getrow(lid)
            if a in ('approve','reject') and cb.get('message',{}).get('text','').startswith('Жалоба на объявление'):
                raise HTTPException(409,'Откройте Админ → Жалобы, чтобы разобрать жалобу и указать причину решения')
            if a in ('approve','reject'):decision(lid,uid,a)
            elif a=='rented':status_change(lid,uid,'rented')
            elif a=='still':
                if uid!=r['uid'] and uid not in ADMINS:raise HTTPException(403,'Это не ваше объявление')
                await tg('answerCallbackQuery',{'callback_query_id':cb['id'],'text':'Статус объявления меняется вручную в разделе «Мои».'});return
            else:raise HTTPException(400,'Неизвестная кнопка')
            await tg('answerCallbackQuery',{'callback_query_id':cb['id'],'text':{'rented':'Объявление снято','approve':'Одобрено','reject':'Снято с публикации'}.get(a,'Готово')})
        except HTTPException as e:await tg('answerCallbackQuery',{'callback_query_id':cb['id'],'text':str(e.detail),'show_alert':True})
        return
    m=update.get('message',{})
    if m.get('chat',{}).get('type')!='private':return
    u=m.get('from',{});uid=u.get('id')
    if not uid:return
    with db() as c:c.execute('INSERT INTO users(uid,username,name,started) VALUES(?,?,?,1) ON CONFLICT(uid) DO UPDATE SET username=excluded.username,name=excluded.name,started=1',(uid,u.get('username',''),u.get('first_name','')))
    text=m.get('text','')
    if text.startswith('/stop'):
        with db() as c:c.execute('UPDATE subscriptions SET active=0 WHERE uid=?',(uid,))
        await tg('sendMessage',{'chat_id':uid,'text':'Все поисковые уведомления приостановлены.'});return
    if text.startswith('/myid'):
        await tg('sendMessage',{'chat_id':uid,'text':str(uid)});return
    if text.startswith('/start proof_'):
        rid=text.removeprefix('/start proof_')
        try:r=report_row(rid,uid)
        except HTTPException:
            await tg('sendMessage',{'chat_id':uid,'text':'Жалоба не найдена.'});return
        if r['uid']!=uid or r['status']!='draft':
            await tg('sendMessage',{'chat_id':uid,'text':'Приём доказательств для этой жалобы закрыт.'});return
        with db() as c:c.execute('INSERT OR REPLACE INTO report_uploads VALUES(?,?,?)',(uid,rid,time.time()))
        await tg('sendMessage',{'chat_id':uid,'text':'Пришлите до 5 скриншотов как фото, затем вернитесь в форму и отправьте жалобу. Доказательства доступны вам и администраторам. /cancel — закончить приём.','reply_markup':{'inline_keyboard':[[app_button('Вернуться к жалобе','report_'+rid)]]}});return
    if text.startswith(('/start','/new','/cancel')):
        with db() as c:c.execute('DELETE FROM report_uploads WHERE uid=?',(uid,))
    if text=='/cancel':
        await tg('sendMessage',{'chat_id':uid,'text':'Приём фото закончен. Загруженные доказательства сохранены в жалобе.'});return
    start=text.removeprefix('/start ').strip()
    if text.startswith('/start ') and re.fullmatch(r'(?:l_[A-Za-z0-9_-]{1,50}|report_[a-f0-9]{16}|review_[A-Za-z0-9_-]{1,50}|admin)',start):
        if start=='admin' or start.startswith('review_'):
            if uid not in ADMINS:return
        await tg('sendMessage',{'chat_id':uid,'text':'Откройте в приложении:','reply_markup':{'inline_keyboard':[[app_button('Открыть',start)]]}});return
    if text=='/start photos':
        await tg('sendMessage',{'chat_id':uid,'text':'Пришлите до 10 фотографий альбомом, затем вернитесь к объявлению.','reply_markup':{'inline_keyboard':[[{'text':'Вернуться к объявлению','web_app':{'url':PUBLIC_URL+'?start=draft'}}]]}});return
    if text.startswith('/start') or text.startswith('/help'):
        await tg('sendMessage',{'chat_id':uid,'text':'Найдите жильё без шума или пришлите сюда текст и фото своего объявления. Дальше укажите жильё, адрес и цену в приложении.','reply_markup':{'inline_keyboard':[[{'text':'Открыть приложение','web_app':{'url':PUBLIC_URL}}],[{'text':'Сдать жильё','web_app':{'url':PUBLIC_URL+'?start=add'}}]]}})
        return
    if text.startswith('/new'):
        with db() as c:c.execute('DELETE FROM drafts WHERE uid=?',(uid,))
        await tg('sendMessage',{'chat_id':uid,'text':'Черновик очищен. Пришлите текст и фото нового объявления.'});return
    with db() as c:binding=c.execute('SELECT * FROM report_uploads WHERE uid=?',(uid,)).fetchone()
    if binding:return await receive_report_photo(m,uid,binding)
    if not (m.get('photo') or text or m.get('caption')):return
    rate(uid,'draft',100)
    with db() as c:old=c.execute('SELECT * FROM drafts WHERE uid=?',(uid,)).fetchone()
    fresh=not old
    photos=[] if fresh else json.loads(old['photos']);raw='' if fresh else old['text']
    caption=text or m.get('caption','')
    if caption and caption not in raw:raw=(raw+'\n'+caption).strip()[:12000]
    if m.get('photo') and len(photos)<10:
        photos.append(store_telegram_photo(m['photo'],uid))
    with db() as c:c.execute('INSERT OR REPLACE INTO drafts VALUES(?,?,?,?)',(uid,raw,dumps(photos),time.time()))
    if fresh:
        await tg('sendMessage',{'chat_id':uid,'text':'Собираю черновик. Добавьте остальные фото, затем откройте карточку. Для следующего объявления: /new','reply_markup':{'inline_keyboard':[[{'text':'Проверить карточку','web_app':{'url':PUBLIC_URL+'?start=draft'}}]]}})

async def poll():
    with db() as c:r=c.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
    offset=int(r['value']) if r else 0
    try:await tg('setChatMenuButton',{'menu_button':{'type':'web_app','text':'Найти жильё','web_app':{'url':PUBLIC_URL}}})
    except Exception as e:log.warning('Menu setup failed (%s)',type(e).__name__)
    while True:
        try:
            updates=await tg('getUpdates',{'offset':offset,'timeout':25,'allowed_updates':['message','callback_query']})
            for update in updates:
                try:await receive(update)
                except Exception as e:log.warning('Update %s failed (%s)',update['update_id'],type(e).__name__)
                offset=update['update_id']+1
                with db() as c:c.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)",(str(offset),))
        except asyncio.CancelledError:raise
        except Exception as e:log.warning('Polling unavailable (%s)',type(e).__name__);await asyncio.sleep(5)

if __name__=='__main__':
    import uvicorn
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    uvicorn.run(app,host=os.getenv('HOST','127.0.0.1'),port=int(os.getenv('PORT','8000')),access_log=False)
