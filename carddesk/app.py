"""Small, same-origin card desk. No third-party session appears in logs or responses."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
from datetime import datetime

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).parent
DATA = Path(os.environ.get('CARDDESK_DATA', ROOT / 'data'))
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
os.umask(0o077)
KEYFILE = DATA / 'encryption.key'
try:
    with KEYFILE.open('xb') as key_out:
        key_out.write(Fernet.generate_key())
except FileExistsError:
    pass
KEY = KEYFILE.read_bytes()
CIPHER = Fernet(KEY)
ORIGIN = os.environ.get('CARDDESK_ORIGIN', 'http://127.0.0.1:8808').rstrip('/')
SECURE = ORIGIN.startswith('https://')
COOKIE = '__Host-carddesk' if SECURE else 'carddesk_local'
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

@contextmanager
def db():
    c = sqlite3.connect(DATA / 'carddesk.db', timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()

with db() as c:
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT UNIQUE, password TEXT);
    CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, role TEXT NOT NULL, expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, title TEXT NOT NULL, months INTEGER NOT NULL, price INTEGER NOT NULL CHECK(price>0), active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS cards(hash TEXT PRIMARY KEY, tail TEXT NOT NULL, product_id TEXT REFERENCES products(id), state TEXT NOT NULL DEFAULT 'ready', created INTEGER NOT NULL, order_id TEXT UNIQUE);
    CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, owner TEXT NOT NULL, lookup_hash TEXT NOT NULL, product_id TEXT REFERENCES products(id), title TEXT NOT NULL, price INTEGER NOT NULL, email TEXT NOT NULL, state TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL, payment_ref TEXT UNIQUE, credential BLOB, credential_expiry INTEGER, channel TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '', provider_ref TEXT UNIQUE);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, order_id TEXT REFERENCES orders(id), state TEXT NOT NULL, created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL, created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS rate_limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, window INTEGER NOT NULL);
    ''')
    for row in [('plus-1','ChatGPT Plus',1,14900),('plus-3','ChatGPT Plus',3,42900)]:
        c.execute('INSERT OR IGNORE INTO products(id,title,months,price) VALUES(?,?,?,?)',row)
    if not c.execute('SELECT 1 FROM admins').fetchone() and not (DATA/'setup-code').exists():
        (DATA/'setup-code').write_text(secrets.token_urlsafe(32))

def digest(s):
    return hmac.new(KEY, s.encode(), hashlib.sha256).hexdigest()

def cfg(c, key, default=''):
    r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return r['value'] if r else default

def savecfg(c,key,value):
    c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))

def encrypt(s):
    return CIPHER.encrypt(s.encode()).decode()

def decrypt(s):
    return CIPHER.decrypt(s.encode()).decode() if s else ''

def password_hash(p, salt=None):
    salt=salt or secrets.token_hex(16)
    return salt+':'+hashlib.scrypt(p.encode(),salt=salt.encode(),n=16384,r=8,p=1).hex()

def fail(msg,status=400):
    raise HTTPException(status,msg)

def limited(request,scope,limit=30):
    # Only the trusted loopback reverse proxy can supply the source address.
    addr=request.headers.get('x-real-ip') if request.client.host in ('127.0.0.1','::1') else request.client.host
    key=digest(scope+str(addr)); now=int(time.time()); window=now//60
    with db() as c:
        c.execute('DELETE FROM rate_limits WHERE window < ?',(window-2,))
        c.execute('INSERT INTO rate_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END,window=excluded.window',(key,window))
        count=c.execute('SELECT count FROM rate_limits WHERE key=?',(key,)).fetchone()[0]
    if count>limit: fail('操作过于频繁，请稍后重试',429)

def identity(request,admin=False):
    raw=request.cookies.get(COOKIE,'')
    with db() as c:
        row=c.execute('SELECT role FROM sessions WHERE token=? AND expires>?',(digest(raw),time.time())).fetchone()
    if not row or (admin and row['role']!='admin'): fail('请重新登录' if admin else '请刷新页面后重试',401)
    return digest(raw)

def session_response(role,payload):
    raw=secrets.token_urlsafe(32)
    with db() as c:
        c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
        c.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(raw),role,int(time.time())+(28800 if role=='admin' else 2592000)))
    out=JSONResponse(payload)
    out.set_cookie(COOKIE,raw,httponly=True,secure=SECURE,samesite='strict',path='/',max_age=28800 if role=='admin' else 2592000)
    return out

@app.middleware('http')
async def boundary(request,call_next):
    if request.method in ('POST','PUT','PATCH','DELETE'):
        if request.url.path!='/api/payment/callback' and request.headers.get('origin') != ORIGIN:
            return JSONResponse({'detail':'请求来源无效'},403)
        try:
            if int(request.headers.get('content-length','0'))>24576: return JSONResponse({'detail':'内容过长'},413)
        except ValueError:
            return JSONResponse({'detail':'请求无效'},400)
    response=await call_next(request)
    response.headers.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY','Permissions-Policy':'camera=(), microphone=(), geolocation=()','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"})
    response.headers['Cache-Control']='no-store' if request.url.path.startswith(('/api','/admin')) else 'public, max-age=300'
    return response

@app.exception_handler(RequestValidationError)
async def invalid_request(request,exc):
    return JSONResponse({'detail':'请检查输入内容'},422)

@app.exception_handler(Exception)
async def unhandled(request,exc):
    # Neither user input nor upstream response bodies are logged.
    return JSONResponse({'detail':'服务暂时不可用，请稍后重试'},500)

@app.get('/api/health')
def health():
    with db() as c: c.execute('SELECT 1').fetchone()
    return {'status':'ok'}

@app.get('/api/session')
def browser_session(request:Request):
    limited(request,'browser-session',60)
    try:
        identity(request)
        return {'ok':True}
    except HTTPException:
        return session_response('customer',{'ok':True})

@app.get('/api/catalog')
def catalog():
    with db() as c:
        return {'products':[dict(r) for r in c.execute('SELECT * FROM products WHERE active=1 ORDER BY months')], 'payment_enabled':bool(cfg(c,'payment_url')), 'fulfillment_enabled':bool(cfg(c,'provider_url') and cfg(c,'provider_key'))}

class SessionInput(BaseModel):
    session: str = Field(max_length=18000)

def parse_session(raw):
    try:
        s=json.loads(raw)
        token=s.get('accessToken','')
        email=s.get('user',{}).get('email','')
        expires=s.get('expires','')
        if not isinstance(token,str) or len(token)<20 or len(token)>16000: raise ValueError()
        if not isinstance(email,str) or not re.fullmatch(r'[^\s@]{1,100}@[^\s@]{1,150}',email): raise ValueError()
        expiry=int(datetime.fromisoformat(expires.replace('Z','+00:00')).timestamp())
        if expiry<=time.time()+60: fail('Session 已过期，请重新获取')
        return {'email':email,'expiry':expiry,'accessToken':token}
    except HTTPException: raise
    except (ValueError,TypeError,AttributeError,KeyError): fail('Session 格式无效，请粘贴完整内容')

# Parsing is deliberately local in the browser. Only an order with a configured
# fulfillment destination accepts the short-lived credential for delivery.
class OrderInput(BaseModel):
    product_id:str=Field(max_length=50)
    session:str=Field(max_length=18000)
    email:str=Field(max_length=254)
    idempotency_key:str=Field(min_length=20,max_length=100)
    card:str=Field(default='',max_length=100)
    consent:bool=False

def view_order(row):
    return {k:row[k] for k in ('id','title','price','email','state','created','updated','channel')}

def event(c,oid,state):
    c.execute('INSERT INTO events(order_id,state,created) VALUES(?,?,?)',(oid,state,int(time.time())))

def payment_link(template,oid,price):
    # Only order number and amount are appended; never credentials or personal data.
    u=urlparse(template)
    q=dict(parse_qsl(u.query));q.update(order_id=oid,amount=f'{price/100:.2f}')
    return urlunparse(u._replace(query=urlencode(q)))

@app.post('/api/orders')
def create_order(data:OrderInput,request:Request):
    limited(request,'order',10); owner=identity(request)
    if not data.consent: fail('请确认服务协议和隐私政策')
    oid='AI'+digest(owner+data.idempotency_key)[:22].upper()
    lookup=base64.urlsafe_b64encode(hmac.new(KEY,('lookup:'+oid).encode(),hashlib.sha256).digest()).decode()[:24]
    now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if old:
            return {'order':view_order(old),'lookup_key':lookup,'payment_url':payment_link(cfg(c,'payment_url'),oid,old['price']) if old['state']=='pending_payment' else ''}
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(data.product_id,)).fetchone()
        if not p: fail('商品暂不可用',409)
        if not cfg(c,'provider_url') or not cfg(c,'provider_key'): fail('当前通道暂不可用',409)
        if not data.card and not cfg(c,'payment_url'): fail('当前支付暂不可用',409)
        s=parse_session(data.session)
        if s['email'].lower()!=data.email.strip().lower(): fail('账号与 Session 不一致')
        if data.card:
            card=c.execute('SELECT * FROM cards WHERE hash=?',(digest(data.card.strip().upper()),)).fetchone()
            if not card or card['state']!='ready' or card['product_id']!=p['id']: fail('卡密无效、已使用或套餐不符',409)
        state='queued' if data.card else 'pending_payment'
        c.execute('INSERT INTO orders(id,owner,lookup_hash,product_id,title,price,email,state,created,updated,credential,credential_expiry,channel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,owner,digest(lookup),p['id'],f"{p['title']} · {p['months']} 个月",p['price'],s['email'],state,now,now,encrypt(s['accessToken']),min(s['expiry'],now+1800),'card' if data.card else 'payment'))
        if data.card: c.execute("UPDATE cards SET state='used',order_id=? WHERE hash=? AND state='ready'",(oid,digest(data.card.strip().upper())))
        event(c,oid,state)
        row=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        return {'order':view_order(row),'lookup_key':lookup,'payment_url':payment_link(cfg(c,'payment_url'),oid,p['price']) if not data.card else ''}

class CardInput(BaseModel):
    card:str=Field(min_length=6,max_length=100)

@app.post('/api/cards/check')
def check_card(data:CardInput,request:Request):
    limited(request,'card',10);identity(request)
    with db() as c:
        r=c.execute("SELECT p.* FROM cards c JOIN products p ON c.product_id=p.id WHERE c.hash=? AND c.state='ready' AND p.active=1",(digest(data.card.strip().upper()),)).fetchone()
        if not r: fail('卡密无效或已使用',404)
        return {'product':dict(r)}

class Lookup(BaseModel):
    order_id:str=Field(min_length=6,max_length=40)
    lookup_key:str=Field(min_length=10,max_length=100)

@app.post('/api/orders/lookup')
def lookup_order(data:Lookup,request:Request):
    limited(request,'lookup',15)
    with db() as c:
        r=c.execute('SELECT * FROM orders WHERE id=? AND lookup_hash=?',(data.order_id,digest(data.lookup_key))).fetchone()
        if not r: fail('订单号或查询码不正确',404)
        return {'order':view_order(r),'events':[dict(e) for e in c.execute('SELECT state,created FROM events WHERE order_id=? ORDER BY id',(r['id'],))]}

@app.get('/api/orders')
def own_orders(request:Request):
    owner=identity(request)
    with db() as c: return {'orders':[view_order(r) for r in c.execute('SELECT * FROM orders WHERE owner=? ORDER BY created DESC LIMIT 50',(owner,))]}

@app.post('/api/payment/callback')
async def callback(request:Request):
    raw=await request.body()
    if len(raw)>4096: fail('请求过长',413)
    with db() as c:
        key=decrypt(cfg(c,'payment_secret'))
        if not key: fail('Not available',403)
        ts=request.headers.get('x-timestamp','')
        try:
            if abs(time.time()-int(ts))>300: fail('Expired',403)
        except ValueError: fail('Invalid',403)
        sig=hmac.new(key.encode(),ts.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig,request.headers.get('x-signature','')): fail('Invalid',403)
        try:
            data=json.loads(raw)
            oid=data['order_id']; amount=data['amount_cents']; ref=data['transaction_id']
            if data.get('status')!='paid' or type(amount)!=int or not isinstance(ref,str) or not 3<=len(ref)<=100: raise ValueError()
        except (KeyError,ValueError,TypeError): fail('Invalid callback')
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not r or r['price']!=amount or r['channel']!='payment': fail('Order mismatch',409)
        if r['payment_ref']==ref: return {'ok':True}
        if r['state'] not in ('pending_payment','expired') or c.execute('SELECT 1 FROM orders WHERE payment_ref=?',(ref,)).fetchone(): fail('State conflict',409)
        state='queued' if r['credential'] and r['credential_expiry']>time.time() else 'needs_session'
        c.execute('UPDATE orders SET state=?,payment_ref=?,updated=? WHERE id=?',(state,ref,int(time.time()),oid));event(c,oid,state)
    return {'ok':True}

class Login(BaseModel):
    username:str=Field(min_length=3,max_length=60)
    password:str=Field(min_length=12,max_length=128)
    setup_code:str=Field(default='',max_length=100)

@app.get('/api/admin/state')
def admin_state(request:Request):
    with db() as c: setup=not bool(c.execute('SELECT 1 FROM admins').fetchone())
    try: identity(request,True); authenticated=True
    except HTTPException: authenticated=False
    return {'setup':setup,'authenticated':authenticated}

@app.post('/api/admin/login')
def login(data:Login,request:Request):
    limited(request,'login',5)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM admins WHERE id=1').fetchone()
        if not r:
            f=DATA/'setup-code'
            if not f.exists() or not hmac.compare_digest(f.read_text().strip(),data.setup_code): fail('初始化码不正确',403)
            c.execute('INSERT INTO admins VALUES(1,?,?)',(data.username,password_hash(data.password)))
        else:
            candidate=password_hash(data.password,r['password'].split(':')[0])
            if not hmac.compare_digest(candidate,r['password']) or data.username!=r['username']: fail('账号或密码不正确',401)
    (DATA/'setup-code').unlink(missing_ok=True)
    return session_response('admin',{'ok':True})

@app.post('/api/admin/logout')
def logout(request:Request):
    raw=request.cookies.get(COOKIE,'')
    with db() as c: c.execute('DELETE FROM sessions WHERE token=?',(digest(raw),))
    r=JSONResponse({'ok':True});r.delete_cookie(COOKIE,path='/',secure=SECURE,httponly=True,samesite='strict');return r

@app.get('/api/admin/dashboard')
def dashboard(request:Request):
    identity(request,True)
    with db() as c:
        return {'orders':[view_order(r) for r in c.execute('SELECT * FROM orders ORDER BY created DESC LIMIT 200')], 'products':[dict(r) for r in c.execute('SELECT * FROM products ORDER BY months')], 'cards':[dict(r) for r in c.execute('SELECT tail,product_id,state,created,order_id FROM cards ORDER BY created DESC LIMIT 200')], 'settings':{'payment_url':cfg(c,'payment_url'),'payment_secret_set':bool(cfg(c,'payment_secret')),'provider_url':cfg(c,'provider_url'),'provider_key_set':bool(cfg(c,'provider_key'))},'stats':{'orders':c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],'ready_cards':c.execute("SELECT COUNT(*) FROM cards WHERE state='ready'").fetchone()[0],'paid_cents':c.execute('SELECT COALESCE(SUM(price),0) FROM orders WHERE payment_ref IS NOT NULL').fetchone()[0]}}

class ProductInput(BaseModel):
    id:str=Field(pattern=r'^[a-z0-9-]{2,40}$')
    title:str=Field(min_length=2,max_length=60)
    months:int=Field(ge=1,le=12)
    price:int=Field(ge=1,le=10000000)
    active:bool=True

@app.post('/api/admin/products')
def product_edit(data:ProductInput,request:Request):
    identity(request,True)
    with db() as c:
        c.execute('INSERT INTO products VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,months=excluded.months,price=excluded.price,active=excluded.active',(data.id,data.title,data.months,data.price,data.active))
        c.execute('INSERT INTO audit(action,target,created) VALUES(?,?,?)',('product.update',data.id,int(time.time())))
    return {'ok':True}

class CardsInput(BaseModel):
    product_id:str=Field(max_length=50)
    count:int=Field(ge=1,le=100)

@app.post('/api/admin/cards')
def generate_cards(data:CardsInput,request:Request):
    identity(request,True)
    codes=['XY-'+secrets.token_hex(12).upper() for _ in range(data.count)]
    with db() as c:
        if not c.execute('SELECT 1 FROM products WHERE id=?',(data.product_id,)).fetchone(): fail('商品不存在')
        c.executemany('INSERT INTO cards(hash,tail,product_id,created) VALUES(?,?,?,?)',[(digest(x),x[-6:],data.product_id,int(time.time())) for x in codes])
        c.execute('INSERT INTO audit(action,target,created) VALUES(?,?,?)',('cards.generate',data.product_id+':'+str(data.count),int(time.time())))
    return {'codes':codes}

class SettingsInput(BaseModel):
    payment_url:str=Field(default='',max_length=2000)
    payment_secret:str=Field(default='',max_length=256)
    provider_url:str=Field(default='',max_length=2000)
    provider_key:str=Field(default='',max_length=256)

def validate_url(value):
    if not value:return
    u=urlparse(value)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.fragment: fail('请输入有效的 HTTPS 链接')
    # Provider network destinations must be public; host resolution is checked again at dispatch.
    if u.hostname in ('localhost','metadata.google.internal') or u.hostname.endswith(('.local','.internal')): fail('地址不可用')

@app.post('/api/admin/settings')
def update_settings(data:SettingsInput,request:Request):
    identity(request,True);validate_url(data.payment_url);validate_url(data.provider_url)
    with db() as c:
        savecfg(c,'payment_url',data.payment_url);savecfg(c,'provider_url',data.provider_url.rstrip('/'))
        if data.payment_secret: savecfg(c,'payment_secret',encrypt(data.payment_secret))
        if data.provider_key: savecfg(c,'provider_key',encrypt(data.provider_key))
        c.execute('INSERT INTO audit(action,target,created) VALUES(?,?,?)',('settings.update','payment/provider',int(time.time())))
    return {'ok':True}

class StatusInput(BaseModel):
    order_id:str=Field(max_length=40)
    state:str=Field(pattern=r'^(completed|failed|refunded)$')
    evidence:str=Field(min_length=4,max_length=200)

@app.post('/api/admin/order-status')
def order_status(data:StatusInput,request:Request):
    identity(request,True)
    allowed={'processing':{'completed','failed'},'queued':{'failed'},'needs_session':{'failed'},'failed':{'refunded'},'completed':{'refunded'}}
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM orders WHERE id=?',(data.order_id,)).fetchone()
        if not r or data.state not in allowed.get(r['state'],set()):fail('订单状态不允许此操作',409)
        c.execute('UPDATE orders SET state=?,evidence=?,credential=NULL,updated=? WHERE id=?',(data.state,data.evidence,int(time.time()),data.order_id));event(c,data.order_id,data.state)
        c.execute('INSERT INTO audit(action,target,created) VALUES(?,?,?)',('order.'+data.state,data.order_id,int(time.time())))
    return {'ok':True}

@app.head('/')
@app.get('/')
def index():return FileResponse(ROOT/'static/index.html')
@app.head('/admin')
@app.get('/admin')
def admin():return FileResponse(ROOT/'static/admin.html')
@app.head('/service')
@app.get('/service')
def service():return FileResponse(ROOT/'static/service.html')
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

class RefreshSession(Lookup):
    session:str=Field(max_length=18000)

@app.post('/api/orders/session')
def refresh_order_session(data:RefreshSession,request:Request):
    limited(request,'refresh-session',10)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM orders WHERE id=? AND lookup_hash=?',(data.order_id,digest(data.lookup_key))).fetchone()
        if not r or r['state']!='needs_session':fail('订单状态不允许更新',409)
        s=parse_session(data.session)
        if s['email'].lower()!=r['email'].lower():fail('账号与原订单不一致')
        if not cfg(c,'provider_url') or not cfg(c,'provider_key'):fail('当前通道暂不可用',409)
        c.execute("UPDATE orders SET state='queued',credential=?,credential_expiry=?,updated=? WHERE id=?",(encrypt(s['accessToken']),min(s['expiry'],int(time.time())+1800),int(time.time()),r['id']));event(c,r['id'],'queued')
    return {'ok':True}
