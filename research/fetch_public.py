import json, re, sys, urllib.request, urllib.parse, concurrent.futures
from pathlib import Path
from html.parser import HTMLParser

ROOT = Path(__file__).parent
class Page(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.links=[]; self.skip=0; self.current=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in ('script','style','noscript'): self.skip+=1
        if tag=='a': self.current=[a.get('href',''),'']
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'): self.skip=max(0,self.skip-1)
        if tag=='a' and self.current: self.links.append(self.current); self.current=None
    def handle_data(self,data):
        if not self.skip:
            t=data.strip()
            if t: self.parts.append(t)
            if self.current: self.current[1]+=t+' '

def fetch(url):
    out={'requested_url':url}
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (compatible; public research)'})
        with urllib.request.urlopen(req,timeout=25) as r:
            raw=r.read(2500000).decode('utf-8','replace'); out.update(url=r.url,status=r.status)
        p=Page(); p.feed(raw)
        out['text']='\n'.join(p.parts)
        out['links']=[[urllib.parse.urljoin(out['url'],u),t.strip()] for u,t in p.links]
        out['title']=re.findall(r'<title[^>]*>(.*?)</title>',raw,re.S)[:1]
    except Exception as e: out['error']=str(e)
    return out

if __name__=='__main__':
    urls=sys.argv[1:]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for out in ex.map(fetch,urls):
            key=re.sub(r'[^a-zA-Z0-9_-]+','_',out['requested_url']).strip('_')[:150]
            (ROOT/'evidence').mkdir(exist_ok=True)
            (ROOT/'evidence'/f'{key}.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
            print(json.dumps({k:out[k] for k in ['requested_url','url','status','title','error'] if k in out},ensure_ascii=False))
