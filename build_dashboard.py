#!/usr/bin/env python3
"""Read-only export of observed CS2 buys to one static, mobile-friendly HTML page.
No secrets or Telegram data are read or exported.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parent
RE_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,180}$")
RE_WALLET = re.compile(r"^0x[a-fA-F0-9]{40}$")


def fetch_data(db_path: Path, cache_path: Path, maximum: int = 30000) -> dict:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = {x[0] for x in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'trades' not in tables:
            raise RuntimeError("Brak tabeli trades w bazie")
        has_info = 'trade_market_info' in tables
        meta = {}
        if 'meta' in tables:
            meta = {str(r[0]):str(r[1]) for r in con.execute("SELECT key,value FROM meta")}
        if has_info:
            q = '''SELECT t.timestamp, t.condition_id, t.wallet, t.outcome, t.usd, t.price,
                         COALESCE(i.title,'') as title, COALESCE(i.market_title,'') as market_title,
                         COALESCE(i.slug,'') as slug, COALESCE(i.tier,'unknown') as tier
                   FROM trades t LEFT JOIN trade_market_info i ON i.condition_id=t.condition_id
                   ORDER BY t.timestamp DESC, t.id DESC LIMIT ?'''
        else:
            q = '''SELECT timestamp,condition_id,wallet,outcome,usd,price,
                         '' AS title,'' AS market_title,'' AS slug,'unknown' AS tier
                   FROM trades ORDER BY timestamp DESC,id DESC LIMIT ?'''
        raw = [dict(r) for r in con.execute(q, (maximum,))]
    finally:
        con.close()
    cached = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding='utf-8'))
            for item in cache.get('markets', []):
                if isinstance(item,dict) and item.get('condition_id'):
                    cached[str(item['condition_id']).lower()] = item
        except (ValueError, OSError):
            pass
    entries = []
    for r in raw:
        cond = str(r['condition_id'] or '').lower()
        backup = cached.get(cond, {})
        match = str(r['title'] or backup.get('title') or '')[:180]
        market = str(r['market_title'] or backup.get('market_title') or '')[:180]
        tier = str(r['tier'] or '')
        if tier not in ('lower','major'):
            tier = str(backup.get('tier') or 'unknown')
        if tier not in ('lower','major','unknown'):
            tier = 'unknown'
        slug = str(r['slug'] or backup.get('event_slug') or '')
        if not RE_SLUG.fullmatch(slug): slug=''
        wallet = str(r['wallet'] or '').lower()
        if not RE_WALLET.fullmatch(wallet): wallet=''
        try:
            ts=int(r['timestamp']); amount=float(r['usd']); price=float(r['price'])
        except (TypeError,ValueError):
            continue
        if amount < 0 or not 0 < price <= 1: continue
        entries.append({
            't': ts, 'm': match or 'Nieznany mecz (brak metadanych)',
            'q': market, 'o': str(r['outcome'] or '?')[:100],
            'a': round(amount, 2), 'p': round(price,6),
            'w': wallet, 'c': cond, 's': slug, 'tier': tier
        })
    generated = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return {'generated':generated,'lastScan':meta.get('last_successful_poll',''), 'trades':entries,
            'notice': 'Tylko zaobserwowane zakupy brutto z publicznego API (minimum 100 USD); nie jest to aktualna pozycja ani dowód manipulacji.'}


STYLE = r'''<style>
:root{color-scheme:dark;--bg:#080d18;--panel:#121b2c;--line:#28374c;--text:#eaf2ff;--muted:#93a5bd;--accent:#64c8fa;--gold:#f6cd74}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(140deg,#101d33,#090f1c 42%,#070b13);font:14px/1.5 system-ui,-apple-system,Segoe UI,Arial;color:var(--text);min-height:100vh}
a{color:var(--accent)}button,input,select{font:inherit}.container{max-width:1420px;margin:0 auto;padding:26px 20px 70px}
header{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;padding-bottom:22px}
.brand{display:flex;gap:13px;align-items:center}.brand .logo{display:grid;place-items:center;width:49px;height:49px;background:#132b3d;border:1px solid #286080;border-radius:15px;font-size:26px}.brand h1{font-size:23px;line-height:1.2;letter-spacing:-.5px;margin:0}.sub{color:var(--muted);font-size:12px}.live{display:inline-flex;align-items:center;gap:9px;border:1px solid #235d57;border-radius:99px;background:#112d2b;color:#96ead7;padding:7px 13px;font-size:12px;font-weight:650}.dot{height:8px;width:8px;border-radius:50%;background:#31d7a3}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:15px 0 19px}.stat{padding:17px 18px;border-radius:14px;background:rgba(20,31,49,.95);border:1px solid var(--line)}.stat .val{font-size:27px;font-weight:750;letter-spacing:-.5px}.stat .label{color:var(--muted);font-size:12px}
.panel{background:rgba(16,26,44,.94);border:1px solid var(--line);border-radius:16px;overflow:hidden}.panel-top{padding:16px;border-bottom:1px solid var(--line)}.toolbar{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.toolbar input,.toolbar select{background:#0d1524;border:1px solid #354963;border-radius:9px;padding:10px 12px;color:var(--text);min-height:42px}.toolbar input[type=search]{flex:1 1 240px;min-width:180px}.toolbar select{flex:0 1 150px}.toolbar button,.tab{border:1px solid #39536d;background:#162841;color:var(--text);padding:10px 13px;border-radius:9px;cursor:pointer;min-height:42px}.toolbar button:hover,.tab:hover{background:#1e3855}.tabs{display:flex;gap:7px;flex-wrap:wrap;padding:0 16px 14px}.tab.active{background:#164766;border-color:#398bb4;color:#bdeafe}.row-meta{color:var(--muted);padding:0 16px 10px;font-size:12px}
.table-wrap{overflow:auto}table{width:100%;min-width:830px;border-collapse:collapse}th,td{text-align:left;padding:13px 14px;border-top:1px solid #223249}th{font-size:11px;color:#adc0d7;letter-spacing:.04em;text-transform:uppercase;background:#101e32;white-space:nowrap}tbody tr:hover{background:#142840}td{max-width:340px}td:nth-child(2){font-weight:600}.secondary{display:block;color:var(--muted);font-size:11px;margin-top:3px}.money{font-variant-numeric:tabular-nums;font-weight:700;color:#b1f6db;white-space:nowrap}.tag{font-size:10px;border:1px solid #4c5565;padding:3px 7px;border-radius:99px;color:#c6d6e8;white-space:nowrap}.tag.lower{color:#ffd7a3;border-color:#82643f;background:#382a1c}.tag.major{color:#bdd8ff;border-color:#4e6792}.buttonlink{display:inline-block;padding:5px 9px;border-radius:8px;text-decoration:none;background:#17354b;white-space:nowrap;font-size:12px}.ellipsis{overflow-wrap:anywhere}.empty{text-align:center;padding:40px 12px;color:var(--muted)}.bottom{display:flex;justify-content:center;padding:14px}footer{font-size:12px;color:var(--muted);max-width:1080px;margin:20px auto 0}footer p{margin:6px 0}.note{background:#12253c;border-left:3px solid #5ba6d1;border-radius:5px;padding:12px 16px;margin-bottom:18px;color:#cbd9eb}.status{font-variant-numeric:tabular-nums}
@media(max-width:680px){.container{padding:16px 10px 40px}.brand h1{font-size:19px}.stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.stat{padding:13px}.stat .val{font-size:22px}.panel-top{padding:12px}.tabs{padding:0 12px 12px}.toolbar select{flex:1 1 90px}.toolbar input[type=search]{flex-basis:100%}.note{font-size:12px}}
</style>'''
SCRIPT = r'''<script>
(()=>{'use strict';
const records=window.__RADAR_DATA__.trades||[];
const $=s=>document.querySelector(s);
const fmt=n=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(n);
const date=t=>new Intl.DateTimeFormat('pl-PL',{dateStyle:'short',timeStyle:'short',timeZone:'Europe/Warsaw'}).format(t*1000);
const short=s=>s?s.slice(0,7)+'…'+s.slice(-5):'—';
const tiertext=t=>t==='lower'?'Niższy tier':t==='major'?'Duży turniej':'Nieustalony';
let view='trades',visible=100, current=[];
function el(tag,cls,txt){let e=document.createElement(tag);if(cls)e.className=cls;if(txt!==undefined)e.textContent=String(txt);return e}
function url(slug){return slug?'https://polymarket.com/event/'+encodeURIComponent(slug):null}
function anchor(text,url,external=true){if(!url)return el('span','secondary','Brak linku');const a=el('a','buttonlink',text);a.href=url;if(external){a.target='_blank';a.rel='noopener noreferrer'}return a}
function td(row,val,cls=''){const cell=el('td',cls);if(val instanceof Node)cell.append(val);else cell.textContent=val;row.append(cell);return cell}
function trrade(r){const row=document.createElement('tr');td(row,date(r.t),'status');const title=el('div','',r.m);const st=el('span','secondary',r.q||'');const wrap=el('div');wrap.append(title,st);td(row,wrap);td(row,r.o);td(row,fmt(r.a),'money');td(row,(1/r.p).toFixed(2)+'  ('+(r.p*100).toFixed(1)+'%)');const tag=el('span','tag '+r.tier,tiertext(r.tier));td(row,tag);const w=r.w?anchor(short(r.w),'https://polygonscan.com/address/'+r.w):el('span','secondary','—');td(row,w);td(row,anchor('Mecz ↗',url(r.s)));return row}
function aggregate(group){const d=new Map();for(const r of current){let key=group==='wallet'?[r.c,r.o,r.w].join('|'):r.c+'|'+r.o;if(group==='wallet'&&!r.w)continue;let item=d.get(key);if(!item){item={...r,total:0,count:0,last:0};d.set(key,item)}item.total+=r.a;item.count++;item.last=Math.max(item.last,r.t)}return [...d.values()].sort((a,b)=>b.total-a.total)}
function aggregatedRow(r,withWallet){const row=document.createElement('tr');td(row,date(r.last));td(row,r.m);td(row,r.o);td(row,fmt(r.total),'money');td(row,String(r.count));td(row,el('span','tag '+r.tier,tiertext(r.tier)));if(withWallet)td(row,anchor(short(r.w),'https://polygonscan.com/address/'+r.w));td(row,anchor('Mecz ↗',url(r.s)));return row}
function filtering(){const s=$('#search').value.trim().toLowerCase(),tier=$('#tier').value,min=Number($('#minimum').value||0),days=Number($('#days').value),from=days?Date.now()/1000-days*86400:0;
current=records.filter(r=>(tier==='all'||r.tier===tier)&&r.a>=min&&r.t>=from&&(!s||[r.m,r.q,r.o,r.w,r.c].join(' ').toLowerCase().includes(s)));if(view==='trades')current.sort((a,b)=>b.t-a.t);render()}
function headers(titles){const row=el('tr');for(const name of titles){const th=el('th','',name);row.append(th)}return row}
function render(){const table=$('#records');table.replaceChildren();const head=el('thead'),body=el('tbody');let rows;
if(view==='trades'){head.append(headers(['Czas PL','Mecz / rynek','Obstawiony wynik','Zakup','Kurs / cena','Tier','Portfel','Link']));rows=current;}
else {const isWallet=view==='wallet';head.append(headers(isWallet?['Ostatni zakup','Mecz','Wynik','Zakupy brutto','Transakcje','Tier','Portfel','Link']:['Ostatni zakup','Mecz','Wynik','Zakupy brutto','Transakcje','Tier','Link']));rows=aggregate(isWallet?'wallet':'market')}
for(const r of rows.slice(0,visible))body.append(view==='trades'?trrade(r):aggregatedRow(r,view==='wallet'));
table.append(head,body);$('#count').textContent=`${rows.length} pozycji • wyświetlono ${Math.min(visible,rows.length)}`;$('#more').hidden=rows.length<=visible;$('#empty').hidden=rows.length>0;}
function exportCsv(){let rows,heads;if(view==='trades'){heads=['timestamp_PL','mecz','rynek','wynik','kwota_USD','cena_udzialu','kurs_przyblizony','tier','portfel','link'];rows=current.map(r=>[date(r.t),r.m,r.q,r.o,r.a,r.p,(1/r.p).toFixed(3),r.tier,r.w,url(r.s)||''])}
else {heads=['ostatni_zakup_PL','mecz','wynik','laczne_zakupy_USD','liczba_transakcji','tier','portfel','link'];rows=aggregate(view==='wallet'?'wallet':'market').map(r=>[date(r.last),r.m,r.o,r.total.toFixed(2),r.count,r.tier,view==='wallet'?r.w:'',url(r.s)||''])}
const safe=x=>{let v=String(x??'');if(/^[=+@-]/.test(v.trimStart()))v="'"+v;return '"'+v.replaceAll('"','""')+'"'};
const csv='\ufeff'+[heads,...rows].map(line=>line.map(safe).join(';')).join('\r\n');const blob=new Blob([csv],{type:'text/csv;charset=utf-8'}),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=`cs2-radar-${view}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)}
const total=records.reduce((a,r)=>a+r.a,0);$('#stat-count').textContent=records.length.toLocaleString('pl-PL');$('#stat-volume').textContent=fmt(total);$('#stat-wallets').textContent=new Set(records.map(r=>r.w).filter(Boolean)).size.toLocaleString('pl-PL');$('#stat-markets').textContent=new Set(records.map(r=>r.c)).size.toLocaleString('pl-PL');
const scan=Number(window.__RADAR_DATA__.lastScan);$('#scantime').textContent=scan?date(scan):'brak danych';$('#updated').textContent=new Date(window.__RADAR_DATA__.generated).toLocaleString('pl-PL',{timeZone:'Europe/Warsaw'});
for(const id of ['search','tier','minimum','days'])$('#'+id).addEventListener(id==='search'?'input':'change',()=>{visible=100;filtering()});
for(const b of document.querySelectorAll('[data-view]'))b.addEventListener('click',()=>{view=b.dataset.view;visible=100;for(const t of document.querySelectorAll('[data-view]'))t.classList.toggle('active',t===b);filtering()});
$('#more').addEventListener('click',()=>{visible+=100;render()});$('#csv').addEventListener('click',exportCsv);filtering();
})();</script>'''


def build_page(data:dict)->str:
    raw_json = json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return '''<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#0c1524"><title>CS2 Radar · Historia transakcji</title>''' + STYLE + '''</head><body><div class="container">
<header><div class="brand"><div class="logo">◎</div><div><h1>CS2 Polymarket Radar</h1><div class="sub">Historia zaobserwowanych transakcji · Telegram alerts</div></div></div><div class="live"><span class="dot"></span>Ostatni skan: <span id="scantime"></span></div></header>
<div class="stats"><div class="stat"><div id="stat-count" class="val">—</div><div class="label">Zapisane transakcje</div></div><div class="stat"><div id="stat-volume" class="val">—</div><div class="label">Zakupy brutto w bazie</div></div><div class="stat"><div id="stat-wallets" class="val">—</div><div class="label">Zidentyfikowane portfele</div></div><div class="stat"><div id="stat-markets" class="val">—</div><div class="label">Rynki CS2</div></div></div>
<div class="note">Wykaz obejmuje wyłącznie transakcje, które radar zdążył zaobserwować i zapisać (zwykle od 100 USD). Suma zakupów nie jest wartością aktualnej pozycji; sprzedaże nie są odejmowane. Niższy tier jest klasyfikacją przybliżoną. Duży zakład nie świadczy sam w sobie o ustawieniu meczu.</div>
<section class="panel"><div class="panel-top"><div class="toolbar"><input id="search" type="search" placeholder="Szukaj meczu, drużyny, portfela…" aria-label="Wyszukaj transakcję"><select id="tier" aria-label="Tier"><option value="all">Wszystkie tiery</option><option value="lower">Niższy tier</option><option value="unknown">Nieustalony tier</option><option value="major">Duży turniej</option></select><select id="minimum" aria-label="Minimalna kwota"><option value="0">Od 0 USD</option><option value="100">Od 100 USD</option><option value="250">Od 250 USD</option><option value="500">Od 500 USD</option><option value="1000">Od 1 000 USD</option><option value="2500">Od 2 500 USD</option></select><select id="days" aria-label="Zakres czasu"><option value="0">Cała baza</option><option value="1">24 godziny</option><option value="7">7 dni</option><option value="30">30 dni</option></select><button type="button" id="csv">↓ Pobierz CSV</button></div></div>
<div class="tabs"><button class="tab active" data-view="trades">Transakcje</button><button class="tab" data-view="wallet">Zakupy portfeli</button><button class="tab" data-view="market">Wyniki i rynki</button></div><div id="count" class="row-meta"></div><div class="table-wrap"><table id="records"></table></div><div id="empty" class="empty" hidden>Brak transakcji spełniających wybrane filtry.</div><div class="bottom"><button class="tab" id="more" type="button" hidden>Pokaż kolejne 100</button></div></section>
<footer><p>Dane przygotowano: <span id="updated"></span> czasu polskiego. Strona jest statyczna; aktualizuje się po kolejnym opublikowaniu przez GitHub Actions, a nie w czasie rzeczywistym.</p><p>Portfele prowadzą do Polygonscan, a mecze do Polymarket. Starsze wpisy mogą nie mieć opisów wydarzeń, jeśli pierwotna baza ich nie zapisała. Strona publiczna — nie dodawaj tokenów ani prywatnych danych.</p></footer></div>
<script>window.__RADAR_DATA__=''' + raw_json + ''';</script>''' + SCRIPT + '''</body></html>'''


def main()->None:
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',type=Path,default=ROOT/'radar.sqlite3')
    parser.add_argument('--cache',type=Path,default=ROOT/'market_cache.json')
    parser.add_argument('--out',type=Path,default=ROOT/'public'/'index.html')
    args=parser.parse_args()
    data=fetch_data(args.db,args.cache)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(build_page(data),encoding='utf-8')
    print(f"Panel wygenerowany: {args.out}; {len(data['trades'])} transakcji")

if __name__=='__main__':main()
