"""Render the local research notes. Standard library only; no remote assets."""
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
rows = json.loads((ROOT/'sites-50.json').read_text())

def inline(s):
    s = html.escape(s)
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', s)
    s = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    return s

def markdown(text):
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith('#'):
            level = len(line) - len(line.lstrip('#'))
            title = line[level:].strip()
            if level > 1:
                out.append(f'<h{level}>{inline(title)}</h{level}>')
            i += 1
        elif line.startswith('|'):
            table = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = [x.strip() for x in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?', c) for c in cells):
                    table.append(cells)
                i += 1
            out.append('<div class="tablewrap"><table><thead><tr>'+''.join('<th>'+inline(c)+'</th>' for c in table[0])+'</tr></thead><tbody>')
            for row in table[1:]:
                out.append('<tr>'+''.join('<td>'+inline(c)+'</td>' for c in row)+'</tr>')
            out.append('</tbody></table></div>')
        elif re.match(r'(- |\d+\. )', line):
            ordered = bool(re.match(r'\d+\. ', line))
            tag = 'ol' if ordered else 'ul'
            out.append('<'+tag+'>')
            while i < len(lines) and re.match(r'(- |\d+\. )', lines[i].strip()):
                content = re.sub(r'^(- |\d+\. )', '', lines[i].strip())
                out.append('<li>'+inline(content)+'</li>')
                i += 1
            out.append('</'+tag+'>')
        else:
            paragraph = []
            while i < len(lines) and lines[i].strip() and not re.match(r'^(#|\||- |\d+\. )',lines[i].strip()):
                paragraph.append(lines[i].strip())
                i += 1
            out.append('<p>'+inline(' '.join(paragraph))+'</p>')
    return '\n'.join(out)

cards = []
for row in rows:
    search = html.escape(' '.join(str(v) for v in row.values()), quote=True)
    category = html.escape(row['category'], quote=True)
    cards.append(f'''<details class="card" data-search="{search}" data-category="{category}">
      <summary><span class="num">{row['id']:02d}</span><span class="entry"><b>{html.escape(row['name'])}</b><small>{html.escape(row['category'])} · {html.escape(row['evidence'])}</small></span><span class="price">{html.escape(row['price'])}</span><span class="chevron">＋</span></summary>
      <div class="cardbody"><a class="source" href="{html.escape(row['url'])}" target="_blank" rel="noopener noreferrer">打开原站 ↗</a>
      <dl>{''.join('<dt>'+label+'</dt><dd>'+html.escape(row[key])+'</dd>' for key,label in [('delivery','交付流程'),('payment','收款方式'),('terms','售后与约束'),('lesson','研究判断')])}</dl><p class="muted">核验：{row['checked_on']} · 公开自述，未实单验证</p></div></details>''')

body = markdown((ROOT/'AI代充站调研报告.md').read_text())
categories = ''.join(f'<option>{html.escape(x)}</option>' for x in sorted({r['category'] for r in rows}))
template = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI代充站 · 50站研究与行动建议</title>
<style>
:root{color-scheme:light;--ink:#152526;--muted:#596a68;--paper:#f3f6f2;--line:#d9e2db;--accent:#11634e}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.8 system-ui,-apple-system,"PingFang SC",sans-serif}a{color:#086c54;text-decoration-thickness:1px;text-underline-offset:3px}a:hover{color:#013e2e}header{background:#153d33;color:#fff;padding:42px max(24px,calc((100vw - 1180px)/2)) 28px}header .kicker{font-size:12px;letter-spacing:2px;color:#bed9c8}h1{font-size:clamp(27px,4vw,43px);font-weight:650;line-height:1.3;letter-spacing:-1px;margin:12px 0}header p{max-width:850px;color:#d4e3da;margin:12px 0 20px}.stats{display:flex;gap:12px;flex-wrap:wrap}.stat{background:#ffffff0c;border:1px solid #ffffff25;border-radius:8px;padding:10px 17px;font-size:13px}.stat b{font-size:23px;margin-right:8px;font-weight:600}.wrap{max-width:1228px;margin:auto;padding:0 24px 60px}.tabs{display:flex;gap:8px;padding:20px 0;position:sticky;top:0;background:var(--paper);z-index:2;border-bottom:1px solid var(--line)}button,input,select{font:inherit}button{border:1px solid #b4c7bc;border-radius:7px;background:white;padding:9px 18px;color:var(--ink);cursor:pointer}button[aria-selected="true"]{background:var(--accent);border-color:var(--accent);color:white}button:focus-visible,a:focus-visible,input:focus-visible,summary:focus-visible,select:focus-visible{outline:3px solid #d79832;outline-offset:3px}.panel{padding-top:24px}.panel[hidden]{display:none}.lead{background:white;border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:8px;padding:18px 22px;margin-bottom:24px}.lead p{margin:0}.muted{color:var(--muted);font-size:13px}.filters{display:flex;gap:12px;margin:20px 0 12px}input,select{border:1px solid #bccfc3;border-radius:7px;background:white;padding:10px 14px;min-height:45px}input{flex:1;min-width:100px}label{font-size:13px;font-weight:600;display:block}.filter-main{flex:1}.filter-main input{width:100%}.filter-type{width:185px}.filter-type select{width:100%}#count{color:var(--muted);margin:6px 0 18px;font-size:13px}.card{background:white;border:1px solid var(--line);border-radius:9px;margin-bottom:11px;overflow:hidden}.card[hidden]{display:none}.card summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:36px minmax(190px,1fr) 1.45fr 24px;gap:14px;align-items:center;padding:17px 20px}.card summary::-webkit-details-marker{display:none}.num{color:#738a7e;font:13px/1.5 ui-monospace,monospace}.entry b{font-size:17px}.entry small{display:block;color:var(--muted);font-size:12px;margin-top:2px}.price{font-size:14px;line-height:1.55;color:#354d43}.chevron{font-size:22px;color:#5f7c6e}.card[open] .chevron{transform:rotate(45deg)}.cardbody{border-top:1px solid var(--line);padding:18px 25px 20px 70px}.source{font-size:13px}dl{display:grid;grid-template-columns:100px 1fr;gap:10px 16px;font-size:14px;margin:18px 0}dt{color:var(--muted)}dd{margin:0}article{max-width:1100px;margin:auto}article>p,article>ul,article>ol{max-width:950px}article p{margin:16px 0}article h2{font-size:24px;font-weight:650;line-height:1.45;margin:44px 0 18px;padding-top:8px;border-top:1px solid var(--line)}article h2:first-of-type{margin-top:28px}article li{margin:9px 0}code{background:#e6ede6;border-radius:4px;padding:2px 5px;font-size:.9em;overflow-wrap:anywhere}.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;margin:20px 0;background:white}table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.7}th,td{text-align:left;vertical-align:top;padding:13px 15px;border-bottom:1px solid var(--line)}th{background:#e5eee7;color:#244a3c;font-weight:650}tr:last-child td{border-bottom:0}th:first-child,td:first-child{min-width:140px}td{min-width:220px}footer{margin-top:40px;border-top:1px solid var(--line);padding-top:20px;color:var(--muted);font-size:13px}.downloads{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}.flow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:18px 0}.flow span{background:#e4eee6;border-radius:6px;padding:8px 12px;font-size:13px}.flow i{font-style:normal;color:#788c7f}.warning{color:#775923}#empty{padding:25px;background:white;border:1px solid var(--line);border-radius:8px}@media(max-width:700px){header{padding:28px 20px 24px}.wrap{padding:0 15px 40px}.stats{gap:7px}.stat{padding:7px 10px}.stat b{font-size:20px}.card summary{grid-template-columns:27px 1fr 20px;padding:15px;gap:8px}.price{grid-column:2;grid-row:2}.chevron{grid-column:3;grid-row:1 / 3}.cardbody{padding:16px}dl{grid-template-columns:80px 1fr;gap:9px;font-size:14px}.filters{gap:9px}.filter-type{width:130px}.tabs button{padding:8px 12px;font-size:14px}article h2{font-size:21px}th,td{padding:10px}header p{font-size:14px}}@media print{.tabs,.filters,.downloads{display:none}header{background:white;color:#152526;padding:10px}header p,header .kicker{color:#596a68}.stats{display:none}.panel[hidden]{display:block}details .cardbody{display:block!important}.card{break-inside:avoid}.wrap{max-width:none}.panel{padding:0}body{background:white;font-size:11pt}.tablewrap{overflow:visible}table{font-size:9pt}td{min-width:0}}
</style></head><body><header><div class="kicker">商业模式研究 · 2026.10.22</div><h1>AI 代充站，真正要解决什么</h1><p>从50个原站样本、GitHub与X经营线索，拆解供货、收款、交付和售后。现成商城可用，渠道资格和可持续利润仍需验证。</p><div class="stats"><div class="stat"><b>50</b>原站样本</div><div class="stat"><b>7</b>GitHub候选</div><div class="stat"><b>0</b>实际交易</div><div class="stat">公开信息核验 · 非供应商背书</div></div></header>
<main class="wrap"><nav class="tabs" role="tablist" aria-label="报告视图"><button role="tab" id="report-tab" aria-controls="report" aria-selected="true">结论与行动建议</button><button role="tab" id="catalog-tab" aria-controls="catalog" aria-selected="false">50站清单与来源</button></nav>
<section id="report" class="panel" role="tabpanel" aria-labelledby="report-tab"><div class="lead"><p><strong>最接近目标的公开实例：</strong>Aileader 的链动店铺、自有 Dujiao-Next 商城和独立兑换站。同时，链动公开禁售目录存在相关限制，具体商品准入需单独确认。</p><div class="flow" aria-label="交易与履约的五个环节"><span>商品与资格</span><i>→</i><span>付款确认</span><i>→</i><span>唯一CDK交付</span><i>→</i><span>上游充值任务</span><i>→</i><span>到账与售后</span></div><p class="muted">发码成功、充值到账、30天履约是不同状态。页面销量、卡来源和保障均未做交易验证。</p></div><article>__BODY__</article></section>
<section id="catalog" class="panel" role="tabpanel" aria-labelledby="catalog-tab" hidden><div class="lead"><p><strong>50个站点样本，50份原站公开记录。</strong>这是流程与商业模式研究，不是50家已经证实成熟、合法供货或适合采购的商家。价格有时效，部分为起价或附条件商品；每条均记录限制和证据缺口。</p></div><div class="filters"><div class="filter-main"><label for="query">搜索名称、价格、渠道、退款或流程</label><input id="query" type="search" placeholder="例如：批发、Dujiao、原路退款、iOS" autocomplete="off"></div><div class="filter-type"><label for="category">业务类型</label><select id="category"><option value="">全部类型</option>__CATEGORIES__</select></div></div><p id="count" aria-live="polite">显示50 / 50个站点 · 点击条目查看交付、收款、售后与研究判断</p><div id="cards">__CARDS__</div><p id="empty" hidden>没有匹配项，请更换关键词或业务类型。</p></section>
<footer>材料保存在本地；本报告未购买服务、上传账号凭证、联系供应商或部署业务站。公开商家资料不等同授权、合同或实际履约证据。<div class="downloads"><a href="AI代充站调研报告.md">阅读报告原文</a><a href="50站点逐项调研.md">逐站文字清单</a><a href="sites-50.json">结构化公开资料</a></div></footer></main>
<script>
const tabs=[...document.querySelectorAll('[role="tab"]')];
function showPanel(tab){tabs.forEach(t=>{const active=t===tab;t.setAttribute('aria-selected',String(active));document.getElementById(t.getAttribute('aria-controls')).hidden=!active});if(tab.id==='catalog-tab')document.getElementById('query').focus()}
tabs.forEach(tab=>tab.addEventListener('click',()=>showPanel(tab)));
const q=document.getElementById('query'), category=document.getElementById('category'), cards=[...document.querySelectorAll('.card')];
function filter(){const term=q.value.trim().toLocaleLowerCase();let visible=0;cards.forEach(card=>{const match=(!term||card.dataset.search.toLocaleLowerCase().includes(term))&&(!category.value||card.dataset.category===category.value);card.hidden=!match;if(match)visible++});document.getElementById('count').textContent=`显示${visible} / 50个站点 · 点击条目查看交付、收款、售后与研究判断`;document.getElementById('empty').hidden=visible!==0}
q.addEventListener('input',filter);category.addEventListener('change',filter);
tabs.forEach((tab,index)=>tab.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();showPanel(tabs[1-index]);tabs[1-index].focus()}}));
</script></body></html>'''
(ROOT/'AI代充站调研.html').write_text(template.replace('__BODY__',body).replace('__CATEGORIES__',categories).replace('__CARDS__','\n'.join(cards)))
print(json.dumps({'sites':len(rows),'html_bytes':(ROOT/'AI代充站调研.html').stat().st_size},ensure_ascii=False))
