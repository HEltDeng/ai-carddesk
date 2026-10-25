import importlib
import json
import hmac
import hashlib
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).parents[1]))

@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setenv('CARDDESK_DATA',str(tmp_path))
    monkeypatch.setenv('CARDDESK_ORIGIN','https://card.example')
    import app
    m=importlib.reload(app)
    c=TestClient(m.app,base_url='https://card.example',headers={'origin':'https://card.example'})
    c.get('/api/session')
    return m,c

def session():
    return json.dumps({'user':{'email':'fixture@example.test'},'accessToken':'test-only-token-never-a-real-account','expires':datetime.fromtimestamp(time.time()+3600,timezone.utc).isoformat()})

def configure(m):
    with m.db() as c:
        m.savecfg(c,'provider_url','https://provider.example')
        m.savecfg(c,'provider_key',m.encrypt('synthetic-provider-key'))
        m.savecfg(c,'payment_url','https://payments.example/pay')
        m.savecfg(c,'payment_secret',m.encrypt('synthetic-callback-key'))

def order(c,**kw):
    data={'product_id':'plus-1','session':session(),'email':'fixture@example.test','idempotency_key':'a-unique-test-order-key-12345','consent':True};data.update(kw)
    return c.post('/api/orders',json=data)

def admin(m,c):
    return c.post('/api/admin/login',json={'username':'test-admin','password':'A-long-test-password-123','setup_code':(m.DATA/'setup-code').read_text()})

def callback(m,c,oid,**kw):
    d={'order_id':oid,'amount_cents':11990,'transaction_id':'txn-123','status':'paid'};d.update(kw)
    raw=json.dumps(d).encode();ts=str(int(time.time()));sig=hmac.new(b'synthetic-callback-key',ts.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    return c.post('/api/payment/callback',content=raw,headers={'content-type':'application/json','x-timestamp':ts,'x-signature':sig})

def test_unconfigured_never_accepts_credentials(env):
    m,c=env
    assert c.get('/api/catalog').json()['payment_enabled'] is False
    assert order(c).status_code==409
    with m.db() as db:assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0

def test_origin_and_admin_boundaries(env):
    m,c=env
    assert c.get('/api/admin/dashboard').status_code==401
    assert c.post('/api/admin/login',json={},headers={'origin':'https://evil.example'}).status_code==403
    assert c.post('/api/admin/settings',json={}).status_code==401
    h=c.get('/api/session').headers
    assert 'frame-ancestors' in h['content-security-policy']
    raw=TestClient(m.app,base_url='https://card.example').get('/api/session').headers['set-cookie']
    assert 'HttpOnly' in raw and 'Secure' in raw and '__Host-carddesk' in raw and 'SameSite=strict' in raw

def test_one_use_admin_setup(env):
    m,c=env
    assert admin(m,c).status_code==200
    assert not (m.DATA/'setup-code').exists()
    assert c.get('/api/admin/dashboard').status_code==200
    c.post('/api/admin/logout',json={})
    assert c.get('/api/admin/dashboard').status_code==401
    assert c.post('/api/admin/login',json={'username':'test-admin','password':'Wrong-test-password-123'}).status_code==401

def test_session_validation_and_no_reflection(env):
    m,c=env;configure(m)
    assert order(c,session='invalid secret-content').status_code==400
    r=order(c,session={'secret':'never-reflect-this'})
    assert r.status_code==422 and 'never-reflect-this' not in r.text
    assert order(c,email='someone-else@example.test').status_code==400
    assert order(c,consent=False).status_code==400

def test_order_idempotency_price_and_encryption(env):
    m,c=env;configure(m)
    r=order(c).json();again=order(c).json()
    assert r['order']['id']==again['order']['id']
    assert r['order']['price']==11990
    assert 'accessToken' not in json.dumps(r) and 'test-only-token' not in json.dumps(r)
    with m.db() as db:
        row=db.execute('SELECT * FROM orders').fetchone()
        assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1
        assert 'test-only-token' not in row['credential']
        assert m.decrypt(row['credential'])=='test-only-token-never-a-real-account'
    assert 'fixture' not in r['payment_url'] and 'token' not in r['payment_url']

def test_lookup_requires_order_capability(env):
    m,c=env;configure(m);r=order(c).json()
    assert c.post('/api/orders/lookup',json={'order_id':r['order']['id'],'lookup_key':'invalid-key-12345'}).status_code==404
    assert c.post('/api/orders/lookup',json={'order_id':r['order']['id'],'lookup_key':r['lookup_key']}).status_code==200
    stranger=TestClient(m.app,base_url='https://card.example');stranger.get('/api/session')
    assert stranger.get('/api/orders').json()['orders']==[]

def test_card_cannot_be_spent_twice_concurrently(env):
    m,c=env;configure(m);admin(m,c)
    code=c.post('/api/admin/cards',json={'product_id':'plus-1','count':1}).json()['codes'][0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda n:order(c,card=code,idempotency_key=f'concurrent-order-key-1234-{n}').status_code,[1,2]))
    assert sorted(results)==[200,409]
    with m.db() as db:
        assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==1
        assert code not in str([tuple(x) for x in db.execute('SELECT * FROM cards')])

def test_callback_signatures_amount_and_idempotency(env):
    m,c=env;configure(m);r=order(c).json();oid=r['order']['id']
    assert c.post('/api/payment/callback',json={}).status_code==403
    assert callback(m,c,oid,amount_cents=1).status_code==409
    assert callback(m,c,oid).status_code==200
    assert callback(m,c,oid).status_code==200
    assert callback(m,c,oid,transaction_id='different-txn').status_code==409
    with m.db() as db:
        assert db.execute('SELECT state FROM orders').fetchone()[0]=='queued'
        assert db.execute("SELECT COUNT(*) FROM events WHERE state='queued'").fetchone()[0]==1

def test_payment_transaction_cannot_pay_two_orders(env):
    m,c=env;configure(m)
    a=order(c).json()['order']['id'];b=order(c,idempotency_key='another-unique-order-key-1234').json()['order']['id']
    assert callback(m,c,a).status_code==200
    assert callback(m,c,b).status_code==409

def test_late_payment_requires_fresh_session(env):
    m,c=env;configure(m);r=order(c).json();oid=r['order']['id']
    with m.db() as db:db.execute("UPDATE orders SET state='expired',credential=NULL,credential_expiry=0 WHERE id=?",(oid,))
    assert callback(m,c,oid).status_code==200
    assert c.post('/api/orders/session',json={'order_id':oid,'lookup_key':r['lookup_key'],'session':session()}).status_code==200
    with m.db() as db:assert db.execute('SELECT state FROM orders').fetchone()[0]=='queued'

def test_provider_success_requires_receipt_and_reference(env,monkeypatch):
    m,c=env;configure(m);r=order(c).json();oid=r['order']['id'];callback(m,c,oid)
    import worker;worker=importlib.reload(worker)
    monkeypatch.setattr(worker,'provider_request',lambda method,url,key,payload=None:{'reference':oid,'status':'completed','id':'task-1'})
    worker.tick()
    with m.db() as db:
        row=db.execute('SELECT * FROM orders').fetchone();assert row['state']=='processing' and row['credential'] is None
    worker.apply_result(oid,{'reference':'wrong','status':'completed','id':'task-1','receipt':'receipt-123'})
    worker.apply_result(oid,{'reference':oid,'status':'completed','id':'task-1','receipt':'receipt-123'})
    with m.db() as db:assert db.execute('SELECT state FROM orders').fetchone()[0]=='completed'

def test_worker_does_not_resubmit_after_timeout(env,monkeypatch):
    m,c=env;configure(m);r=order(c).json();callback(m,c,r['order']['id'])
    import worker;worker=importlib.reload(worker);calls=[]
    def timeout(method,*args,**kwargs):calls.append(method);raise TimeoutError()
    monkeypatch.setattr(worker,'provider_request',timeout)
    worker.tick();worker.tick()
    assert calls==['POST','GET']
    with m.db() as db:assert db.execute('SELECT credential FROM orders').fetchone()[0] is None

def test_admin_cannot_mark_unpaid_order_completed(env):
    m,c=env;configure(m);r=order(c).json();admin(m,c)
    assert c.post('/api/admin/order-status',json={'order_id':r['order']['id'],'state':'completed','evidence':'fake-completion'}).status_code==409

def test_upstream_addresses_and_stored_secrets(env):
    m,c=env;admin(m,c)
    assert c.post('/api/admin/settings',json={'provider_url':'https://localhost'}).status_code==400
    assert c.post('/api/admin/settings',json={'payment_url':'javascript:alert(1)'}).status_code==400
    r=c.post('/api/admin/settings',json={'payment_secret':'secret-value-123','provider_key':'provider-key-456'})
    assert r.status_code==200
    public=c.get('/api/admin/dashboard').text
    assert 'secret-value-123' not in public and 'provider-key-456' not in public

def test_order_rate_limit(env):
    m,c=env
    for n in range(10):order(c)
    assert order(c).status_code==429

def test_catalog_has_brand_tiers_and_no_three_month_product(env):
    m,c=env
    products=c.get('/api/catalog').json()['products']
    assert {p['category'] for p in products}=={'chatgpt','claude','grok','codex'}
    assert all(p['months']==1 for p in products)
    assert not any(p['id']=='plus-3' for p in products)
    assert all(p['price'] and p['price']>0 for p in products)
    assert next(p for p in products if p['id']=='codex-2000')['price']==59500
    assert next(p for p in products if p['id']=='claude-max-5x')['price']==66700
    # Startup does not restore a retired SKU or overwrite operator prices.
    with m.db() as db:db.execute("UPDATE products SET price=15900 WHERE id='plus-1'")
    importlib.reload(m)
    products=c.get('/api/catalog').json()['products']
    assert next(p for p in products if p['id']=='plus-1')['price']==15900

def test_new_tier_needs_price_and_correct_brand_adapter(env):
    m,c=env;configure(m)
    assert order(c,product_id='codex-250').status_code==409
    assert order(c,product_id='claude-pro').status_code==409
    assert order(c,product_id='grok-super').status_code==409
    assert order(c,product_id='plus-3').status_code==409
    with m.db() as db:assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0

def test_admin_can_price_tier_but_not_repurpose_issued_card(env):
    m,c=env;configure(m);admin(m,c)
    product={'id':'gpt-pro-5x','title':'ChatGPT Pro 5x','category':'chatgpt','tier':'Pro 5x','price':69900,'active':True}
    assert c.post('/api/admin/products',json=product).status_code==200
    assert order(c,product_id=product['id']).json()['order']['price']==69900
    product['category']='claude'
    assert c.post('/api/admin/products',json=product).status_code==409
    product['category']='chatgpt';product['price']=None
    assert c.post('/api/admin/products',json=product).status_code==200
    assert next(p for p in c.get('/api/catalog').json()['products'] if p['id']==product['id'])['price'] is None


def new_ticket(c,**kw):
    d={'topic':'购买咨询','subject':'测试商品购买问题','body':'这是测试环境的购买咨询详情，不包含真实账号。','idempotency_key':'ticket-create-key-1234567890'};d.update(kw)
    return c.post('/api/tickets',json=d)

def stranger(m):
    c=TestClient(m.app,base_url='https://card.example',headers={'origin':'https://card.example'})
    c.get('/api/session');return c

def test_ticket_ownership_lookup_and_safe_content(env):
    m,c=env;r=new_ticket(c).json();tid=r['ticket']['id'];other=stranger(m)
    assert other.get('/api/tickets').json()['tickets']==[]
    assert other.post('/api/tickets/read',json={'ticket_id':tid}).status_code==404
    assert other.post('/api/tickets/read',json={'ticket_id':tid,'lookup_key':r['lookup_key']}).status_code==200
    assert c.post('/api/tickets/read',json={'ticket_id':tid}).status_code==200
    assert new_ticket(c,body='{"accessToken":"sensitive-never-store-this-value"}',idempotency_key='different-sensitive-ticket-1234').status_code==400
    with m.db() as db:assert 'sensitive-never-store' not in str([tuple(x) for x in db.execute('SELECT * FROM ticket_messages')])

def test_ticket_idempotency_and_open_limit(env):
    m,c=env;a=new_ticket(c).json();b=new_ticket(c).json();assert a==b
    assert new_ticket(c,idempotency_key='second-ticket-key-1234567890').status_code==200
    assert new_ticket(c,idempotency_key='third-ticket-key-1234567890').status_code==200
    assert new_ticket(c,idempotency_key='fourth-ticket-key-1234567890').status_code==429
    with m.db() as db:assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]==3

def test_ticket_cannot_attach_stranger_order(env):
    m,c=env;configure(m);r=order(c).json();other=stranger(m)
    assert new_ticket(other,order_id=r['order']['id']).status_code==404
    assert new_ticket(other,order_id=r['order']['id'],order_key=r['lookup_key']).status_code==200

def test_ticket_honeypot_and_cross_origin(env):
    m,c=env
    assert new_ticket(c,website='bot.example').status_code==400
    assert c.post('/api/tickets',json={},headers={'origin':'https://evil.example'}).status_code==403
    with m.db() as db:assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]==0

def test_ticket_reply_closed_and_reopen(env):
    m,c=env;r=new_ticket(c).json();key={'ticket_id':r['ticket']['id'],'lookup_key':r['lookup_key']}
    reply={**key,'body':'补充测试信息','idempotency_key':'reply-key-123456789012345'}
    assert c.post('/api/tickets/reply',json=reply).status_code==200
    assert c.post('/api/tickets/reply',json=reply).status_code==200
    assert c.post('/api/tickets/state',json={**key,'state':'closed'}).status_code==200
    assert c.post('/api/tickets/reply',json={**reply,'idempotency_key':'another-reply-123456789012'}).status_code==409
    assert c.post('/api/tickets/state',json={**key,'state':'open'}).status_code==200
    assert len(c.post('/api/tickets/read',json=key).json()['messages'])==2

def test_staff_permissions_reply_and_revocation(env):
    m,c=env;t=new_ticket(c).json();admin(m,c)
    assert c.post('/api/admin/staff',json={'username':'operator1','password':'Operator-test-password-123'}).status_code==200
    op=stranger(m)
    assert op.post('/api/admin/login',json={'username':'operator1','password':'Operator-test-password-123'}).status_code==200
    dash=op.get('/api/admin/dashboard').json();assert dash['role']=='staff' and dash['settings']=={} and dash['cards']==[]
    assert op.post('/api/admin/settings',json={}).status_code==401
    assert op.post('/api/admin/products',json={'id':'x1','title':'test','category':'chatgpt','tier':'Plus'}).status_code==401
    assert op.post('/api/admin/staff',json={'username':'newstaff','password':'Operator-test-password-123'}).status_code==401
    assert op.post('/api/admin/tickets/reply',json={'ticket_id':t['ticket']['id'],'body':'测试客服回复内容','idempotency_key':'staff-reply-test-key-12345'}).status_code==200
    assert c.post('/api/admin/staff',json={'username':'operator1','active':False}).status_code==200
    assert op.get('/api/admin/dashboard').status_code==401

def test_staff_claim_prevents_duplicate_handling(env):
    m,c=env;t=new_ticket(c).json()['ticket']['id'];admin(m,c)
    ops=[]
    for username in ('operator1','operator2'):
        c.post('/api/admin/staff',json={'username':username,'password':'Operator-test-password-123'})
        op=stranger(m);assert op.post('/api/admin/login',json={'username':username,'password':'Operator-test-password-123'}).status_code==200;ops.append(op)
    assert ops[0].post('/api/admin/tickets/bulk',json={'ids':[t],'action':'claim'}).json()['results'][0]['ok']
    assert not ops[1].post('/api/admin/tickets/bulk',json={'ids':[t],'action':'claim'}).json()['results'][0]['ok']
    assert ops[1].post('/api/admin/tickets/reply',json={'ticket_id':t,'body':'争抢回复测试','idempotency_key':'staff-reply-test-key-12345'}).status_code==409

def test_unpaid_caps_atomic_and_no_stock_reserved(env):
    m,c=env;configure(m);admin(m,c)
    c.post('/api/admin/cards',json={'product_id':'plus-1','count':2})
    with ThreadPoolExecutor(max_workers=5) as pool:
        codes=list(pool.map(lambda i:order(c,idempotency_key=f'unpaid-cap-concurrent-key-{i}').status_code,range(5)))
    assert codes.count(200)==3 and codes.count(429)==2
    with m.db() as db:
        assert db.execute("SELECT COUNT(*) FROM orders WHERE state='pending_payment'").fetchone()[0]==3
        assert db.execute("SELECT COUNT(*) FROM cards WHERE state='ready'").fetchone()[0]==2
        assert db.execute("SELECT COUNT(*) FROM orders WHERE state='queued'").fetchone()[0]==0

def test_rotating_session_cannot_bypass_ip_pending_cap(env):
    m,c=env;configure(m)
    for i in range(8):assert order(stranger(m),idempotency_key=f'rotating-session-test-key-{i}').status_code==200
    assert order(stranger(m),idempotency_key='rotating-session-test-key-9').status_code==429

def test_unpaid_ttl_and_late_real_payment(env):
    m,c=env;configure(m);r=order(c).json();oid=r['order']['id']
    with m.db() as db:db.execute('UPDATE orders SET created=?',(int(time.time())-901,))
    import worker;worker=importlib.reload(worker);worker.tick()
    with m.db() as db:
        row=db.execute('SELECT * FROM orders').fetchone();assert row['state']=='expired' and row['credential'] is None
    assert callback(m,c,oid).status_code==200
    with m.db() as db:assert db.execute('SELECT state FROM orders').fetchone()[0]=='needs_session'

def test_bulk_close_skips_paid_orders_and_audits(env):
    m,c=env;configure(m);a=order(c).json();b=order(c,idempotency_key='second-order-bulk-close-123').json();callback(m,c,b['order']['id']);admin(m,c)
    r=c.post('/api/admin/orders/bulk',json={'ids':[a['order']['id'],b['order']['id']],'action':'close_unpaid'}).json()['results']
    assert r[0]['ok'] and not r[1]['ok']
    assert callback(m,c,a['order']['id'],transaction_id='late-real-transaction').status_code==200
    with m.db() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE action='bulk.close_unpaid' AND actor='admin'").fetchone()[0]==1
        assert db.execute('SELECT state FROM orders WHERE id=?',(b['order']['id'],)).fetchone()[0]=='queued'

def test_bulk_results_require_fresh_state_and_evidence(env):
    m,c=env;configure(m);r=order(c).json();oid=r['order']['id'];callback(m,c,oid);admin(m,c)
    item={'order_id':oid,'expected_state':'processing','state':'completed','evidence':'receipt-12345'}
    result=c.post('/api/admin/orders/results',json={'items':[item]}).json()['results'][0];assert not result['ok']
    with m.db() as db:db.execute("UPDATE orders SET state='processing' WHERE id=?",(oid,))
    assert c.post('/api/admin/orders/results',json={'items':[item]}).json()['results'][0]['ok']
    assert not c.post('/api/admin/orders/results',json={'items':[item]}).json()['results'][0]['ok']

def test_review_holds_worker_dispatch(env,monkeypatch):
    m,c=env;configure(m);r=order(c).json();oid=r['order']['id'];callback(m,c,oid);admin(m,c)
    c.post('/api/admin/orders/bulk',json={'ids':[oid],'action':'hold'})
    import worker;worker=importlib.reload(worker);calls=[]
    monkeypatch.setattr(worker,'provider_request',lambda *args,**kw:calls.append(args))
    worker.tick();assert calls==[]
    c.post('/api/admin/orders/bulk',json={'ids':[oid],'action':'release'})
    worker.tick();assert len(calls)==1

def test_source_email_cannot_lock_other_customers(env):
    m,c=env;configure(m)
    # Same email is deliberately not a global lock: it is unverified client input.
    assert order(c).status_code==200
    assert order(stranger(m)).status_code==200

def test_idempotency_rejects_different_product(env):
    m,c=env;configure(m);assert order(c).status_code==200
    assert order(c,product_id='gpt-pro-5x').status_code==409

def test_staff_cannot_access_admin_single_order_status(env):
    m,c=env;admin(m,c);c.post('/api/admin/staff',json={'username':'operator1','password':'Operator-test-password-123'})
    op=stranger(m);op.post('/api/admin/login',json={'username':'operator1','password':'Operator-test-password-123'})
    assert op.post('/api/admin/order-status',json={'order_id':'not-real','state':'refunded','evidence':'fake-refund'}).status_code==401


def test_paginated_staff_search_reaches_older_tickets(env):
    m,c=env;admin(m,c)
    with m.db() as db:
        for i in range(205):
            db.execute('INSERT INTO tickets(id,owner,lookup_hash,source_hash,topic,subject,created,updated) VALUES(?,?,?,?,?,?,?,?)',(f'TK-FIXTURE-{i:04d}','fixture-owner','fixture-lookup','fixture-source','购买咨询','分页测试',i,i))
    first=c.get('/api/admin/tickets?page=1').json();last=c.get('/api/admin/tickets?page=3').json()
    assert first['total']==205 and len(first['tickets'])==100
    assert len(last['tickets'])==5 and last['tickets'][-1]['id']=='TK-FIXTURE-0000'
    assert c.get('/api/admin/tickets?q=%27%20OR%201%3D1--').json()['tickets']==[]
    assert c.get('/api/admin/tickets?page=0').status_code==400
    assert c.get('/api/admin/orders?q=%27%20OR%201%3D1--').json()['orders']==[]
