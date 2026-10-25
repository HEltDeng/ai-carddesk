"""Read-only public deployment smoke test. No credentials or transactions."""
import json
import urllib.request
import urllib.error
BASE='https://ai.mychangliapp.cn'
for path in ['/', '/admin', '/service', '/api/health', '/api/catalog', '/api/admin/state']:
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
