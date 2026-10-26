"""Authorization, single-use email verification, reporting and sensitive-data boundaries."""
import json
import secrets
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_flows import env, configure, session, admin, callback, order, seed_customer, stranger

PASSWORD='Customer-test-password-123'
ADMIN_PASSWORD='A-long-test-password-123'

def guest(m):
    c=TestClient(m.app,base_url='https://card.example',headers={'origin':'https://card.example'})
    c.get('/api/session');return c

def payload(key=None):
    return {'product_id':'plus-1','session':session(),'email':'fixture@example.test','consent':True,'idempotency_key':key or secrets.token_hex(16)}

def mail(m,monkeypatch):
    sent=[]
    with m.db() as db:
        for k,v in {'host':'smtp.example.test','port':'465','username':'sender','password':m.encrypt('mail-test-secret'),'sender':'sender@example.test'}.items():m.savecfg(db,'smtp_'+k,v)
    monkeypatch.setattr(m,'send_email_code',lambda config,email,code,purpose:sent.append((email,code,purpose)))
    return sent

def send(c,purpose='purchase',email=''):
    return c.post('/api/account/email/send',json={'purpose':purpose,'email':email})

def verify(c,challenge,code,**kwargs):
    return c.post('/api/account/email/verify',json={'challenge_id':challenge,'code':code,**kwargs})

def test_guest_and_unverified_cannot_create_or_redeem(env):
    m,c=env;configure(m);g=guest(m)
    assert g.post('/api/orders',json=payload()).status_code==401
    assert g.post('/api/cards/check',json={'card':'XY-FAKE'}).status_code==401
    assert g.post('/api/account/register',json={'username':'new-buyer','password':PASSWORD}).status_code==200
    assert g.post('/api/orders',json=payload()).status_code==403
    assert not g.get('/api/account').json()['user']['email_verified']
    with m.db() as db:assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0

def test_registration_password_hash_and_cookie_separation(env):
    m,c=env;g=guest(m)
    r=g.post('/api/account/register',json={'username':'new-buyer','password':PASSWORD})
    assert r.status_code==200 and 'HttpOnly' in r.headers['set-cookie'] and 'Secure' in r.headers['set-cookie']
    assert m.USER_COOKIE in r.headers['set-cookie']
    assert g.get('/api/admin/dashboard').status_code==401
    with m.db() as db:
        u=db.execute("SELECT * FROM customers WHERE username='new-buyer'").fetchone()
        assert PASSWORD not in u['password'] and m.valid_password(PASSWORD,u['password'])
    assert PASSWORD not in g.get('/api/account').text
    assert g.post('/api/account/register',json={'username':'NEW-BUYER','password':PASSWORD}).status_code==409

def test_email_required_and_one_use_purchase_grant(env,monkeypatch):
    m,c=env;configure(m);sent=mail(m,monkeypatch)
    assert c.post('/api/orders',json=payload()).status_code==403
    challenge=send(c).json()['challenge_id'];assert sent
    assert verify(c,challenge,sent[-1][1]).status_code==200
    body=payload();a=c.post('/api/orders',json=body);assert a.status_code==200
    assert c.post('/api/orders',json=body).json()['order']['id']==a.json()['order']['id']
    assert c.post('/api/orders',json=payload()).status_code==403
    assert verify(c,challenge,sent[-1][1]).status_code==400

def test_binding_sets_unique_email_and_enables_first_purchase(env,monkeypatch):
    m,c=env;sent=mail(m,monkeypatch);g=guest(m)
    g.post('/api/account/register',json={'username':'new-buyer','password':PASSWORD})
    challenge=send(g,'bind','NewBuyer@Example.test').json()['challenge_id']
    assert verify(g,challenge,sent[-1][1]).status_code==200
    state=g.get('/api/account').json();assert state['user']['email']=='newbuyer@example.test' and state['purchase_verified']
    assert 'password' not in json.dumps(state)
    with m.db() as db:
        challenge_row=db.execute('SELECT * FROM email_challenges WHERE id=?',(challenge,)).fetchone()
        assert challenge_row['code_hash']!=sent[-1][1]


def test_otp_attempts_persist_and_expiry_is_enforced(env,monkeypatch):
    m,c=env;sent=mail(m,monkeypatch);challenge=send(c).json()['challenge_id'];code=sent[-1][1];wrong='000000' if code!='000000' else '111111'
    for _ in range(5):assert verify(c,challenge,wrong).status_code==400
    assert verify(c,challenge,code).status_code==400
    with m.db() as db:
        assert db.execute('SELECT attempts FROM email_challenges WHERE id=?',(challenge,)).fetchone()[0]==5
        db.execute('UPDATE email_challenges SET attempts=0,expires=? WHERE id=?',(int(time.time())-1,challenge))
    assert verify(c,challenge,code).status_code==400


def test_otp_is_bound_to_browser_and_one_order_under_race(env,monkeypatch):
    m,c=env;configure(m);sent=mail(m,monkeypatch);challenge=send(c).json()['challenge_id']
    other=guest(m)
    assert verify(other,challenge,sent[-1][1]).status_code==400
    assert verify(c,challenge,sent[-1][1]).status_code==200
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:c.post('/api/orders',json=payload()).status_code,range(2)))
    assert sorted(results)==[200,403]


def test_email_cooldown_and_delivery_failure_never_grant(env,monkeypatch):
    m,c=env;mail(m,monkeypatch)
    def broken(*args):raise RuntimeError('PRIVATE_SMTP_FAILURE')
    monkeypatch.setattr(m,'send_email_code',broken)
    r=send(c);assert r.status_code==503 and 'PRIVATE_SMTP_FAILURE' not in r.text
    assert send(c).status_code==429
    assert not c.get('/api/account').json()['purchase_verified']
    with m.db() as db:assert db.execute('SELECT delivered FROM email_challenges').fetchone()[0]==0


def test_missing_smtp_fails_closed(env):
    m,c=env;assert send(c).status_code==503
    assert not c.get('/api/account').json()['email_available']
    assert c.post('/api/orders',json=payload()).status_code==403


def test_account_quota_survives_new_login(env):
    m,c=env;configure(m)
    for _ in range(3):assert order(c,idempotency_key=secrets.token_hex(16)).status_code==200
    u=c.get('/api/account').json()['user'];second=guest(m)
    assert second.post('/api/account/login',json={'username':u['username'],'password':PASSWORD}).status_code==200
    assert order(second,idempotency_key=secrets.token_hex(16)).status_code==429
    assert len(second.get('/api/orders').json()['orders'])==3


def test_disabled_account_revoke_sessions_and_codes(env,monkeypatch):
    m,c=env;sent=mail(m,monkeypatch);challenge=send(c).json()['challenge_id'];u=c.get('/api/account').json()['user'];admin(m,c)
    assert c.post('/api/admin/customers',json={'user_id':u['id'],'active':False}).status_code==200
    assert c.get('/api/account').json()['user'] is None
    assert verify(c,challenge,sent[-1][1]).status_code==400
    assert c.post('/api/account/login',json={'username':u['username'],'password':PASSWORD}).status_code==401


def test_reset_password_only_by_valid_email_code_revokes_every_session(env,monkeypatch):
    m,c=env;sent=mail(m,monkeypatch);u=c.get('/api/account').json()['user'];g=guest(m)
    challenge=send(g,'reset',u['email']).json()['challenge_id'];new='Updated-customer-password-456'
    assert verify(g,challenge,sent[-1][1],new_password=new).status_code==200
    assert c.get('/api/account').json()['user'] is None
    assert g.post('/api/account/login',json={'username':u['username'],'password':PASSWORD}).status_code==401
    assert g.post('/api/account/login',json={'username':u['username'],'password':new}).status_code==200
    assert verify(g,challenge,sent[-1][1],new_password='Another-new-password-123').status_code==400


def test_unknown_reset_address_is_generic(env,monkeypatch):
    m,c=env;sent=mail(m,monkeypatch);g=guest(m)
    r=send(g,'reset','unknown@example.test');assert r.status_code==200 and not sent
    assert verify(g,r.json()['challenge_id'],'123456',new_password=PASSWORD).status_code==400


def test_admin_customer_password_reset_needs_reauthentication(env):
    m,c=env;u=c.get('/api/account').json()['user'];admin(m,c)
    body={'user_id':u['id'],'active':True,'password':'Changed-test-password-123','admin_password':'incorrect'}
    assert c.post('/api/admin/customers',json=body).status_code==403
    body['admin_password']=ADMIN_PASSWORD
    assert c.post('/api/admin/customers',json=body).status_code==200
    assert c.get('/api/account').json()['user'] is None
    assert 'Changed-test-password' not in c.get('/api/admin/customers').text


def test_delivery_encryption_reauth_owner_and_audit(env):
    m,c=env;configure(m);r=order(c).json();oid=r['order']['id'];callback(m,c,oid);admin(m,c)
    body={'order_id':oid,'account':'delivered@example.test','password':'Private-delivery-secret-123','reason':'核对测试交付信息','admin_password':ADMIN_PASSWORD}
    assert c.post('/api/admin/delivery',json=body).status_code==200
    with m.db() as db:
        row=db.execute('SELECT * FROM deliveries').fetchone();assert body['password'] not in row['secret'] and body['account'] not in row['account']
    assert c.post('/api/admin/delivery/read',json={**body,'admin_password':'incorrect'}).status_code==403
    assert c.post('/api/admin/delivery/read',json=body).json()['password']==body['password']
    assert c.post('/api/orders/delivery',json={'order_id':oid}).status_code==404
    with m.db() as db:db.execute("UPDATE orders SET state='completed' WHERE id=?",(oid,))
    assert c.post('/api/orders/delivery',json={'order_id':oid}).json()['account']==body['account']
    other=stranger(m);assert other.post('/api/orders/delivery',json={'order_id':oid}).status_code==404
    for endpoint in ('/api/admin/orders','/api/admin/dashboard','/api/admin/audit'):
        text=c.get(endpoint).text;assert body['password'] not in text and body['account'] not in text
    exported=c.post('/api/admin/orders/export',json={}).text;assert body['password'] not in exported and body['account'] not in exported
    assert 'delivery.reveal' in c.get('/api/admin/audit').text


def test_unpaid_order_rejects_delivery(env):
    m,c=env;configure(m);oid=order(c).json()['order']['id'];admin(m,c)
    assert c.post('/api/admin/delivery',json={'order_id':oid,'account':'test','password':'test','reason':'测试未付款交付','admin_password':ADMIN_PASSWORD}).status_code==409


def test_staff_cannot_access_customers_mail_audit_or_secrets(env):
    m,c=env;admin(m,c);c.post('/api/admin/staff',json={'username':'operator1','password':'Operator-test-password-123'})
    op=guest(m);op.post('/api/admin/login',json={'username':'operator1','password':'Operator-test-password-123'})
    for path in ('/api/admin/customers','/api/admin/audit'):assert op.get(path).status_code==401
    assert op.post('/api/admin/audit/verify',json={}).status_code==401
    assert op.post('/api/admin/delivery/read',json={'order_id':'fake','reason':'查看测试信息'}).status_code==401
    assert op.get('/api/admin/summary').status_code==200


def test_audit_is_append_only_and_detects_forgery(env):
    m,c=env;admin(m,c)
    with m.db() as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE audit SET actor='evil'")
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM audit')
    assert c.post('/api/admin/audit/verify',json={}).json()['valid']
    with m.db() as db:db.execute("INSERT INTO audit(action,target,created) VALUES('forged','fake',1)")
    assert not c.post('/api/admin/audit/verify',json={}).json()['valid']


def test_summary_counts_cash_refunds_and_redemptions_separately(env):
    m,c=env;configure(m);a=order(c,idempotency_key=secrets.token_hex(16)).json();b=order(c,idempotency_key=secrets.token_hex(16)).json();callback(m,c,a['order']['id']);admin(m,c)
    code=c.post('/api/admin/cards',json={'product_id':'plus-1','count':1}).json()['codes'][0];order(c,card=code,idempotency_key=secrets.token_hex(16))
    stats=c.get('/api/admin/summary').json();assert stats['orders']==3 and stats['quantity']==3 and stats['paid_orders']==1 and stats['redeemed_orders']==1
    assert stats['paid_cents']==11990 and stats['net_cents']==11990 and stats['pending_orders']==1
    with m.db() as db:db.execute("UPDATE orders SET state='completed' WHERE id=?",(a['order']['id'],))
    c.post('/api/admin/order-status',json={'order_id':a['order']['id'],'state':'refunded','evidence':'refund-fixture-123'})
    stats=c.get('/api/admin/summary').json();assert stats['paid_cents']==11990 and stats['refunded_cents']==11990 and stats['net_cents']==0
    assert c.get('/api/admin/summary?start=2026-10-27&end=2026-10-26').status_code==400
    assert c.get('/api/admin/summary?start=2000-01-01&end=2000-01-01').json()['paid_cents']==0


def test_audit_filters_pagination_and_csv_formula_escape(env):
    m,c=env;configure(m);r=order(c).json();admin(m,c)
    with m.db() as db:
        db.execute("UPDATE orders SET title='=HYPERLINK(test)' WHERE id=?",(r['order']['id'],))
        for _ in range(60):m.audit_log(db,'fixture.event','test','admin')
    first=c.get('/api/admin/audit?action=fixture.event').json();second=c.get('/api/admin/audit?action=fixture.event&page=2').json()
    assert first['total']==60 and len(first['entries'])==50 and len(second['entries'])==10
    assert c.get('/api/admin/audit?q=%27%20OR%201%3D1--').json()['entries']==[]
    exported=c.post('/api/admin/orders/export',json={}).json()['csv'];assert "'=HYPERLINK(test)" in exported
    assert c.get('/api/admin/audit?action=orders.export').json()['total']==1


def test_smtp_secret_is_encrypted_and_not_echoed(env):
    m,c=env;admin(m,c)
    body={'host':'smtp.example.test','port':465,'username':'sender','password':'SMTP-private-test','sender':'sender@example.test'}
    assert c.post('/api/admin/email-settings',json=body).status_code==200
    assert 'SMTP-private-test' not in c.get('/api/admin/dashboard').text
    assert 'SMTP-private-test' not in c.get('/api/admin/audit').text
    with m.db() as db:assert m.cfg(db,'smtp_password')!='SMTP-private-test'
    assert c.post('/api/admin/email-settings',json={**body,'port':25}).status_code==400


def test_reload_migration_preserves_existing_rows_and_audit(env):
    import importlib
    m,c=env;configure(m);order(c);admin(m,c)
    m=importlib.reload(m)
    assert c.get('/api/admin/summary').json()['orders']==1
    assert c.post('/api/admin/audit/verify',json={}).json()['valid']


def test_slow_mail_cannot_reactivate_revoked_challenge(env,monkeypatch):
    m,c=env;mail(m,monkeypatch);codes=[]
    def revoke_during_send(config,email,code,purpose):
        codes.append(code)
        with m.db() as db:db.execute('UPDATE email_challenges SET consumed=1')
    monkeypatch.setattr(m,'send_email_code',revoke_during_send)
    challenge=send(c).json()['challenge_id']
    assert verify(c,challenge,codes[0]).status_code==400


def test_authenticated_order_detail_resumes_payment_and_checks_ownership(env):
    m,c=env;configure(m);r=order(c).json();oid=r['order']['id']
    detail=c.get('/api/orders/detail',params={'order_id':oid}).json()
    assert detail['lookup_key']==r['lookup_key'] and detail['payment_url']==r['payment_url']
    assert detail['events'][0]['state']=='pending_payment'
    assert stranger(m).get('/api/orders/detail',params={'order_id':oid}).status_code==404


def test_expired_purchase_grant_does_not_create_order(env):
    m,c=env;configure(m)
    with m.db() as db:
        row=db.execute('SELECT * FROM customer_sessions WHERE token=?',(m.digest(c.cookies.get(m.USER_COOKIE)),)).fetchone()
        db.execute('INSERT INTO checkout_grants VALUES(?,?,?)',(row['token'],row['user_id'],int(time.time())-1))
    assert c.post('/api/orders',json=payload()).status_code==403


def test_order_export_uses_buyer_filter(env):
    m,c=env;configure(m);order(c);u=c.get('/api/account').json()['user'];admin(m,c)
    assert c.get('/api/admin/orders',params={'q':u['username']}).json()['total']==1
    assert c.post('/api/admin/orders/export',json={'q':u['username']}).json()['count']==1
