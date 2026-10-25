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
    CREATE TABLE IF NOT EXISTS staff(username TEXT PRIMARY KEY,password TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,owner TEXT NOT NULL,lookup_hash TEXT NOT NULL,source_hash TEXT NOT NULL,topic TEXT NOT NULL,subject TEXT NOT NULL,order_id TEXT REFERENCES orders(id),state TEXT NOT NULL DEFAULT 'open',assignee TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL,updated INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS ticket_messages(id INTEGER PRIMARY KEY,ticket_id TEXT NOT NULL REFERENCES tickets(id),author TEXT NOT NULL,body TEXT NOT NULL,created INTEGER NOT NULL,request_key TEXT UNIQUE);
    CREATE INDEX IF NOT EXISTS ix_tickets_owner ON tickets(owner,state);
    CREATE INDEX IF NOT EXISTS ix_tickets_source ON tickets(source_hash,created);
    CREATE INDEX IF NOT EXISTS ix_orders_owner ON orders(owner,state);
    CREATE TABLE IF NOT EXISTS rate_limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, window INTEGER NOT NULL);
    ''')
    # Additive migration: existing orders and issued cards retain their SKU IDs.
    c.execute('BEGIN IMMEDIATE')
    columns={r['name'] for r in c.execute('PRAGMA table_info(products)')}
    for name,definition in {
        'category':"TEXT NOT NULL DEFAULT 'chatgpt'",
        'tier':"TEXT NOT NULL DEFAULT 'Plus'",
        'sort_order':'INTEGER NOT NULL DEFAULT 0',
        'price_configured':'INTEGER NOT NULL DEFAULT 1',
    }.items():
        if name not in columns:c.execute(f'ALTER TABLE products ADD COLUMN {name} {definition}')
    if not c.execute("SELECT 1 FROM settings WHERE key='catalog_tiers_v2'").fetchone():
        c.execute("UPDATE products SET active=0 WHERE id='plus-3'")
        c.execute("UPDATE products SET category='chatgpt',tier='Plus',sort_order=10 WHERE id='plus-1'")
        products=[
            ('plus-1','ChatGPT Plus','chatgpt','Plus',10,14900,1),
            ('gpt-pro-5x','ChatGPT Pro 5x','chatgpt','Pro 5x',20,1,0),
            ('gpt-pro-10x','ChatGPT Pro 10x','chatgpt','Pro 10x',30,1,0),
            ('gpt-pro-20x-renew','ChatGPT Pro 20x · 续费','chatgpt','Pro 20x · 续费',40,1,0),
            ('gpt-pro-500','ChatGPT Pro 500','chatgpt','Pro 500',50,1,0),
            ('claude-pro','Claude Pro','claude','Pro',60,1,0),
            ('claude-max-5x','Claude Max 5x','claude','Max 5x',70,1,0),
            ('claude-max-20x','Claude Max 20x','claude','Max 20x',80,1,0),
            ('grok-super','SuperGrok','grok','SuperGrok',90,1,0),
            ('grok-plus','SuperGrok Plus','grok','SuperGrok Plus',100,1,0),
            ('grok-heavy','SuperGrok Heavy','grok','SuperGrok Heavy',110,1,0),
        ]
        for pid,title,category,tier,rank,price,configured in products:
            c.execute('INSERT OR IGNORE INTO products(id,title,months,price,category,tier,sort_order,price_configured) VALUES(?,?,1,?,?,?,?,?)',(pid,title,price,category,tier,rank,configured))
        c.execute("INSERT INTO settings(key,value) VALUES('catalog_tiers_v2','1')")
    # One-time reference pricing. Subsequent restarts preserve operator edits.
    for table, columns in {
        'products': {'price_source': "TEXT NOT NULL DEFAULT ''"},
        'orders': {'source_hash': "TEXT NOT NULL DEFAULT ''", 'review': "INTEGER NOT NULL DEFAULT 0"},
        'audit': {'actor': "TEXT NOT NULL DEFAULT 'system'"},
    }.items():
        existing={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
        for name,definition in columns.items():
            if name not in existing:c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    c.execute('CREATE INDEX IF NOT EXISTS ix_orders_source_created ON orders(source_hash,created)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_orders_state_created ON orders(state,created)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_ticket_messages_ticket ON ticket_messages(ticket_id,id)')
    if not c.execute("SELECT 1 FROM settings WHERE key='catalog_reference_v3'").fetchone():
        source='vipcz.vip / 2026-10-25'
        fx='官方美元月价 × 6.67；2026-10-23 USD/CNY；2026-10-25 核价'
        rows=[
          ('plus-1','ChatGPT Plus','chatgpt','Plus',10,11990,source),
          ('gpt-pro-5x','ChatGPT Pro 5x','chatgpt','Pro 5x',20,69000,source),
          ('gpt-pro-10x','ChatGPT Pro 10x','chatgpt','Pro 10x',30,109900,source),
          ('gpt-pro-20x-renew','ChatGPT Pro 10x · 续费','chatgpt','Pro 10x · 续费',40,109900,source),
          ('gpt-pro-500','ChatGPT Pro 25x · 智利','chatgpt','Pro 25x · $500 智利',50,324800,source),
          ('gpt-pro-500-ph','ChatGPT Pro 25x · 菲律宾','chatgpt','Pro 25x · $500 菲律宾',51,341900,source),
          ('gpt-go','ChatGPT Go','chatgpt','Go',52,4500,source),
          ('gpt-plus-ios','ChatGPT Plus · iOS','chatgpt','Plus · iOS',53,14500,source),
          ('gpt-pro-5x-ios','ChatGPT Pro 5x · iOS','chatgpt','Pro 5x · iOS',54,67000,source),
          ('claude-pro','Claude Pro','claude','Pro',60,14800,source),
          ('claude-max-5x','Claude Max 5x','claude','Max 5x',70,66700,'claude.com/pricing / $100；'+fx),
          ('claude-max-20x','Claude Max 20x','claude','Max 20x',80,133400,'claude.com/pricing / $200；'+fx),
          ('grok-super','Grok Premium','grok','Premium',90,20010,'x.ai/pricing / $30；'+fx),
          ('grok-plus','Grok Super','grok','Super',100,66700,'x.ai/pricing / $100；'+fx),
          ('grok-heavy','Grok Ultra','grok','Ultra',110,133400,'x.ai/pricing / $200；'+fx),
          ('codex-250','Codex 250 积分','codex','250 积分',120,8400,source),
          ('codex-500','Codex 500 积分','codex','500 积分',130,16200,source),
          ('codex-1000','Codex 1000 积分','codex','1000 积分',140,30500,source),
          ('codex-2000','Codex 2000 积分','codex','2000 积分',150,59500,source),
        ]
        for pid,title,category,tier,rank,price,src in rows:
            old=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            used=c.execute('SELECT 1 FROM orders WHERE product_id=? UNION ALL SELECT 1 FROM cards WHERE product_id=? LIMIT 1',(pid,pid)).fetchone()
            if old and used and old['tier']!=tier:
                # Never silently repurpose already-issued inventory.
                pid=pid+'-v3';old=None
            if not old:
                c.execute('INSERT INTO products(id,title,months,price,category,tier,sort_order,price_configured,price_source) VALUES(?,?,1,?,?,?,?,1,?)',(pid,title,price,category,tier,rank,src))
            else:
                c.execute('UPDATE products SET title=?,tier=?,price=?,price_configured=1,price_source=?,sort_order=? WHERE id=?',(title,tier,price,src,rank,pid))
        c.execute("INSERT INTO settings VALUES('catalog_reference_v3','1')")
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

def source_hash(request):
    addr=request.client.host
    if addr in ('127.0.0.1','::1'):addr=request.headers.get('x-real-ip') or addr
    return digest('source:'+addr)

def identity(request,admin=False):
    raw=request.cookies.get(COOKIE,'')
    with db() as c:
        row=c.execute('SELECT role FROM sessions WHERE token=? AND expires>?',(digest(raw),time.time())).fetchone()
    if not row or (admin and row['role']!='admin'): fail('请重新登录' if admin else '请刷新页面后重试',401)
    return digest(raw)

def staff_identity(request):
    owner=identity(request)
    with db() as c:
        role=c.execute('SELECT role FROM sessions WHERE token=?',(owner,)).fetchone()['role']
        if role=='admin':return 'admin'
        if role.startswith('staff:'):
            username=role[6:]
            if c.execute('SELECT 1 FROM staff WHERE username=? AND active=1',(username,)).fetchone():return username
    fail('请使用运营账号登录',401)

def session_response(role,payload):
    raw=secrets.token_urlsafe(32)
    with db() as c:
        c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
        c.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(raw),role,int(time.time())+(28800 if role!='customer' else 2592000)))
    out=JSONResponse(payload)
    out.set_cookie(COOKIE,raw,httponly=True,secure=SECURE,samesite='strict',path='/',max_age=28800 if role!='customer' else 2592000)
    return out

@app.middleware('http')
async def boundary(request,call_next):
    if request.method in ('POST','PUT','PATCH','DELETE'):
        if request.url.path!='/api/payment/callback' and request.headers.get('origin') != ORIGIN:
            return JSONResponse({'detail':'请求来源无效'},403)
        try:
            if int(request.headers.get('content-length','0'))>(131072 if request.url.path=='/api/admin/orders/results' else 24576): return JSONResponse({'detail':'内容过长'},413)
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

def view_product(row):
    p=dict(row)
    p['price']=p['price'] if p['price_configured'] else None
    p['account_supported']=p['category']=='chatgpt'
    return p

@app.get('/api/catalog')
def catalog():
    with db() as c:
        return {'products':[view_product(r) for r in c.execute('SELECT * FROM products WHERE active=1 ORDER BY sort_order,id')], 'payment_enabled':bool(cfg(c,'payment_url')), 'fulfillment_enabled':bool(cfg(c,'provider_url') and cfg(c,'provider_key'))}

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

def expire_unpaid(c,now=None):
    now=now or int(time.time())
    rows=c.execute("SELECT id FROM orders WHERE channel='payment' AND payment_ref IS NULL AND state='pending_payment' AND created<=?",(now-900,)).fetchall()
    for r in rows:
        c.execute("UPDATE orders SET state='expired',credential=NULL,credential_expiry=0,updated=? WHERE id=?",(now,r['id']))
        event(c,r['id'],'expired')
    return len(rows)

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
    now=int(time.time()); source=source_hash(request)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        expire_unpaid(c,now)
        old=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if old:
            if old['product_id']!=data.product_id or old['email'].lower()!=data.email.strip().lower() or old['channel']!=('card' if data.card else 'payment'):fail('重复请求与原订单不一致',409)
            return {'order':view_order(old),'lookup_key':lookup,'payment_url':payment_link(cfg(c,'payment_url'),oid,old['price']) if old['state']=='pending_payment' else ''}
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(data.product_id,)).fetchone()
        if not p: fail('商品暂不可用',409)
        if p['category']!='chatgpt':fail('该品类通道暂不可用',409)
        if not p['price_configured']:fail('商品暂不可用',409)
        if not cfg(c,'provider_url') or not cfg(c,'provider_key'): fail('当前通道暂不可用',409)
        if not data.card and not cfg(c,'payment_url'): fail('当前支付暂不可用',409)
        s=parse_session(data.session)
        if s['email'].lower()!=data.email.strip().lower(): fail('账号与 Session 不一致')
        if data.card:
            card=c.execute('SELECT * FROM cards WHERE hash=?',(digest(data.card.strip().upper()),)).fetchone()
            if not card or card['state']!='ready' or card['product_id']!=p['id']: fail('卡密无效、已使用或套餐不符',409)
        # No unverified email can lock an account or reserve inventory.
        if not data.card:
            pending=c.execute("SELECT COUNT(*) FROM orders WHERE owner=? AND state='pending_payment'",(owner,)).fetchone()[0]
            from_source=c.execute("SELECT COUNT(*) FROM orders WHERE source_hash=? AND state='pending_payment'",(source,)).fetchone()[0]
            hourly=c.execute('SELECT COUNT(*) FROM orders WHERE source_hash=? AND created>?',(source,now-3600)).fetchone()[0]
            if pending>=3 or from_source>=8 or hourly>=30:fail('待支付订单过多，请先处理已有订单或稍后重试',429)
        state='queued' if data.card else 'pending_payment'
        c.execute('INSERT INTO orders(id,owner,lookup_hash,product_id,title,price,email,state,created,updated,credential,credential_expiry,channel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,owner,digest(lookup),p['id'],p['title'],p['price'],s['email'],state,now,now,encrypt(s['accessToken']),min(s['expiry'],now+(1800 if data.card else 900)),'card' if data.card else 'payment'))
        c.execute('UPDATE orders SET source_hash=? WHERE id=?',(source,oid))
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
        return {'product':view_product(r)}

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
    try: staff_identity(request); authenticated=True
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
            if data.username!=r['username']:
                operator=c.execute('SELECT * FROM staff WHERE username=? AND active=1',(data.username,)).fetchone()
                candidate=password_hash(data.password,operator['password'].split(':')[0] if operator else '0'*32)
                if not operator or not hmac.compare_digest(candidate,operator['password']):fail('账号或密码不正确',401)
                role='staff:'+operator['username']
            else:role='admin'
            if role.startswith('staff:'):r=operator
            candidate=password_hash(data.password,r['password'].split(':')[0])
            if not hmac.compare_digest(candidate,r['password']) or data.username!=r['username']: fail('账号或密码不正确',401)
    (DATA/'setup-code').unlink(missing_ok=True)
    return session_response(locals().get('role','admin'),{'ok':True})

@app.post('/api/admin/logout')
def logout(request:Request):
    raw=request.cookies.get(COOKIE,'')
    with db() as c: c.execute('DELETE FROM sessions WHERE token=?',(digest(raw),))
    r=JSONResponse({'ok':True});r.delete_cookie(COOKIE,path='/',secure=SECURE,httponly=True,samesite='strict');return r

@app.get('/api/admin/dashboard')
def dashboard(request:Request):
    actor=staff_identity(request)
    with db() as c:
        orders=[{**view_order(r),'review':bool(r['review'])} for r in c.execute('SELECT * FROM orders ORDER BY created DESC LIMIT 200')]
        result={'role':'admin' if actor=='admin' else 'staff','actor':actor,'orders':orders,'tickets':[ticket_view(r) for r in c.execute('SELECT * FROM tickets ORDER BY updated DESC LIMIT 200')], 'stats':{'orders':c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],'ready_cards':c.execute("SELECT COUNT(*) FROM cards WHERE state='ready'").fetchone()[0],'paid_cents':c.execute('SELECT COALESCE(SUM(price),0) FROM orders WHERE payment_ref IS NOT NULL').fetchone()[0]},'products':[], 'cards':[], 'settings':{},'staff':[]}
        if actor=='admin':
            result.update(products=[view_product(r) for r in c.execute("SELECT * FROM products WHERE id!='plus-3' ORDER BY sort_order,id")],cards=[dict(r) for r in c.execute('SELECT tail,product_id,state,created,order_id FROM cards ORDER BY created DESC LIMIT 200')],settings={'payment_url':cfg(c,'payment_url'),'payment_secret_set':bool(cfg(c,'payment_secret')),'provider_url':cfg(c,'provider_url'),'provider_key_set':bool(cfg(c,'provider_key'))},staff=[dict(r) for r in c.execute('SELECT username,active,created FROM staff ORDER BY created')])
        return result

class ProductInput(BaseModel):
    id:str=Field(pattern=r'^[a-z0-9-]{2,40}$')
    title:str=Field(min_length=2,max_length=60)
    category:str=Field(pattern=r'^(chatgpt|claude|grok|codex)$')
    tier:str=Field(min_length=2,max_length=40)
    price:int | None=Field(default=None,ge=1,le=10000000)
    active:bool=True

@app.post('/api/admin/products')
def product_edit(data:ProductInput,request:Request):
    identity(request,True)
    with db() as c:
        if data.id=='plus-3':fail('此商品已停用',409)
        old=c.execute('SELECT category,tier FROM products WHERE id=?',(data.id,)).fetchone()
        if old and (old['category']!=data.category or old['tier']!=data.tier):
            # A SKU already referenced by cards/orders cannot be repurposed.
            if c.execute('SELECT 1 FROM orders WHERE product_id=? UNION ALL SELECT 1 FROM cards WHERE product_id=? LIMIT 1',(data.id,data.id)).fetchone():
                fail('已有订单或卡密的商品不能更换品类或档位',409)
        c.execute('INSERT INTO products(id,title,months,price,active,category,tier,price_configured) VALUES(?,?,1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,price=excluded.price,active=excluded.active,category=excluded.category,tier=excluded.tier,price_configured=excluded.price_configured',(data.id,data.title,data.price or 1,data.active,data.category,data.tier,data.price is not None))
        c.execute("UPDATE products SET price_source='后台设定' WHERE id=?",(data.id,))
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

# Support is a first-party mailbox. Staff never receive third-party credentials.
def support_text(value):
    value=value.strip()
    if re.search(r'(?i)("accessToken"\s*:|bearer\s+[a-z0-9._-]{16,}|sk-(?:proj-)?[a-z0-9_-]{16,}|eyJ[a-z0-9_-]{16,}\.[a-z0-9_-]+|-----BEGIN .*PRIVATE KEY)',value):
        fail('请删除密码、访问令牌或 Session 后再提交')
    if not value:fail('请填写问题内容')
    return value

def ticket_view(row):
    return {k:row[k] for k in ('id','topic','subject','order_id','state','assignee','created','updated')}

def ticket_key(tid):
    return base64.urlsafe_b64encode(hmac.new(KEY,('ticket:'+tid).encode(),hashlib.sha256).digest()).decode()[:24]

def ticket_access(c,tid,lookup,request):
    row=c.execute('SELECT * FROM tickets WHERE id=?',(tid,)).fetchone()
    owner=digest(request.cookies.get(COOKIE,''))
    valid=c.execute('SELECT 1 FROM sessions WHERE token=? AND expires>?',(owner,time.time())).fetchone()
    if not row or not ((valid and row['owner']==owner) or (lookup and hmac.compare_digest(row['lookup_hash'],digest(lookup)))):fail('工单号或查询码不正确',404)
    return row

class TicketCreate(BaseModel):
    topic:str=Field(pattern=r'^(购买咨询|充值未到账|账号核对|Codex 使用|售后退款)$')
    subject:str=Field(min_length=4,max_length=100)
    body:str=Field(min_length=10,max_length=2000)
    order_id:str=Field(default='',max_length=40)
    order_key:str=Field(default='',max_length=100)
    website:str=Field(default='',max_length=100)
    idempotency_key:str=Field(min_length=20,max_length=100)

@app.post('/api/tickets')
def ticket_create(data:TicketCreate,request:Request):
    limited(request,'ticket-create',5);owner=identity(request);source=source_hash(request);now=int(time.time())
    if data.website:fail('提交失败，请检查输入内容')
    subject=support_text(data.subject);body=support_text(data.body)
    if len(subject)<4 or len(body)<10:fail('请补充问题标题和详情')
    tid='TK'+digest(owner+data.idempotency_key)[:20].upper();key=ticket_key(tid)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM tickets WHERE id=?',(tid,)).fetchone()
        if old:return {'ticket':ticket_view(old),'lookup_key':key}
        if data.order_id:
            order=c.execute('SELECT * FROM orders WHERE id=?',(data.order_id,)).fetchone()
            if not order or not (order['owner']==owner or (data.order_key and hmac.compare_digest(order['lookup_hash'],digest(data.order_key)))):fail('订单号或查询码不正确',404)
        if c.execute("SELECT COUNT(*) FROM tickets WHERE owner=? AND state!='closed'",(owner,)).fetchone()[0]>=3:fail('请先在已有工单中补充问题',429)
        if c.execute('SELECT COUNT(*) FROM tickets WHERE source_hash=? AND created>?',(source,now-86400)).fetchone()[0]>=10:fail('今日提交次数较多，请在已有工单中继续沟通',429)
        c.execute('INSERT INTO tickets(id,owner,lookup_hash,source_hash,topic,subject,order_id,created,updated) VALUES(?,?,?,?,?,?,?,?,?)',(tid,owner,digest(key),source,data.topic,subject,data.order_id or None,now,now))
        c.execute('INSERT INTO ticket_messages(ticket_id,author,body,created) VALUES(?,?,?,?)',(tid,'customer',body,now))
        return {'ticket':ticket_view(c.execute('SELECT * FROM tickets WHERE id=?',(tid,)).fetchone()),'lookup_key':key}

@app.get('/api/tickets')
def tickets_own(request:Request):
    owner=identity(request)
    with db() as c:return {'tickets':[ticket_view(r) for r in c.execute('SELECT * FROM tickets WHERE owner=? ORDER BY updated DESC LIMIT 50',(owner,))]}

class TicketAccess(BaseModel):
    ticket_id:str=Field(min_length=6,max_length=40)
    lookup_key:str=Field(default='',max_length=100)

@app.post('/api/tickets/read')
def ticket_read(data:TicketAccess,request:Request):
    limited(request,'ticket-read',30)
    with db() as c:
        r=ticket_access(c,data.ticket_id,data.lookup_key,request)
        return {'ticket':ticket_view(r),'messages':[dict(m) for m in c.execute('SELECT author,body,created FROM ticket_messages WHERE ticket_id=? ORDER BY id',(r['id'],))]}

class TicketReply(TicketAccess):
    body:str=Field(min_length=2,max_length=2000)
    idempotency_key:str=Field(min_length=20,max_length=100)

@app.post('/api/tickets/reply')
def ticket_reply(data:TicketReply,request:Request):
    limited(request,'ticket-reply',10);body=support_text(data.body);now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=ticket_access(c,data.ticket_id,data.lookup_key,request)
        key=digest(r['id']+':customer:'+data.idempotency_key)
        if c.execute('SELECT 1 FROM ticket_messages WHERE request_key=?',(key,)).fetchone():return {'ok':True}
        if r['state']=='closed':fail('请先重新打开工单',409)
        if c.execute('SELECT COUNT(*) FROM ticket_messages WHERE ticket_id=?',(r['id'],)).fetchone()[0]>=200:fail('工单内容已达上限，请创建新工单',409)
        c.execute('INSERT INTO ticket_messages(ticket_id,author,body,created,request_key) VALUES(?,?,?,?,?)',(r['id'],'customer',body,now,key))
        c.execute("UPDATE tickets SET state='open',updated=? WHERE id=?",(now,r['id']))
    return {'ok':True}

class TicketState(TicketAccess):
    state:str=Field(pattern=r'^(open|closed)$')

@app.post('/api/tickets/state')
def ticket_state(data:TicketState,request:Request):
    limited(request,'ticket-state',10)
    with db() as c:
        c.execute('BEGIN IMMEDIATE');r=ticket_access(c,data.ticket_id,data.lookup_key,request)
        if data.state=='open' and r['state']=='closed' and c.execute("SELECT COUNT(*) FROM tickets WHERE owner=? AND state!='closed'",(r['owner'],)).fetchone()[0]>=3:fail('请先处理已有工单',429)
        c.execute('UPDATE tickets SET state=?,updated=? WHERE id=?',(data.state,int(time.time()),r['id']))
    return {'ok':True}

@app.post('/api/admin/tickets/read')
def staff_ticket_read(data:TicketAccess,request:Request):
    staff_identity(request)
    with db() as c:
        r=c.execute('SELECT * FROM tickets WHERE id=?',(data.ticket_id,)).fetchone()
        if not r:fail('工单不存在',404)
        return {'ticket':ticket_view(r),'messages':[dict(m) for m in c.execute('SELECT author,body,created FROM ticket_messages WHERE ticket_id=? ORDER BY id',(r['id'],))]}

@app.post('/api/admin/tickets/reply')
def staff_ticket_reply(data:TicketReply,request:Request):
    actor=staff_identity(request);body=support_text(data.body);now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM tickets WHERE id=?',(data.ticket_id,)).fetchone()
        if not r:fail('工单不存在',404)
        if r['assignee'] and r['assignee']!=actor and actor!='admin':fail('工单已由其他运营接手',409)
        key=digest(r['id']+':staff:'+data.idempotency_key)
        if c.execute('SELECT 1 FROM ticket_messages WHERE request_key=?',(key,)).fetchone():return {'ok':True}
        c.execute('INSERT INTO ticket_messages(ticket_id,author,body,created,request_key) VALUES(?,?,?,?,?)',(r['id'],'staff',body,now,key))
        c.execute("UPDATE tickets SET state='replied',assignee=?,updated=? WHERE id=?",(actor,now,r['id']))
        c.execute('INSERT INTO audit(action,target,created,actor) VALUES(?,?,?,?)',('ticket.reply',r['id'],now,actor))
    return {'ok':True}

class BulkInput(BaseModel):
    ids:list[str]=Field(min_length=1,max_length=100)
    action:str=Field(pattern=r'^(close_unpaid|hold|release|claim|close|reopen)$')

@app.post('/api/admin/orders/bulk')
def bulk_orders(data:BulkInput,request:Request):
    actor=staff_identity(request)
    if data.action not in ('close_unpaid','hold','release'):fail('操作无效')
    now=int(time.time());results=[]
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        for oid in dict.fromkeys(data.ids):
            r=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
            reason=''
            if not r:reason='订单不存在'
            elif data.action=='close_unpaid':
                if r['state']!='pending_payment' or r['payment_ref'] or r['channel']!='payment':reason='仅可关闭未付款订单'
                else:
                    c.execute("UPDATE orders SET state='expired',credential=NULL,credential_expiry=0,updated=? WHERE id=?",(now,oid));event(c,oid,'expired')
            elif r['state'] not in ('pending_payment','queued','needs_session'):reason='此状态不能暂停或恢复'
            else:c.execute('UPDATE orders SET review=?,updated=? WHERE id=?',(int(data.action=='hold'),now,oid))
            if not reason:c.execute('INSERT INTO audit(action,target,created,actor) VALUES(?,?,?,?)',('bulk.'+data.action,oid,now,actor))
            results.append({'id':oid,'ok':not reason,'reason':reason})
    return {'results':results}

@app.post('/api/admin/tickets/bulk')
def bulk_tickets(data:BulkInput,request:Request):
    actor=staff_identity(request)
    if data.action not in ('claim','close','reopen'):fail('操作无效')
    now=int(time.time());results=[]
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        for tid in dict.fromkeys(data.ids):
            r=c.execute('SELECT * FROM tickets WHERE id=?',(tid,)).fetchone();reason=''
            if not r:reason='工单不存在'
            elif r['assignee'] and r['assignee']!=actor and actor!='admin':reason='已由其他运营接手'
            elif data.action=='claim':c.execute('UPDATE tickets SET assignee=?,updated=? WHERE id=?',(actor,now,tid))
            else:c.execute('UPDATE tickets SET state=?,updated=? WHERE id=?',('closed' if data.action=='close' else 'open',now,tid))
            if not reason:c.execute('INSERT INTO audit(action,target,created,actor) VALUES(?,?,?,?)',('ticket.'+data.action,tid,now,actor))
            results.append({'id':tid,'ok':not reason,'reason':reason})
    return {'results':results}

class BulkResult(BaseModel):
    order_id:str=Field(max_length=40)
    expected_state:str=Field(pattern=r'^(processing|queued|needs_session|failed|completed)$')
    state:str=Field(pattern=r'^(completed|failed|refunded)$')
    evidence:str=Field(min_length=4,max_length=200)

class BulkResults(BaseModel):
    items:list[BulkResult]=Field(min_length=1,max_length=100)

@app.post('/api/admin/orders/results')
def bulk_results(data:BulkResults,request:Request):
    actor=staff_identity(request);now=int(time.time());results=[]
    allowed={'processing':{'completed','failed'},'queued':{'failed'},'needs_session':{'failed'},'failed':{'refunded'},'completed':{'refunded'}}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        for item in data.items:
            r=c.execute('SELECT * FROM orders WHERE id=?',(item.order_id,)).fetchone();reason=''
            if item.state=='refunded' and actor!='admin':reason='退款记录仅限管理员核验'
            elif not r:reason='订单不存在'
            elif r['state']!=item.expected_state:reason='状态已变更，请刷新核对'
            elif item.state not in allowed.get(r['state'],set()):reason='不允许此状态转换'
            elif len(item.evidence.strip())<4:reason='请填写真实凭证编号'
            else:
                c.execute('UPDATE orders SET state=?,evidence=?,credential=NULL,updated=? WHERE id=?',(item.state,item.evidence.strip(),now,item.order_id));event(c,item.order_id,item.state)
                c.execute('INSERT INTO audit(action,target,created,actor) VALUES(?,?,?,?)',('bulk.result.'+item.state,item.order_id,now,actor))
            results.append({'id':item.order_id,'ok':not reason,'reason':reason})
    return {'results':results}

class StaffInput(BaseModel):
    username:str=Field(pattern=r'^[a-zA-Z0-9_-]{3,40}$')
    password:str=Field(default='',max_length=128)
    active:bool=True

@app.post('/api/admin/staff')
def staff_manage(data:StaffInput,request:Request):
    identity(request,True)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        admin_name=c.execute('SELECT username FROM admins WHERE id=1').fetchone()['username']
        if data.username in ('admin',admin_name):fail('此账号名不可用')
        old=c.execute('SELECT 1 FROM staff WHERE username=?',(data.username,)).fetchone()
        if (not old or data.password) and len(data.password)<12:fail('密码至少 12 位')
        if not old:
            c.execute('INSERT INTO staff VALUES(?,?,?,?)',(data.username,password_hash(data.password),int(data.active),int(time.time())))
        elif data.password:c.execute('UPDATE staff SET password=?,active=? WHERE username=?',(password_hash(data.password),int(data.active),data.username))
        else:c.execute('UPDATE staff SET active=? WHERE username=?',(int(data.active),data.username))
        c.execute('DELETE FROM sessions WHERE role=?',('staff:'+data.username,))
        c.execute('INSERT INTO audit(action,target,created,actor) VALUES(?,?,?,?)',('staff.update',data.username,int(time.time()),'admin'))
    return {'ok':True}

@app.get('/api/admin/orders')
def search_orders(request:Request,state:str='',q:str='',page:int=1):
    staff_identity(request)
    if page<1 or page>100000 or len(q)>100 or len(state)>30:fail('查询条件无效')
    where='WHERE (?=\'\' OR state=?) AND (?=\'\' OR instr(id,?)>0 OR instr(email,?)>0)'
    args=(state,state,q,q,q)
    with db() as c:
        total=c.execute('SELECT COUNT(*) FROM orders '+where,args).fetchone()[0]
        rows=c.execute('SELECT * FROM orders '+where+' ORDER BY created DESC,id LIMIT 100 OFFSET ?',(*args,(page-1)*100))
        return {'orders':[{**view_order(r),'review':bool(r['review'])} for r in rows],'total':total,'page':page}

@app.get('/api/admin/tickets')
def search_tickets(request:Request,state:str='',q:str='',page:int=1,mine:bool=False):
    actor=staff_identity(request)
    if page<1 or page>100000 or len(q)>100 or state not in ('','open','replied','closed'):fail('查询条件无效')
    where="WHERE (?='' OR state=?) AND (?='' OR instr(id,?)>0 OR instr(subject,?)>0) AND (?=0 OR assignee=?)"
    args=(state,state,q,q,q,int(mine),actor)
    with db() as c:
        total=c.execute('SELECT COUNT(*) FROM tickets '+where,args).fetchone()[0]
        rows=c.execute('SELECT * FROM tickets '+where+' ORDER BY updated DESC,id LIMIT 100 OFFSET ?',(*args,(page-1)*100))
        return {'tickets':[ticket_view(r) for r in rows],'total':total,'page':page}
