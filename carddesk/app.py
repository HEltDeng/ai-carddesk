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
import csv
import io
import smtplib
import ssl
import socket
import ipaddress
from email.message import EmailMessage
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

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
USER_COOKIE = '__Host-carddesk-user' if SECURE else 'carddesk_user_local'
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

def body_limit(path):
    if path == '/api/admin/orders/results':
        return 131072
    if path == '/api/payment/callback':
        return 4096
    return 24576

class BodyLimitMiddleware:
    """Count actual streamed bytes, preserving each endpoint's existing limit."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        middleware = RequestBodyLimitMiddleware(self.app, max_body_size=body_limit(scope.get('path', '')))
        await middleware(scope, receive, send)

app.add_middleware(BodyLimitMiddleware)

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
    CREATE TABLE IF NOT EXISTS customers(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE COLLATE NOCASE,password TEXT NOT NULL,email TEXT UNIQUE COLLATE NOCASE,email_verified INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,created INTEGER NOT NULL,last_login INTEGER);
    CREATE TABLE IF NOT EXISTS customer_sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES customers(id),expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS email_challenges(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES customers(id),owner TEXT NOT NULL,email TEXT NOT NULL,purpose TEXT NOT NULL,code_hash TEXT NOT NULL,created INTEGER NOT NULL,expires INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,consumed INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS ix_challenge_user ON email_challenges(user_id,purpose,created);
    CREATE TABLE IF NOT EXISTS checkout_grants(owner TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES customers(id),expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS email_limits(key TEXT PRIMARY KEY,count INTEGER NOT NULL,window INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS deliveries(order_id TEXT PRIMARY KEY REFERENCES orders(id),account TEXT NOT NULL,secret TEXT NOT NULL,updated INTEGER NOT NULL);
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
        'orders': {'source_hash': "TEXT NOT NULL DEFAULT ''", 'review': "INTEGER NOT NULL DEFAULT 0", 'customer_id': 'TEXT REFERENCES customers(id)', 'quantity': 'INTEGER NOT NULL DEFAULT 1', 'paid_at': 'INTEGER', 'completed_at': 'INTEGER', 'refunded_at': 'INTEGER'},
        'audit': {'actor': "TEXT NOT NULL DEFAULT 'system'", 'source_hash': "TEXT NOT NULL DEFAULT ''", 'result': "TEXT NOT NULL DEFAULT 'success'", 'details': "TEXT NOT NULL DEFAULT '{}'", 'previous_hash': "TEXT NOT NULL DEFAULT ''", 'entry_hash': "TEXT NOT NULL DEFAULT ''"},
        'email_challenges': {'delivered': 'INTEGER NOT NULL DEFAULT 0'},
    }.items():
        existing={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
        for name,definition in columns.items():
            if name not in existing:c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    c.execute('CREATE INDEX IF NOT EXISTS ix_orders_source_created ON orders(source_hash,created)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_orders_customer ON orders(customer_id,state,created)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_audit_created ON audit(created,id)')
    c.execute('CREATE INDEX IF NOT EXISTS ix_audit_action ON audit(action,created)')
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

def audit_signature(row):
    fields=('id','action','target','created','actor','source_hash','result','details','previous_hash')
    return digest('audit:v1:'+json.dumps([row[k] for k in fields],ensure_ascii=False,separators=(',',':')))

def audit_log(c,action,target='',actor='system',request=None,result='success',details=None):
    # Call within the mutation transaction so neither the change nor its audit can be lost alone.
    if not c.in_transaction:c.execute('BEGIN IMMEDIATE')
    previous=c.execute('SELECT id,entry_hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
    row={'id':previous['id']+1 if previous else 1,'action':action,'target':target,'created':int(time.time()),'actor':actor,'source_hash':source_hash(request) if request else '', 'result':result,'details':json.dumps(details or {},ensure_ascii=False,sort_keys=True),'previous_hash':previous['entry_hash'] if previous else ''}
    row['entry_hash']=audit_signature(row)
    c.execute('INSERT INTO audit(id,action,target,created,actor,source_hash,result,details,previous_hash,entry_hash) VALUES(:id,:action,:target,:created,:actor,:source_hash,:result,:details,:previous_hash,:entry_hash)',row)

with db() as c:
    c.execute('BEGIN IMMEDIATE')
    if not cfg(c,'audit_chain_v1'):
        previous=''
        for record in c.execute('SELECT * FROM audit ORDER BY id').fetchall():
            row=dict(record);row['previous_hash']=previous;previous=audit_signature(row)
            c.execute('UPDATE audit SET previous_hash=?,entry_hash=? WHERE id=?',(row['previous_hash'],previous,row['id']))
        savecfg(c,'audit_chain_v1','1')
    c.execute("CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'audit is append only'); END")
    c.execute("CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'audit is append only'); END")
    if not cfg(c,'order_times_v1'):
        # Derive old timestamps only from recorded events; do not invent payment times.
        c.execute("UPDATE orders SET paid_at=(SELECT MIN(created) FROM events WHERE order_id=orders.id AND state IN ('queued','needs_session')) WHERE payment_ref IS NOT NULL")
        c.execute("UPDATE orders SET completed_at=(SELECT MAX(created) FROM events WHERE order_id=orders.id AND state='completed'),refunded_at=(SELECT MAX(created) FROM events WHERE order_id=orders.id AND state='refunded')")
        savecfg(c,'order_times_v1','1')

def customer_session(request,required=True):
    token=digest(request.cookies.get(USER_COOKIE,''))
    with db() as c:
        row=c.execute('SELECT u.*,s.token AS session_token FROM customers u JOIN customer_sessions s ON s.user_id=u.id WHERE s.token=? AND s.expires>? AND u.active=1',(token,int(time.time()))).fetchone()
    if not row and required:fail('请先登录购买账号',401)
    return row

def verified_customer(request):
    user=customer_session(request)
    if not user['email_verified']:fail('请先绑定邮箱并输入验证码',403)
    return user

def customer_view(user):
    return {k:user[k] for k in ('id','username','email','email_verified','active','created','last_login')}

def issue_customer_session(c,user_id,request):
    raw=secrets.token_urlsafe(32);now=int(time.time())
    c.execute('DELETE FROM customer_sessions WHERE expires<=? OR token=?',(now,digest(request.cookies.get(USER_COOKIE,''))))
    c.execute('INSERT INTO customer_sessions VALUES(?,?,?)',(digest(raw),user_id,now+604800))
    c.execute('UPDATE customers SET last_login=? WHERE id=?',(now,user_id))
    out=JSONResponse({'ok':True})
    out.set_cookie(USER_COOKIE,raw,httponly=True,secure=SECURE,samesite='strict',path='/',max_age=604800)
    return out

def valid_password(password,stored):
    candidate=password_hash(password,stored.split(':')[0] if stored else '0'*32)
    return bool(stored) and hmac.compare_digest(candidate,stored)

def normalize_email(value):
    value=value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+",value) or len(value)>254:fail('请输入有效邮箱')
    return value

def mail_config(c):
    return {k:cfg(c,'smtp_'+k) for k in ('host','port','username','password','sender')}

def mail_ready(c):
    return all(mail_config(c).values())

class PublicSMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        # Resolve once and connect to those numeric addresses. smtplib keeps
        # the configured hostname for certificate checks during STARTTLS.
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(info[4][0] for info in answers))
        if not addresses:
            raise ValueError('No mail server address')
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast or ip.is_reserved:
                raise ValueError('Invalid mail host')
        for index, address in enumerate(addresses):
            try:
                sock = socket.create_connection((address, port), timeout, self.source_address)
            except OSError:
                if index == len(addresses) - 1:
                    raise
                continue
            if port == 465:
                try:
                    return ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                except BaseException:
                    sock.close()
                    raise
            return sock

def send_email_code(config,email,code,purpose):
    host=config['host'];port=int(config['port'])
    message=EmailMessage();message['From']=config['sender'];message['To']=email;message['Subject']='AI 卡台 · '+('重置密码' if purpose=='reset' else '邮箱验证码')
    message.set_content(f'你的验证码为：{code}\n\n10 分钟内有效，请勿转发。若非本人操作，请忽略此邮件。')
    with PublicSMTP(host,port,timeout=12) as client:
        if port!=465:client.ehlo();client.starttls(context=ssl.create_default_context());client.ehlo()
        client.login(config['username'],decrypt(config['password']))
        client.send_message(message)

def email_limit(c,key,maximum,seconds):
    now=int(time.time());window=now//seconds;key=digest('email-limit:'+key+':'+str(seconds))
    old=c.execute('SELECT * FROM email_limits WHERE key=?',(key,)).fetchone()
    if old and old['window']==window and old['count']>=maximum:fail('验证码发送过于频繁，请稍后重试',429)
    c.execute('INSERT INTO email_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END,window=excluded.window',(key,window))

def reauthenticate_admin(c,request,password):
    identity(request,True);limited(request,'admin-reauth',5)
    stored=c.execute('SELECT password FROM admins WHERE id=1').fetchone()
    if not stored or not valid_password(password,stored['password']):fail('管理员密码不正确',403)

@app.middleware('http')
async def boundary(request,call_next):
    if request.method in ('POST','PUT','PATCH','DELETE'):
        if request.url.path!='/api/payment/callback' and request.headers.get('origin') != ORIGIN:
            return JSONResponse({'detail':'请求来源无效'},403)
        try:
            length = int(request.headers.get('content-length','0'))
            if length < 0: raise ValueError
            if length > body_limit(request.url.path): return JSONResponse({'detail':'内容过长'},413)
        except ValueError:
            return JSONResponse({'detail':'请求无效'},400)
    response=await call_next(request)
    if request.url.path.startswith('/api/') and response.status_code in (401,403,429):
        with db() as c:
            user=customer_session(request,False)
            audit_log(c,'security.denied',request.url.path[:160],actor='customer:'+user['id'] if user else 'anonymous',request=request,result='denied',details={'status':response.status_code})
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

def payment_template(c):
    return cfg(c,'payment_url') if cfg(c,'payment_secret') else ''

@app.get('/api/catalog')
def catalog():
    with db() as c:
        return {'products':[view_product(r) for r in c.execute('SELECT * FROM products WHERE active=1 ORDER BY sort_order,id')], 'payment_enabled':bool(payment_template(c)), 'fulfillment_enabled':bool(cfg(c,'provider_url') and cfg(c,'provider_key'))}

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
    return {k:row[k] for k in ('id','title','price','email','state','created','updated','channel','quantity','paid_at','completed_at','refunded_at','customer_id')}

def expire_unpaid(c,now=None):
    now=now or int(time.time())
    rows=c.execute("SELECT id FROM orders WHERE channel='payment' AND payment_ref IS NULL AND state='pending_payment' AND created<=?",(now-900,)).fetchall()
    for r in rows:
        c.execute("UPDATE orders SET state='expired',credential=NULL,credential_expiry=0,updated=? WHERE id=?",(now,r['id']))
        event(c,r['id'],'expired')
    return len(rows)

def event(c,oid,state):
    c.execute('INSERT INTO events(order_id,state,created) VALUES(?,?,?)',(oid,state,int(time.time())))
    if state in ('completed','refunded'):
        column='completed_at' if state=='completed' else 'refunded_at'
        c.execute(f'UPDATE orders SET {column}=? WHERE id=?',(int(time.time()),oid))
    audit_log(c,'order.state',oid,details={'state':state})

def payment_link(template,oid,price):
    # Only order number and amount are appended; never credentials or personal data.
    if not template:return ''
    u=urlparse(template)
    q=dict(parse_qsl(u.query));q.update(order_id=oid,amount=f'{price/100:.2f}')
    return urlunparse(u._replace(query=urlencode(q)))

@app.post('/api/orders')
def create_order(data:OrderInput,request:Request):
    limited(request,'order',10); user=verified_customer(request);owner=user['session_token']
    if not data.consent: fail('请确认服务协议和隐私政策')
    oid='AI'+digest(user['id']+data.idempotency_key)[:22].upper()
    lookup=base64.urlsafe_b64encode(hmac.new(KEY,('lookup:'+oid).encode(),hashlib.sha256).digest()).decode()[:24]
    now=int(time.time()); source=source_hash(request)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        expire_unpaid(c,now)
        old=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if old:
            if old['product_id']!=data.product_id or old['email'].lower()!=data.email.strip().lower() or old['channel']!=('card' if data.card else 'payment'):fail('重复请求与原订单不一致',409)
            return {'order':view_order(old),'lookup_key':lookup,'payment_url':payment_link(payment_template(c),oid,old['price']) if old['state']=='pending_payment' else ''}
        # Recheck account status in the same write transaction as order creation.
        current=c.execute('SELECT active,email_verified FROM customers WHERE id=?',(user['id'],)).fetchone()
        if not current or not current['active'] or not current['email_verified']:fail('账号当前不可购买',403)
        grant=c.execute('SELECT 1 FROM checkout_grants WHERE owner=? AND user_id=? AND expires>?',(owner,user['id'],now)).fetchone()
        if not grant:fail('请先输入邮箱验证码确认本次购买',403)
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(data.product_id,)).fetchone()
        if not p: fail('商品暂不可用',409)
        if p['category']!='chatgpt':fail('该品类通道暂不可用',409)
        if not p['price_configured']:fail('商品暂不可用',409)
        if not cfg(c,'provider_url') or not cfg(c,'provider_key'): fail('当前通道暂不可用',409)
        if not data.card and not payment_template(c): fail('当前支付暂不可用',409)
        s=parse_session(data.session)
        if s['email'].lower()!=data.email.strip().lower(): fail('账号与 Session 不一致')
        if data.card:
            card=c.execute('SELECT * FROM cards WHERE hash=?',(digest(data.card.strip().upper()),)).fetchone()
            if not card or card['state']!='ready' or card['product_id']!=p['id']: fail('卡密无效、已使用或套餐不符',409)
        # No unverified email can lock an account or reserve inventory.
        if not data.card:
            pending=c.execute("SELECT COUNT(*) FROM orders WHERE customer_id=? AND state='pending_payment'",(user['id'],)).fetchone()[0]
            from_source=c.execute("SELECT COUNT(*) FROM orders WHERE source_hash=? AND state='pending_payment'",(source,)).fetchone()[0]
            hourly=c.execute('SELECT COUNT(*) FROM orders WHERE source_hash=? AND created>?',(source,now-3600)).fetchone()[0]
            customer_hourly=c.execute('SELECT COUNT(*) FROM orders WHERE customer_id=? AND created>?',(user['id'],now-3600)).fetchone()[0]
            if pending>=3 or from_source>=8 or hourly>=30 or customer_hourly>=10:fail('待支付订单过多，请先处理已有订单或稍后重试',429)
        state='queued' if data.card else 'pending_payment'
        c.execute('INSERT INTO orders(id,owner,lookup_hash,product_id,title,price,email,state,created,updated,credential,credential_expiry,channel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,owner,digest(lookup),p['id'],p['title'],p['price'],s['email'],state,now,now,encrypt(s['accessToken']),min(s['expiry'],now+(1800 if data.card else 900)),'card' if data.card else 'payment'))
        c.execute('UPDATE orders SET source_hash=?,customer_id=? WHERE id=?',(source,user['id'],oid))
        c.execute('DELETE FROM checkout_grants WHERE owner=?',(owner,))
        if data.card: c.execute("UPDATE cards SET state='used',order_id=? WHERE hash=? AND state='ready'",(oid,digest(data.card.strip().upper())))
        event(c,oid,state)
        audit_log(c,'order.create',oid,actor='customer:'+user['id'],request=request,details={'channel':'card' if data.card else 'payment','amount_cents':p['price']})
        row=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        return {'order':view_order(row),'lookup_key':lookup,'payment_url':payment_link(payment_template(c),oid,p['price']) if not data.card else ''}

class CardInput(BaseModel):
    card:str=Field(min_length=6,max_length=100)

@app.post('/api/cards/check')
def check_card(data:CardInput,request:Request):
    limited(request,'card',10);verified_customer(request)
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
    user=customer_session(request,False)
    with db() as c: return {'orders':[view_order(r) for r in c.execute('SELECT * FROM orders WHERE customer_id=? OR (customer_id IS NULL AND owner=?) ORDER BY created DESC LIMIT 50',(user['id'] if user else '',owner))]}

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
        c.execute('UPDATE orders SET state=?,payment_ref=?,paid_at=?,updated=? WHERE id=?',(state,ref,int(time.time()),int(time.time()),oid));event(c,oid,state)
        audit_log(c,'payment.confirmed',oid,actor='payment',details={'amount_cents':amount})
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
    role = 'admin'
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM admins WHERE id=1').fetchone()
        if not r:
            f=DATA/'setup-code'
            if not f.exists() or not hmac.compare_digest(f.read_bytes().strip(),data.setup_code.encode()): fail('初始化码不正确',403)
            c.execute('INSERT INTO admins VALUES(1,?,?)',(data.username,password_hash(data.password)))
        else:
            if data.username == r['username']:
                account = r
            else:
                account = c.execute('SELECT * FROM staff WHERE username=? AND active=1',(data.username,)).fetchone()
                role = 'staff:'+data.username
            if not valid_password(data.password, account['password'] if account else ''):
                fail('账号或密码不正确',401)
        audit_log(c,'admin.login',data.username,role,request)
    (DATA/'setup-code').unlink(missing_ok=True)
    return session_response(role,{'ok':True})

@app.post('/api/admin/logout')
def logout(request:Request):
    raw=request.cookies.get(COOKIE,'')
    with db() as c:
        row=c.execute('SELECT role FROM sessions WHERE token=?',(digest(raw),)).fetchone()
        c.execute('DELETE FROM sessions WHERE token=?',(digest(raw),))
        if row:audit_log(c,'admin.logout',actor=row['role'],request=request)
    r=JSONResponse({'ok':True});r.delete_cookie(COOKIE,path='/',secure=SECURE,httponly=True,samesite='strict');return r

@app.get('/api/admin/dashboard')
def dashboard(request:Request):
    actor=staff_identity(request)
    with db() as c:
        orders=[{**view_order(r),'review':bool(r['review'])} for r in c.execute('SELECT * FROM orders ORDER BY created DESC LIMIT 200')]
        result={'role':'admin' if actor=='admin' else 'staff','actor':actor,'orders':orders,'tickets':[ticket_view(r) for r in c.execute('SELECT * FROM tickets ORDER BY updated DESC LIMIT 200')], 'stats':{'orders':c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],'ready_cards':c.execute("SELECT COUNT(*) FROM cards WHERE state='ready'").fetchone()[0],'paid_cents':c.execute('SELECT COALESCE(SUM(price),0) FROM orders WHERE payment_ref IS NOT NULL').fetchone()[0]},'products':[], 'cards':[], 'settings':{},'staff':[]}
        if actor=='admin':
            result.update(products=[view_product(r) for r in c.execute("SELECT * FROM products WHERE id!='plus-3' ORDER BY sort_order,id")],cards=[dict(r) for r in c.execute('SELECT tail,product_id,state,created,order_id FROM cards ORDER BY created DESC LIMIT 200')],settings={'payment_url':cfg(c,'payment_url'),'payment_secret_set':bool(cfg(c,'payment_secret')),'provider_url':cfg(c,'provider_url'),'provider_key_set':bool(cfg(c,'provider_key'))},staff=[dict(r) for r in c.execute('SELECT username,active,created FROM staff ORDER BY created')])
        if actor=='admin':
            mail=mail_config(c)
            result['email_settings']={k:v for k,v in mail.items() if k!='password'}
            result['email_settings']['password_set']=bool(mail['password'])
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
        audit_log(c,'product.update',data.id,'admin',request,details={'price':data.price,'active':data.active})
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
        audit_log(c,'cards.generate',data.product_id,'admin',request,details={'count':data.count})
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
        audit_log(c,'settings.update','payment/provider','admin',request)
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
        audit_log(c,'order.'+data.state,data.order_id,'admin',request,details={'before':r['state'],'after':data.state})
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

class StorefrontFiles(StaticFiles):
    # The development storefront is a nested Git checkout.
    public_paths = {
        'index.html', 'admin.html', 'service.html', 'icon.svg',
        'app.js', 'account.js', 'help.js', 'admin.js', 'admin-extra.js',
        'styles.css', 'storefront.css',
        'brands/band-curve.svg', 'brands/chatgpt.svg', 'brands/claude.svg',
        'brands/codex.svg', 'brands/grok.svg', 'brands/menu.svg',
        'brands/LICENSE', 'brands/SOURCES.md',
    }

    async def get_response(self, path, scope):
        if Path(path).as_posix() not in self.public_paths:
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)

app.mount('/static',StorefrontFiles(directory=ROOT/'static'),name='static')

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
            if not order or not (order['owner']==owner or (customer_session(request,False) and order['customer_id']==customer_session(request,False)['id']) or (data.order_key and hmac.compare_digest(order['lookup_hash'],digest(data.order_key)))):fail('订单号或查询码不正确',404)
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
        audit_log(c,'ticket.reply',r['id'],actor,request)
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
            if not reason:audit_log(c,'bulk.'+data.action,oid,actor,request)
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
            if not reason:audit_log(c,'ticket.'+data.action,tid,actor,request)
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
                audit_log(c,'bulk.result.'+item.state,item.order_id,actor,request,details={'before':r['state'],'after':item.state})
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
        audit_log(c,'staff.update',data.username,'admin',request,details={'active':data.active,'password_changed':bool(data.password)})
    return {'ok':True}

@app.get('/api/admin/orders')
def search_orders(request:Request,state:str='',q:str='',page:int=1,start:str='',end:str=''):
    staff_identity(request)
    if page<1 or page>100000 or len(q)>100 or len(state)>30:fail('查询条件无效')
    since,until=date_range(start,end)
    where="WHERE created>=? AND created<? AND (?='' OR state=?) AND (?='' OR instr(id,?)>0 OR instr(email,?)>0 OR customer_id IN (SELECT id FROM customers WHERE instr(username,?)>0))"
    args=(since,until,state,state,q,q,q,q)
    with db() as c:
        total=c.execute('SELECT COUNT(*) FROM orders '+where,args).fetchone()[0]
        rows=c.execute('SELECT * FROM orders '+where+' ORDER BY created DESC,id LIMIT 100 OFFSET ?',(*args,(page-1)*100))
        return {'orders':[{**view_order(r),'review':bool(r['review']),'buyer':(c.execute('SELECT username FROM customers WHERE id=?',(r['customer_id'],)).fetchone() or ['历史访客'])[0],'has_delivery':bool(c.execute('SELECT 1 FROM deliveries WHERE order_id=?',(r['id'],)).fetchone())} for r in rows],'total':total,'page':page}

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

# Customer authentication is separate from operator sessions.
class CustomerLogin(BaseModel):
    username:str=Field(pattern=r'^[A-Za-z0-9_-]{3,40}$')
    password:str=Field(min_length=12,max_length=128)

@app.get('/api/account')
def account_state(request:Request):
    user=customer_session(request,False)
    with db() as c:
        grant=bool(user and c.execute('SELECT 1 FROM checkout_grants WHERE owner=? AND expires>?',(user['session_token'],int(time.time()))).fetchone())
        return {'user':customer_view(user) if user else None,'purchase_verified':grant,'email_available':mail_ready(c)}

@app.post('/api/account/register')
def customer_register(data:CustomerLogin,request:Request):
    limited(request,'customer-register',3)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        email_limit(c,'registration:'+source_hash(request),10,86400)
        if c.execute('SELECT 1 FROM customers WHERE username=?',(data.username,)).fetchone():fail('该账号名不可用',409)
        uid='U'+secrets.token_hex(12)
        c.execute('INSERT INTO customers(id,username,password,created) VALUES(?,?,?,?)',(uid,data.username,password_hash(data.password),int(time.time())))
        audit_log(c,'account.register',uid,'customer:'+uid,request)
        return issue_customer_session(c,uid,request)

@app.post('/api/account/login')
def customer_login(data:CustomerLogin,request:Request):
    limited(request,'customer-login',5)
    with db() as c:
        user=c.execute('SELECT * FROM customers WHERE username=?',(data.username,)).fetchone()
        if not valid_password(data.password,user['password'] if user else '') or not user or not user['active']:fail('账号或密码不正确，或账号已停用',401)
        c.execute('BEGIN IMMEDIATE')
        # Ensure a concurrently disabled account cannot receive a usable session.
        if not c.execute('SELECT 1 FROM customers WHERE id=? AND active=1 AND password=?',(user['id'],user['password'])).fetchone():fail('账号状态已变更，请重新登录',401)
        audit_log(c,'account.login',user['id'],'customer:'+user['id'],request)
        return issue_customer_session(c,user['id'],request)

@app.post('/api/account/logout')
def customer_logout(request:Request):
    user=customer_session(request,False);token=digest(request.cookies.get(USER_COOKIE,''))
    with db() as c:
        c.execute('DELETE FROM customer_sessions WHERE token=?',(token,))
        c.execute('DELETE FROM checkout_grants WHERE owner=?',(token,))
        if user:audit_log(c,'account.logout',user['id'],'customer:'+user['id'],request)
    out=JSONResponse({'ok':True});out.delete_cookie(USER_COOKIE,path='/',secure=SECURE,httponly=True,samesite='strict');return out

class EmailRequest(BaseModel):
    purpose:str=Field(pattern=r'^(bind|purchase|reset)$')
    email:str=Field(default='',max_length=254)

@app.post('/api/account/email/send')
def email_send(data:EmailRequest,request:Request):
    limited(request,'email-send',5)
    if data.purpose=='reset':
        owner=identity(request);email=normalize_email(data.email)
        with db() as c:user=c.execute('SELECT * FROM customers WHERE email=? AND email_verified=1 AND active=1',(email,)).fetchone()
    else:
        user=customer_session(request);owner=user['session_token']
        if data.purpose=='bind':
            if user['email_verified']:fail('邮箱已绑定，请使用购买验证',409)
            email=normalize_email(data.email)
        else:
            if not user['email_verified']:fail('请先绑定邮箱',403)
            email=user['email']
    challenge=secrets.token_urlsafe(24);code=f'{secrets.randbelow(1000000):06d}';now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not mail_ready(c):fail('邮箱验证服务暂不可用，请稍后重试',503)
        email_limit(c,'ip:'+source_hash(request),20,3600)
        email_limit(c,'email:'+email,1,60);email_limit(c,'email:'+email,5,3600)
        if user:email_limit(c,'user:'+user['id'],5,3600)
        config=mail_config(c)
        c.execute('DELETE FROM email_challenges WHERE expires<?',(now-86400,))
        if user:
            if data.purpose=='bind' and c.execute('SELECT 1 FROM customers WHERE email=? AND id!=?',(email,user['id'])).fetchone():fail('该邮箱不可绑定',409)
            c.execute('UPDATE email_challenges SET consumed=1 WHERE user_id=? AND purpose=? AND owner=?',(user['id'],data.purpose,owner))
            c.execute('INSERT INTO email_challenges(id,user_id,owner,email,purpose,code_hash,created,expires) VALUES(?,?,?,?,?,?,?,?)',(challenge,user['id'],owner,email,data.purpose,digest(challenge+':'+code),now,now+600))
    if user:
        try:send_email_code(config,email,code,data.purpose)
        except Exception:
            with db() as c:audit_log(c,'email.send',user['id'],'customer:'+user['id'],request,result='failed',details={'purpose':data.purpose})
            fail('验证码发送失败，请稍后重试',503)
        with db() as c:
            c.execute('UPDATE email_challenges SET delivered=1 WHERE id=?',(challenge,))
            audit_log(c,'email.send',user['id'],'customer:'+user['id'],request,details={'purpose':data.purpose})
    # A random challenge for unknown reset addresses prevents account enumeration.
    return {'challenge_id':challenge,'expires_in':600,'retry_after':60}

class EmailVerify(BaseModel):
    challenge_id:str=Field(min_length=20,max_length=100)
    code:str=Field(pattern=r'^\d{6}$')
    new_password:str=Field(default='',max_length=128)

@app.post('/api/account/email/verify')
def email_verify(data:EmailVerify,request:Request):
    limited(request,'email-verify',10);now=int(time.time());error=None
    user=customer_session(request,False)
    owners={digest(request.cookies.get(COOKIE,'')),digest(request.cookies.get(USER_COOKIE,''))}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT e.*,u.active FROM email_challenges e JOIN customers u ON u.id=e.user_id WHERE e.id=?',(data.challenge_id,)).fetchone()
        if not row or row['owner'] not in owners or not row['active'] or not row['delivered'] or row['consumed'] or row['expires']<=now or row['attempts']>=5:
            error='验证码无效或已过期，请重新获取'
        elif row['purpose']!='reset' and (not user or user['id']!=row['user_id']):error='请重新登录后验证'
        elif not hmac.compare_digest(row['code_hash'],digest(row['id']+':'+data.code)):
            c.execute('UPDATE email_challenges SET attempts=attempts+1 WHERE id=?',(row['id'],));error='验证码不正确'
        elif row['purpose']=='reset' and len(data.new_password)<12:error='新密码至少 12 位'
        elif row['purpose']=='bind' and c.execute('SELECT 1 FROM customers WHERE email=? AND id!=?',(row['email'],row['user_id'])).fetchone():error='该邮箱不可绑定'
        else:
            c.execute('UPDATE email_challenges SET consumed=1 WHERE id=?',(row['id'],))
            if row['purpose']=='reset':
                c.execute('UPDATE customers SET password=? WHERE id=?',(password_hash(data.new_password),row['user_id']))
                c.execute('DELETE FROM customer_sessions WHERE user_id=?',(row['user_id'],));c.execute('DELETE FROM checkout_grants WHERE user_id=?',(row['user_id'],))
                c.execute('UPDATE email_challenges SET consumed=1 WHERE user_id=?',(row['user_id'],))
            else:
                if row['purpose']=='bind':
                    c.execute('UPDATE customers SET email=?,email_verified=1 WHERE id=?',(row['email'],row['user_id']))
                    c.execute("UPDATE email_challenges SET consumed=1 WHERE user_id=? AND purpose='bind'",(row['user_id'],))
                c.execute('INSERT INTO checkout_grants VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET expires=excluded.expires',(row['owner'],row['user_id'],now+600))
            audit_log(c,'email.verify.'+row['purpose'],row['user_id'],'customer:'+row['user_id'],request)
        if error:audit_log(c,'email.verify','challenge',actor='customer:'+user['id'] if user else 'anonymous',request=request,result='denied')
    if error:fail(error,400)
    return {'ok':True,'purpose':row['purpose']}

class PasswordChange(BaseModel):
    current_password:str=Field(max_length=128)
    new_password:str=Field(min_length=12,max_length=128)

@app.post('/api/account/password')
def customer_password(data:PasswordChange,request:Request):
    limited(request,'password-change',5);user=customer_session(request)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT password FROM customers WHERE id=?',(user['id'],)).fetchone()
        if not valid_password(data.current_password,current['password']):fail('当前密码不正确',403)
        c.execute('UPDATE customers SET password=? WHERE id=?',(password_hash(data.new_password),user['id']))
        c.execute('DELETE FROM customer_sessions WHERE user_id=?',(user['id'],));c.execute('DELETE FROM checkout_grants WHERE user_id=?',(user['id'],))
        c.execute('UPDATE email_challenges SET consumed=1 WHERE user_id=?',(user['id'],))
        audit_log(c,'account.password',user['id'],'customer:'+user['id'],request)
    return {'ok':True}

class MailSettings(BaseModel):
    host:str=Field(pattern=r'^[a-zA-Z0-9.-]{1,253}$')
    port:int=Field(ge=1,le=65535)
    username:str=Field(min_length=1,max_length=254)
    password:str=Field(default='',max_length=256)
    sender:str=Field(max_length=254)

@app.post('/api/admin/email-settings')
def email_settings(data:MailSettings,request:Request):
    identity(request,True)
    if data.port not in (465,587):fail('请选择 465（TLS）或 587（STARTTLS）')
    sender=normalize_email(data.sender)
    if data.host.lower()=='localhost' or data.host.lower().endswith(('.local','.internal')):fail('邮箱服务器地址不可用')
    with db() as c:
        for key,value in {'host':data.host,'port':str(data.port),'username':data.username,'sender':sender}.items():savecfg(c,'smtp_'+key,value)
        if data.password:savecfg(c,'smtp_password',encrypt(data.password))
        if not cfg(c,'smtp_password'):fail('请填写 SMTP 授权码')
        audit_log(c,'settings.email','smtp','admin',request)
    return {'ok':True}

@app.get('/api/admin/customers')
def customers_search(request:Request,q:str='',page:int=1):
    identity(request,True)
    if len(q)>100 or not 1<=page<=100000:fail('查询条件无效')
    where="WHERE ?='' OR instr(username,?)>0 OR instr(COALESCE(email,''),?)>0 OR id=?";args=(q,q,q,q)
    with db() as c:
        total=c.execute('SELECT COUNT(*) FROM customers '+where,args).fetchone()[0]
        rows=c.execute('SELECT * FROM customers '+where+' ORDER BY created DESC,id LIMIT 50 OFFSET ?',(*args,(page-1)*50)).fetchall()
        return {'customers':[customer_view(r) for r in rows],'total':total,'page':page}

class CustomerManage(BaseModel):
    user_id:str=Field(max_length=40)
    active:bool
    password:str=Field(default='',max_length=128)
    admin_password:str=Field(default='',max_length=128)

@app.post('/api/admin/customers')
def customer_manage(data:CustomerManage,request:Request):
    identity(request,True)
    if data.password and len(data.password)<12:fail('新密码至少 12 位')
    with db() as c:
        if data.password:reauthenticate_admin(c,request,data.admin_password)
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM customers WHERE id=?',(data.user_id,)).fetchone():fail('账号不存在',404)
        c.execute('UPDATE customers SET active=? WHERE id=?',(int(data.active),data.user_id))
        if data.password:c.execute('UPDATE customers SET password=? WHERE id=?',(password_hash(data.password),data.user_id))
        c.execute('DELETE FROM customer_sessions WHERE user_id=?',(data.user_id,));c.execute('DELETE FROM checkout_grants WHERE user_id=?',(data.user_id,))
        c.execute('UPDATE email_challenges SET consumed=1 WHERE user_id=?',(data.user_id,))
        audit_log(c,'customer.update',data.user_id,'admin',request,details={'active':data.active,'password_reset':bool(data.password)})
    return {'ok':True}

class DeliveryWrite(BaseModel):
    order_id:str=Field(max_length=40)
    account:str=Field(min_length=1,max_length=254)
    password:str=Field(min_length=1,max_length=512)
    admin_password:str=Field(max_length=128)
    reason:str=Field(min_length=4,max_length=120)

@app.post('/api/admin/delivery')
def delivery_write(data:DeliveryWrite,request:Request):
    identity(request,True)
    with db() as c:
        reauthenticate_admin(c,request,data.admin_password)
        c.execute('BEGIN IMMEDIATE')
        order=c.execute('SELECT * FROM orders WHERE id=?',(data.order_id,)).fetchone()
        if not order or not (order['payment_ref'] or order['channel']=='card') or order['state'] in ('pending_payment','expired','refunded'):fail('仅可为已付款或已核销订单登记交付账号',409)
        # The delivery account and password never appear in order lists or CSV exports.
        c.execute('INSERT INTO deliveries VALUES(?,?,?,?) ON CONFLICT(order_id) DO UPDATE SET account=excluded.account,secret=excluded.secret,updated=excluded.updated',(data.order_id,encrypt(data.account),encrypt(data.password),int(time.time())))
        audit_log(c,'delivery.write',data.order_id,'admin',request,details={'reason':support_text(data.reason)})
    return {'ok':True}

class DeliveryRead(BaseModel):
    order_id:str=Field(max_length=40)
    admin_password:str=Field(default='',max_length=128)
    reason:str=Field(default='',max_length=120)

@app.post('/api/admin/delivery/read')
def delivery_read(data:DeliveryRead,request:Request):
    identity(request,True)
    if len(data.reason.strip())<4:fail('请填写至少 4 字的查看原因')
    with db() as c:
        reauthenticate_admin(c,request,data.admin_password)
        row=c.execute('SELECT * FROM deliveries WHERE order_id=?',(data.order_id,)).fetchone()
        if not row:fail('尚未登记交付账号',404)
        audit_log(c,'delivery.reveal',data.order_id,'admin',request,details={'reason':support_text(data.reason)})
        return {'account':decrypt(row['account']),'password':decrypt(row['secret']),'updated':row['updated']}

@app.post('/api/orders/delivery')
def customer_delivery(data:DeliveryRead,request:Request):
    user=verified_customer(request);limited(request,'delivery-read',10)
    with db() as c:
        row=c.execute("SELECT d.* FROM deliveries d JOIN orders o ON o.id=d.order_id WHERE o.id=? AND o.customer_id=? AND o.state='completed'",(data.order_id,user['id'])).fetchone()
        if not row:fail('交付信息暂不可用',404)
        audit_log(c,'delivery.customer_reveal',data.order_id,'customer:'+user['id'],request)
        return {'account':decrypt(row['account']),'password':decrypt(row['secret'])}

@app.get('/api/orders/detail')
def customer_order_detail(request:Request,order_id:str):
    user=customer_session(request)
    if len(order_id)>40:fail('订单不存在',404)
    with db() as c:
        row=c.execute('SELECT * FROM orders WHERE id=? AND customer_id=?',(order_id,user['id'])).fetchone()
        if not row:fail('订单不存在',404)
        lookup=base64.urlsafe_b64encode(hmac.new(KEY,('lookup:'+order_id).encode(),hashlib.sha256).digest()).decode()[:24]
        return {'order':{**view_order(row),'has_delivery':bool(c.execute('SELECT 1 FROM deliveries WHERE order_id=?',(order_id,)).fetchone())},'lookup_key':lookup,'events':[dict(r) for r in c.execute('SELECT state,created FROM events WHERE order_id=? ORDER BY id',(order_id,))],'payment_url':payment_link(payment_template(c),order_id,row['price']) if row['state']=='pending_payment' else ''}

# Dates use Asia/Shanghai regardless of the server's timezone.
CN_TZ=timezone(timedelta(hours=8))
def date_range(start='',end=''):
    try:
        since=int(datetime.strptime(start,'%Y-%m-%d').replace(tzinfo=CN_TZ).timestamp()) if start else 0
        until=int((datetime.strptime(end,'%Y-%m-%d').replace(tzinfo=CN_TZ)+timedelta(days=1)).timestamp()) if end else 4102444800
        if since>=until:raise ValueError()
        return since,until
    except ValueError:fail('日期范围无效')

@app.get('/api/admin/summary')
def admin_summary(request:Request,start:str='',end:str=''):
    staff_identity(request);since,until=date_range(start,end)
    with db() as c:
        rows=c.execute('SELECT COUNT(*) orders,COALESCE(SUM(quantity),0) quantity,COALESCE(SUM(price),0) ordered_cents FROM orders WHERE created>=? AND created<?',(since,until)).fetchone()
        paid=c.execute('SELECT COUNT(*) count,COALESCE(SUM(price),0) amount FROM orders WHERE payment_ref IS NOT NULL AND paid_at>=? AND paid_at<?',(since,until)).fetchone()
        refunded=c.execute('SELECT COUNT(*) count,COALESCE(SUM(price),0) amount FROM orders WHERE payment_ref IS NOT NULL AND refunded_at>=? AND refunded_at<?',(since,until)).fetchone()
        redeemed=c.execute("SELECT COUNT(*) count,COALESCE(SUM(price),0) amount FROM orders WHERE channel='card' AND created>=? AND created<?",(since,until)).fetchone()
        return {**dict(rows),'paid_orders':paid['count'],'paid_cents':paid['amount'],'refunded_cents':refunded['amount'],'net_cents':paid['amount']-refunded['amount'],'redeemed_orders':redeemed['count'],'redeemed_cents':redeemed['amount'],'pending_orders':c.execute("SELECT COUNT(*) FROM orders WHERE state='pending_payment'").fetchone()[0], 'by_day':[dict(r) for r in c.execute("SELECT date(paid_at,'unixepoch','+8 hours') day,COUNT(*) orders,SUM(price) amount FROM orders WHERE payment_ref IS NOT NULL AND paid_at>=? AND paid_at<? GROUP BY day ORDER BY day DESC LIMIT 31",(since,until))]}

@app.get('/api/admin/audit')
def audit_search(request:Request,q:str='',action:str='',result:str='',start:str='',end:str='',page:int=1):
    identity(request,True);since,until=date_range(start,end)
    if len(q)>100 or len(action)>80 or result not in ('','success','failed','denied') or not 1<=page<=100000:fail('查询条件无效')
    where="WHERE created>=? AND created<? AND (?='' OR action=?) AND (?='' OR result=?) AND (?='' OR instr(actor,?)>0 OR instr(target,?)>0)"
    args=(since,until,action,action,result,result,q,q,q)
    with db() as c:
        total=c.execute('SELECT COUNT(*) FROM audit '+where,args).fetchone()[0]
        rows=[dict(r) for r in c.execute('SELECT id,action,target,created,actor,source_hash,result,details FROM audit '+where+' ORDER BY id DESC LIMIT 50 OFFSET ?',(*args,(page-1)*50))]
        return {'entries':rows,'total':total,'actions':[r[0] for r in c.execute('SELECT DISTINCT action FROM audit ORDER BY action')],'page':page}

@app.post('/api/admin/audit/verify')
def audit_verify(request:Request):
    identity(request,True)
    with db() as c:
        c.execute('BEGIN IMMEDIATE');previous='';count=0;valid=True
        for row in c.execute('SELECT * FROM audit ORDER BY id'):
            if row['previous_hash']!=previous or not hmac.compare_digest(audit_signature(row),row['entry_hash']):valid=False;break
            previous=row['entry_hash'];count+=1
        if valid:audit_log(c,'audit.verify','audit','admin',request,details={'entries':count,'head':previous})
        return {'valid':valid,'checked':count,'head':previous}

class ExportOrders(BaseModel):
    q:str=Field(default='',max_length=100)
    state:str=Field(default='',max_length=30)
    start:str=Field(default='',max_length=10)
    end:str=Field(default='',max_length=10)

@app.post('/api/admin/orders/export')
def export_orders(data:ExportOrders,request:Request):
    actor=staff_identity(request);since,until=date_range(data.start,data.end)
    with db() as c:
        rows=c.execute("SELECT * FROM orders WHERE created>=? AND created<? AND (?='' OR state=?) AND (?='' OR instr(id,?)>0 OR instr(email,?)>0 OR customer_id IN (SELECT id FROM customers WHERE instr(username,?)>0)) ORDER BY created DESC LIMIT 5000",(since,until,data.state,data.state,data.q,data.q,data.q,data.q)).fetchall()
        audit_log(c,'orders.export','orders',actor,request,details={'count':len(rows)})
    def safe(value):
        text=str(value or '')
        return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text
    def stamp(value):return datetime.fromtimestamp(value,CN_TZ).isoformat() if value else ''
    output=io.StringIO();writer=csv.writer(output);writer.writerow(['订单号','买家编号','商品','充值账号','数量','金额（元）','状态','下单时间','付款时间','完成时间','退款登记时间'])
    for row in rows:writer.writerow([safe(x) for x in (row['id'],row['customer_id'],row['title'],row['email'],row['quantity'],f"{row['price']/100:.2f}",row['state'],stamp(row['created']),stamp(row['paid_at']),stamp(row['completed_at']),stamp(row['refunded_at']))])
    return {'csv':'\ufeff'+output.getvalue(),'count':len(rows),'limit':5000}
