"""Read-only public deployment smoke test. No credentials or transactions."""
import json
import urllib.request
import urllib.error
BASE='https://ai.mychangliapp.cn'
for path in ['/', '/admin', '/service', '/api/health', '/api/catalog', '/api/admin/state', '/api/account']:
    with urllib.request.urlopen(BASE+path,timeout=15) as r:
        assert r.status==200
        assert r.headers['X-Content-Type-Options']=='nosniff'
        assert 'frame-ancestors' in r.headers['Content-Security-Policy']
        if path.startswith('/api') or path=='/admin':assert r.headers['Cache-Control']=='no-store'
        if path=='/api/catalog':
            data=json.load(r)
            assert not data['payment_enabled'] and not data['fulfillment_enabled']
    print('PASS',path)
try:
    urllib.request.urlopen(BASE+'/api/admin/dashboard',timeout=15)
    raise AssertionError('Admin data exposed')
except urllib.error.HTTPError as e:
    assert e.code==401
print('PASS admin protection')
with urllib.request.urlopen(urllib.request.Request(BASE+'/',method='HEAD'),timeout=15) as r:assert r.status==200
print('PASS HEAD')

for path in ['/api/admin/customers','/api/admin/audit','/api/admin/summary','/api/orders/detail?order_id=nonexistent']:
    try:
        urllib.request.urlopen(BASE+path,timeout=15)
        raise AssertionError('Protected API exposed')
    except urllib.error.HTTPError as e:assert e.code==401
    print('PASS protected',path)
body=json.dumps({'product_id':'plus-1','session':'synthetic-test-only','email':'fixture@example.test','consent':True,'idempotency_key':'public-smoke-order-key-12345'}).encode()
try:
    urllib.request.urlopen(urllib.request.Request(BASE+'/api/orders',data=body,headers={'Origin':BASE,'Content-Type':'application/json'}),timeout=15)
    raise AssertionError('Guest purchase accepted')
except urllib.error.HTTPError as e:assert e.code==401
print('PASS guest purchase blocked')
