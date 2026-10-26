
const tabs=[...document.querySelectorAll('[role="tab"]')];
function showPanel(tab){tabs.forEach(t=>{const active=t===tab;t.setAttribute('aria-selected',String(active));document.getElementById(t.getAttribute('aria-controls')).hidden=!active});if(tab.id==='catalog-tab')document.getElementById('query').focus()}
tabs.forEach(tab=>tab.addEventListener('click',()=>showPanel(tab)));
const q=document.getElementById('query'), category=document.getElementById('category'), cards=[...document.querySelectorAll('.card')];
function filter(){const term=q.value.trim().toLocaleLowerCase();let visible=0;cards.forEach(card=>{const match=(!term||card.dataset.search.toLocaleLowerCase().includes(term))&&(!category.value||card.dataset.category===category.value);card.hidden=!match;if(match)visible++});document.getElementById('count').textContent=`显示${visible} / 50个站点 · 点击条目查看交付、收款、售后与研究判断`;document.getElementById('empty').hidden=visible!==0}
q.addEventListener('input',filter);category.addEventListener('change',filter);
tabs.forEach((tab,index)=>tab.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();showPanel(tabs[1-index]);tabs[1-index].focus()}}));
