"""Telegram Mini App. One process; network writes require LIVE=1."""
from __future__ import annotations
import asyncio, contextlib, hashlib, hmac, json, logging, os, re, secrets, sqlite3, time
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl
import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from pydantic import BaseModel, Field, ConfigDict

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
        CREATE TABLE IF NOT EXISTS agent_profiles(uid INTEGER PRIMARY KEY, encrypted BLOB NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, username TEXT, name TEXT, started INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS listings(id TEXT PRIMARY KEY, uid INTEGER, payload TEXT, status TEXT, created REAL, confirmed REAL, fingerprint TEXT, private BLOB, private_expires REAL, reason TEXT, channel_message INTEGER, channel_kind TEXT, reminded REAL DEFAULT 0, UNIQUE(uid,fingerprint));
        CREATE INDEX IF NOT EXISTS listings_author ON listings(uid,status,created);
        CREATE TABLE IF NOT EXISTS photos(id TEXT PRIMARY KEY, uid INTEGER, sha TEXT, tg_file_id TEXT, created REAL, sizes TEXT);
        CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,uid INTEGER,name TEXT,filters TEXT,frequency TEXT,active INTEGER,created REAL,last_digest TEXT);
        CREATE TABLE IF NOT EXISTS deliveries(uid INTEGER,lid TEXT,PRIMARY KEY(uid,lid));
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,jobkey TEXT UNIQUE,status TEXT DEFAULT 'pending',run_at REAL,attempts INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER,lid TEXT,action TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS drafts(uid INTEGER PRIMARY KEY,text TEXT,photos TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        ''')
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
    if f.get('pets') and l.get('pets') not in ('yes','ask'):return False
    if f.get('contract') and l.get('contract')!='yes':return False
    if f.get('residence_registration') and l.get('residence_registration') not in ('yes','ask'):return False
    if f.get('verified') and l.get('document_status') not in ('owner_verified','representative_verified'):return False
    offers=[p for p in l['prices'] if (not f.get('period') or p['period']==f['period']) and (not f.get('currency') or p['currency']==f['currency']) and (not f.get('residence_registration') or p.get('registration')!='no')]
    if not offers:return False
    if f.get('max') and offers[0]['amount']>int(f['max']):return False
    if f.get('q') and norm(f['q']) not in norm(' '.join(l.get(k,'') or '' for k in ['address','city','district','description'])):return False
    return True

def active_effects(lid):
    r=getrow(lid);l=listing(r)
    if json.loads(r['payload']).get('_source_post'):return
    enqueue('publish',{'id':lid},'publish:'+lid)
    with db() as c:subs=c.execute("SELECT * FROM subscriptions WHERE active=1 AND frequency='instant' AND created<=?",(time.time(),)).fetchall()
    for s in subs:
        if matches(l,json.loads(s['filters'])):enqueue('match',{'uid':s['uid'],'id':lid},f"match:{s['uid']}:{lid}")

@asynccontextmanager
async def lifespan(app):
    setup()
    if LIVE and (not TOKEN or not re.fullmatch(r'[A-Za-z0-9_]{5,32}',BOT) or not PUBLIC_URL.startswith('https://') or not ADMINS):
        raise RuntimeError('For LIVE=1 set BOT_TOKEN, BOT_USERNAME, HTTPS PUBLIC_URL and ADMIN_IDS in .env.')
    tasks=[]
    if LIVE:tasks=[asyncio.create_task(poll()),asyncio.create_task(worker())]
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
    r.headers['X-Content-Type-Options']='nosniff';r.headers['Referrer-Policy']='no-referrer'
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
async def config():return {'live':LIVE,'bot_username':BOT if LIVE else '', 'version':'0.5.0','channel_configured':bool(LIVE and CHAT),'paid_channel_configured':bool(LIVE and PAID_CHAT)}

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
    if bool(s.private.document_number)!=bool(s.private.document_password):raise HTTPException(400,'Нужны и номер документа, и пароль — либо оставьте оба пустыми')
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
        d['document_status']='pending' if s.private.document_number else 'none'
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

async def source_photo_url(photo):
    source=photo.get('source_post','');message=photo.get('source_message','')
    if not re.fullmatch(r'https://t.me/[A-Za-z0-9_]{5,32}/[1-9][0-9]*',source):raise HTTPException(404)
    if public_photo_url(photo.get('url')) and 0<=time.time()-photo.get('resolved_at',0)<3000:return photo['url']
    key='source:'+source;cached=FILE_PATHS.get(key)
    if cached and cached[1]>time.monotonic():photos=cached[0]
    else:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                async with client.stream('GET',source+'?embed=1') as response:
                    response.raise_for_status();raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw)>1_000_000:raise ValueError()
            album=TelegramAlbum();album.feed(raw.decode('utf-8'))
            if source.removeprefix('https://t.me/') not in album.posts:raise ValueError()
            photos=album.photos
        except (httpx.HTTPError,ValueError):raise HTTPException(502,'Фото временно недоступно') from None
        if len(FILE_PATHS)>=512:FILE_PATHS.pop(next(iter(FILE_PATHS)))
        FILE_PATHS[key]=(photos,time.monotonic()+3000)
    if message not in photos:raise HTTPException(404,'Фото отсутствует в исходном посте')
    return photos[message]

@app.get('/media/{filename}')
async def media(filename:str):
    match=re.fullmatch(r'([a-f0-9]{32})(-thumb)?\.jpg',filename)
    if not match:raise HTTPException(404)
    photo=photo_sizes(match[1])['thumb' if match[2] else 'full']
    if photo.get('source_post'):
        return RedirectResponse(await source_photo_url(photo),status_code=302,headers={'Cache-Control':'public, max-age=300'})
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
    headers={'Cache-Control':'public, max-age=86400','X-Content-Type-Options':'nosniff'}
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
@app.post('/api/listings/{lid}/report')
async def report(lid:str,u=Depends(user)):
    rate(u['id'],'report',10);getrow(lid);audit(u['id'],lid,'report')
    enqueue('admin',{'id':lid,'report':True},f"report:{lid}:{int(time.time()/3600)}")
    return {'ok':True}
@app.get('/api/admin/queue')
async def queue(u=Depends(admin_user)):
    with db() as c:rs=c.execute("SELECT * FROM listings WHERE status='review' OR private IS NOT NULL ORDER BY created").fetchall()
    return [listing(r,True) for r in rs]
@app.get('/api/admin/listings')
async def admin_listings(status:Literal['all','banned']='all',u=Depends(admin_user)):
    with db() as c:
        rows=c.execute("SELECT * FROM listings WHERE (?='all' OR status=?) ORDER BY created DESC LIMIT 500",(status,status)).fetchall()
    return [listing(r,True) for r in rows]

@app.post('/api/admin/{lid}/ban')
async def ban_listing(lid:str,x:BanIn,u=Depends(admin_user)):
    reason=x.reason.strip()
    if len(reason)<3:raise HTTPException(400,'Укажите причину блокировки')
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM listings WHERE id=?',(lid,)).fetchone()
        if not r:raise HTTPException(404,'Объявление не найдено')
        d=json.loads(r['payload'])
        if r['status']!='banned':d['_status_before_ban']=r['status']
        if d.get('document_status')=='pending':d['document_status']='none'
        c.execute("UPDATE listings SET status='banned',reason=?,payload=?,private=NULL,private_expires=NULL WHERE id=?",(reason,dumps(d),lid))
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

class VerificationIn(BaseModel):
    document_number:str=Field(min_length=4,max_length=80)
    document_password:str=Field(min_length=4,max_length=100)
    applicant_name:str=Field(min_length=3,max_length=140)
    consent:bool

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
async def request_verification(lid:str,x:VerificationIn,u=Depends(user)):
    r=getrow(lid)
    if r['uid']!=u['id']:raise HTTPException(403,'Подтвердить может только автор объявления')
    if r['status'] not in ('active','review'):raise HTTPException(409,'Объявление не опубликовано')
    if not x.consent:raise HTTPException(400,'Нужно согласие на просмотр документа администратором')
    rate(u['id'],'verification',5)
    d=json.loads(r['payload'])
    payload=x.model_dump(exclude={'consent'})
    secret=CIPHER.encrypt(dumps(payload).encode())
    d['document_status']='pending'
    with db() as c:
        c.execute('UPDATE listings SET private=?,private_expires=?,payload=? WHERE id=?',
                  (secret,time.time()+7*86400,dumps(d),lid))
    enqueue('admin',{'id':lid},'verification:'+lid+':'+secrets.token_hex(4))
    audit(u['id'],lid,'verification_requested')
    return {'ok':True,'document_status':'pending'}

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
def link(start=''):return f'https://t.me/{BOT}?startapp='+start

def commission_text(l):
    amount=l.get('commission')
    if amount==0:return 'Без комиссии'
    if amount is None:return 'Комиссия не указана'
    if l.get('commission_type')=='fixed':fee=f"{amount:,} {l.get('commission_currency','AMD')}".replace(',',' ')
    else:fee=f"{amount}% от аренды за {'сутки' if l.get('commission_basis')=='day' else 'месяц'}"
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
    elif kind=='admin':
        for admin in ADMINS:
            label='Жалоба на объявление' if p.get('report') else 'Нужно решение: '+(r['reason'] or 'необязательная проверка документа')
            await tg('sendMessage',{'chat_id':admin,'text':label+'\n'+l['address']+'\nПодробности — в задаче.','reply_markup':{'inline_keyboard':[[{'text':'Открыть задачу','url':link('admin')}],[{'text':'Опубликовать','callback_data':'approve:'+lid},{'text':'Отклонить','callback_data':'reject:'+lid}]]}})
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
            rs=c.execute("SELECT * FROM listings WHERE status='active' ORDER BY created DESC").fetchall()
        ls=[listing(x) for x in rs if x['id'] not in delivered and any(x['created']>=s['created'] and matches(listing(x),json.loads(s['filters'])) for s in ss)]
        if not ls:return
        selected=ls[:8];txt='Новые варианты по вашим фильтрам\n\n'+'\n\n'.join(public_text(x)+'\n'+link('l_'+x['id']) for x in selected)
        await tg('sendMessage',{'chat_id':uid,'text':txt[:4000],'link_preview_options':{'is_disabled':True}})
        with db() as c:
            for x in selected:c.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?)',(uid,x['id']))
    elif kind=='verification_result':
        await tg('sendMessage',{'chat_id':r['uid'],'text':'Проверка объявления завершена: '+l['address']+'. Результат и дата — в карточке.','reply_markup':{'inline_keyboard':[[{'text':'Открыть карточку','url':link('l_'+lid)}]]}})
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

async def receive(update):
    cb=update.get('callback_query')
    if cb:
        uid=cb['from']['id'];parts=cb.get('data','').split(':',1)
        try:
            if len(parts)!=2:raise HTTPException(400,'Неизвестная кнопка')
            a,lid=parts;r=getrow(lid)
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
    if text=='/start photos':
        await tg('sendMessage',{'chat_id':uid,'text':'Пришлите до 10 фотографий альбомом, затем вернитесь к объявлению.','reply_markup':{'inline_keyboard':[[{'text':'Вернуться к объявлению','web_app':{'url':PUBLIC_URL+'?start=draft'}}]]}});return
    if text.startswith('/start') or text.startswith('/help'):
        await tg('sendMessage',{'chat_id':uid,'text':'Найдите жильё без шума или пришлите сюда текст и фото своего объявления. Дальше укажите жильё, адрес и цену в приложении.','reply_markup':{'inline_keyboard':[[{'text':'Открыть приложение','web_app':{'url':PUBLIC_URL}}],[{'text':'Сдать жильё','web_app':{'url':PUBLIC_URL+'?start=add'}}]]}})
        return
    if text.startswith('/new'):
        with db() as c:c.execute('DELETE FROM drafts WHERE uid=?',(uid,))
        await tg('sendMessage',{'chat_id':uid,'text':'Черновик очищен. Пришлите текст и фото нового объявления.'});return
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
