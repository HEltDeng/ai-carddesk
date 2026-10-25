"""Single worker; idempotent provider protocol, no blind POST retry."""
import ipaddress
import socket
import time
from urllib.parse import urlparse, quote
import httpx
from app import db, cfg, decrypt, event, expire_unpaid

def public_endpoint(url):
    host=urlparse(url).hostname
    if not host:raise ValueError('No host')
    for info in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(info[4][0]).is_global:raise ValueError('Private destination')

def provider_request(method,url,key,payload=None):
    public_endpoint(url)
    with httpx.Client(timeout=15,follow_redirects=False,trust_env=False) as client:
        with client.stream(method,url,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},json=payload) as response:
            response.raise_for_status()
            raw=b''
            for part in response.iter_bytes():
                raw+=part
                if len(raw)>32768:raise ValueError('Response too large')
            import json
            return json.loads(raw)

def tick():
    now=int(time.time())
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        expire_unpaid(c,now)
        expired=c.execute("SELECT id,state FROM orders WHERE credential IS NOT NULL AND credential_expiry<=?",(now,)).fetchall()
        for r in expired:
            state='needs_session' if r['state']=='queued' else ('expired' if r['state']=='pending_payment' else r['state'])
            c.execute('UPDATE orders SET credential=NULL,state=?,updated=? WHERE id=?',(state,now,r['id']))
            if state!=r['state']:event(c,r['id'],state)
        url=cfg(c,'provider_url');key=decrypt(cfg(c,'provider_key'))
        if not url or not key:return
        row=c.execute("SELECT o.*,p.months,p.category,p.tier FROM orders o JOIN products p ON p.id=o.product_id WHERE o.state='queued' AND o.review=0 AND o.credential IS NOT NULL ORDER BY o.created LIMIT 1").fetchone()
        if row:
            c.execute("UPDATE orders SET state='processing',updated=? WHERE id=?",(now,row['id']));event(c,row['id'],'processing')
        pending=[dict(r) for r in c.execute("SELECT * FROM orders WHERE state='processing' ORDER BY updated LIMIT 10")]
    if row:
        try:
            payload={'reference':row['id'],'product':row['product_id'],'category':row['category'],'tier':row['tier'],'months':row['months'],'account':{'email':row['email'],'access_token':decrypt(row['credential'])}}
            answer=provider_request('POST',url+'/orders',key,payload)
            apply_result(row['id'],answer)
        except Exception:
            # Ambiguous network outcome remains processing. Recover by our stable
            # reference on the next GET, never create another upstream charge.
            pass
        finally:
            payload=None
            with db() as c:c.execute('UPDATE orders SET credential=NULL WHERE id=?',(row['id'],))
    for r in pending:
        if row and r['id']==row['id']:continue
        try:apply_result(r['id'],provider_request('GET',url+'/orders/by-reference/'+quote(r['id'],safe=''),key))
        except Exception:pass

def apply_result(oid,answer):
    if not isinstance(answer,dict) or answer.get('reference')!=oid:return
    state=answer.get('status')
    if state not in ('processing','completed','failed'):return
    proof=answer.get('receipt','')
    if state=='completed' and (not isinstance(proof,str) or len(proof)<4):return
    ref=answer.get('id','')
    if not isinstance(ref,str) or not 1<=len(ref)<=100:return
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT state FROM orders WHERE id=?',(oid,)).fetchone()
        if not r or r['state']!='processing':return
        c.execute('UPDATE orders SET state=?,provider_ref=?,evidence=?,credential=NULL,updated=? WHERE id=?',(state,ref,str(proof)[:200],int(time.time()),oid))
        if state!=r['state']:event(c,oid,state)

if __name__=='__main__':
    while True:
        try:tick()
        except Exception:pass
        time.sleep(15)
