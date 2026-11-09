#!/usr/bin/env python3
"""Statyczny panel CS2 Radar: mecze, dwie strony rynkow, historia zakupow.
Nie publikuje tokenow ani danych Telegrama. Dane pochodza wylacznie z bazy radaru.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parent
VALID_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,180}$")
VALID_WALLET = re.compile(r"^0x[a-fA-F0-9]{40}$")


def fetch_data(db_path: Path, cache_path: Path, maximum: int = 50000) -> dict:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'trades' not in tables:
            raise RuntimeError('Brakuje tabeli trades')
        cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
        side_sql = 'side' if 'side' in cols else "'BUY'"
        name_sql = 'trader_name' if 'trader_name' in cols else "''"
        raw = [dict(r) for r in con.execute(f'''SELECT id,timestamp,condition_id,wallet,outcome,usd,price,
                                  {side_sql} AS side,{name_sql} AS trader_name
                                  FROM trades ORDER BY timestamp DESC,id DESC LIMIT ?''', (maximum,))]
        meta = {}
        if 'meta' in tables:
            meta = {str(r[0]):str(r[1]) for r in con.execute("SELECT key,value FROM meta")}
        market_db = {}
        if 'trade_market_info' in tables:
            market_cols = {r[1] for r in con.execute("PRAGMA table_info(trade_market_info)")}
            outcomes_col = 'outcomes_json' if 'outcomes_json' in market_cols else "'[]'"
            eid_col = 'event_id' if 'event_id' in market_cols else "''"
            for r in con.execute(f'''SELECT condition_id,title,market_title,slug,tier,
                                     {outcomes_col} AS outcomes_json, {eid_col} AS event_id
                                     FROM trade_market_info'''):
                market_db[str(r['condition_id']).lower()] = dict(r)
    finally:
        con.close()
    cached = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding='utf-8'))
            for item in cache.get('markets', []):
                if isinstance(item,dict) and item.get('condition_id'):
                    cached[str(item['condition_id']).lower()] = item
        except (OSError,ValueError):
            pass
    market_ids = {str(r['condition_id']).lower() for r in raw}
    markets = {}
    for cid in market_ids | set(cached) | set(market_db):
        a = market_db.get(cid) or {}
        b = cached.get(cid) or {}
        title = str(a.get('title') or b.get('title') or '')[:200]
        question = str(a.get('market_title') or b.get('market_title') or title)[:220]
        tier = str(a.get('tier') or b.get('tier') or 'unknown')
        if tier not in ('lower','major','unknown'):
            tier = 'unknown'
        slug = str(a.get('slug') or b.get('event_slug') or '')
        if not VALID_SLUG.fullmatch(slug):
            slug = ''
        outcomes = []
        try:
            raw_outcomes = a.get('outcomes_json') or '[]'
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        except (ValueError,TypeError):
            pass
        if not isinstance(outcomes,list) or not outcomes:
            outcomes = b.get('outcomes') or []
        if isinstance(outcomes,str):
            try: outcomes = json.loads(outcomes)
            except ValueError: outcomes = []
        if not isinstance(outcomes,list):
            outcomes = []
        outcomes = list(dict.fromkeys(str(x)[:100] for x in outcomes if str(x).strip()))[:12]
        markets[cid] = {'c':cid,'m':title or 'Nieznany mecz (brak metadanych)',
                        'q':question,'s':slug,'tier':tier,'o':outcomes,
                        'e':str(a.get('event_id') or b.get('event_id') or '')[:50]}
    trades = []
    for r in raw:
        try:
            ts = int(r['timestamp']); usd = float(r['usd']); price = float(r['price'])
        except (ValueError,TypeError): continue
        if ts <= 0 or usd < 0 or not 0 < price <= 1: continue
        cid = str(r['condition_id']).lower()
        if cid not in markets: continue
        wallet = str(r['wallet'] or '').lower()
        if not VALID_WALLET.fullmatch(wallet): wallet = ''
        side = str(r.get('side') or 'BUY').upper()
        if side != 'BUY': continue
        trades.append({'id':str(r['id'])[:80],'t':ts,'c':cid,'w':wallet,
                       'o':str(r['outcome'] or '?')[:100], 'a':round(usd,2),
                       'p':round(price,6), 'n':str(r.get('trader_name') or '')[:70]})
    used = {x['c'] for x in trades}
    event_keys = {(markets[c]['s'] or markets[c]['m']).casefold() for c in used}
    # Dostepne pozostale rynki do wyboru w szczegolach meczu.
    view_markets = [m for m in markets.values()
                    if m['c'] in used or (m['s'] and m['s'].casefold() in event_keys)]
    view_markets.sort(key=lambda m:(m['m'],m['q']))
    return {
        'generated':datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'lastScan': meta.get('last_successful_poll',''),
        'minimum':100,
        'trades':trades,
        'markets':view_markets,
        'notice': 'Wylacznie zaobserwowane BUY od progu pobierania API; nie wszystkie zaklady, nie saldo otwartych pozycji. Poziom turnieju rozpoznawany heurystycznie.'
    }


STYLE = r'''<style>
:root{color-scheme:dark;--bg:#080d16;--surface:#111c2d;--panel:#131f33;--edge:#263750;--fg:#ecf3ff;--dim:#a0b1cb;--blue:#4bb5f7;--mint:#72e4c2;--gold:#eac382;--danger:#f7a8a8}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 85% 0%,#12314b 0,transparent 45%),linear-gradient(#0c1523,#080d16 65%);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Arial;min-height:100vh}
a{color:#90d5ff}button,input,select{font:inherit}button{cursor:pointer}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.shell{max-width:1440px;margin:0 auto;padding:26px 22px 70px}.brandbar{display:flex;justify-content:space-between;gap:18px;align-items:center;flex-wrap:wrap;margin-bottom:18px}.brand{display:flex;align-items:center;gap:13px}.logo{width:52px;height:52px;border:1px solid #39729e;border-radius:15px;display:grid;place-items:center;background:#123147;font-size:26px}.brand h1{font-size:24px;letter-spacing:-.65px;margin:0;line-height:1.2}.muted{color:var(--dim)}.small{font-size:12px}.status{font-size:12px;border:1px solid #356d68;padding:7px 12px;border-radius:99px;background:#10312f;color:#aff7dc}.bullet{display:inline-block;width:8px;height:8px;margin-right:6px;border-radius:50%;background:#5ce6b6}
.note{background:rgba(16,39,61,.8);border-left:3px solid #66abdf;border-radius:7px;padding:10px 15px;margin-bottom:18px;color:#d6e6f9;font-size:12px}.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px;margin-bottom:22px}.kpi{background:rgba(19,32,52,.92);border:1px solid var(--edge);border-radius:14px;padding:16px}.kpi .big{font-size:26px;font-weight:750;letter-spacing:-.6px}.kpi .hint{font-size:12px;color:var(--dim)}
.tabs{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}.tab{padding:11px 18px;background:transparent;color:var(--dim);border:1px solid var(--edge);border-radius:10px}.tab:hover,.tab.active{color:var(--fg);background:#193c58;border-color:#39799d}.tab.active{font-weight:650}.toolbar{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px;align-items:center}.toolbar input,.toolbar select{background:#101b2d;color:var(--fg);border:1px solid #344765;border-radius:10px;padding:10px 12px;height:43px}.toolbar input[type=search]{flex:1 1 240px;min-width:150px}.toolbar select{flex:0 1 160px}.toolbar button,.btn{border:1px solid #41617e;border-radius:10px;background:#19324b;color:var(--fg);padding:10px 14px;text-decoration:none;display:inline-flex;align-items:center;gap:7px}.toolbar button:hover,.btn:hover{background:#234968}
.panel{background:rgba(18,29,47,.94);border:1px solid var(--edge);border-radius:16px;overflow:hidden}.panel-title{padding:16px 18px;border-bottom:1px solid var(--edge);display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}.panel-title h2{font-size:17px;margin:0}.count{font-size:12px;color:var(--dim)}.matches{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.match-card{padding:17px;background:rgba(19,32,52,.96);border:1px solid var(--edge);border-radius:15px;text-align:left;color:var(--fg);transition:background .12s,border-color .12s}.match-card:hover{background:#192e46;border-color:#487ba0}.card-head{display:flex;gap:14px;justify-content:space-between;align-items:flex-start}.match-title{font-size:15px;font-weight:700;line-height:1.4}.kicker{color:var(--dim);font-size:11px;margin-top:5px}.pill{display:inline-block;font-size:11px;border-radius:99px;border:1px solid #5e6d7e;padding:3px 8px;white-space:nowrap}.pill.lower{border-color:#80633e;color:#f5d09c;background:#34271a}.pill.unknown{color:#c5d4e6}.pill.major{color:#bddbff;border-color:#476b9d}.card-total{font-size:19px;font-weight:730;white-space:nowrap}.side-row{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;margin-top:11px}.teamname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.money{font-variant-numeric:tabular-nums;font-weight:700;color:#94ebca}.bar{background:#2b3d51;border-radius:99px;height:7px;overflow:hidden;margin-top:6px}.bar>span{display:block;background:linear-gradient(90deg,#4d8ad1,#70cadd);height:100%}.bar.alt>span{background:linear-gradient(90deg,#bb8ac9,#e1b9d4)}.card-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:14px;color:var(--dim);font-size:12px}
.detail{margin-bottom:17px}.detail-head{padding:18px 20px;border-bottom:1px solid var(--edge);display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}.detail-head h2{margin:6px 0 2px;font-size:21px}.detail-meta{font-size:12px;color:var(--dim)}.detail-controls{padding:13px 18px;border-bottom:1px solid var(--edge);display:flex;gap:12px;align-items:center;flex-wrap:wrap}.detail-controls select{max-width:100%;flex:1;background:#0e192a;color:var(--fg);border:1px solid #3b516e;border-radius:9px;padding:9px;min-width:160px}.sides{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:16px}.sidebox{background:#102338;border:1px solid #35506d;border-radius:12px;padding:16px;min-width:0}.sidebox.alt{background:#261c32;border-color:#655077}.sidebox h3{font-size:17px;margin:0 0 10px;overflow-wrap:anywhere}.sidebox .large{font-size:30px;font-weight:750;letter-spacing:-.6px}.sidebox .foot{font-size:12px;color:var(--dim);margin-top:6px}.subsection{padding:0 16px 16px}.subsection h3{margin:14px 0 9px;font-size:15px}.details-tables{display:grid;grid-template-columns:1fr 1fr;gap:12px}.table-scroll{overflow:auto;max-width:100%}table{width:100%;border-collapse:collapse;min-width:690px}th,td{padding:11px 13px;border-top:1px solid #26364d;text-align:left}th{font-size:11px;letter-spacing:.035em;text-transform:uppercase;color:#b4c6de;background:#142338;white-space:nowrap}td{max-width:360px;overflow-wrap:anywhere}.table-scroll.compact table{min-width:360px}td .secondary{font-size:11px;color:var(--dim)}.right{text-align:right}.center{text-align:center}tbody tr:hover{background:#1a2d43}.link{color:#8ed4ff;text-decoration:none}.link:hover{text-decoration:underline}.empty{padding:35px 16px;color:var(--dim);text-align:center}.morebox{padding:15px;text-align:center;border-top:1px solid var(--edge)}.back{border:none;background:transparent;color:#8ed4ff;font-size:13px;padding:0;margin-bottom:8px}.sectiongap{margin:14px 0}.tagtext{font-size:11px;color:var(--dim)}footer{color:var(--dim);font-size:12px;margin-top:24px;max-width:1120px}footer p{margin:7px 0}
@media(max-width:780px){.shell{padding:17px 11px 40px}.brand h1{font-size:20px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi .big{font-size:22px}.matches,.sides,.details-tables{grid-template-columns:1fr}.toolbar select{flex:1 1 120px}.toolbar input[type=search]{flex-basis:100%}.detail-head h2{font-size:18px}.card-head .card-total{font-size:17px}}
</style>'''

SCRIPT = r'''<script>
(()=>{'use strict';
const D=window.__RADAR_DATA__||{},TR=D.trades||[],MS=D.markets||[];
const marketById=new Map(MS.map(m=>[m.c,m]));
const $=s=>document.querySelector(s), money=n=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(n||0);
const dt=t=>new Intl.DateTimeFormat('pl-PL',{timeZone:'Europe/Warsaw',dateStyle:'short',timeStyle:'short'}).format(t*1000);
const niceTime=t=>t?dt(t):'—', short=w=>w?w.slice(0,7)+'…'+w.slice(-5):'—';
const tierText=t=>t==='lower'?'Niższy tier':t==='major'?'Duży turniej':'Nieustalony';
const mk=(tag,cls='',text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=String(text);return e};
function child(parent,...nodes){nodes.forEach(n=>n&&parent.append(n));return parent}
function aLink(txt,url){if(!url)return mk('span','muted','—');const a=mk('a','link',txt);a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a}
function matchUrl(slug){return /^[A-Za-z0-9_-]{1,180}$/.test(slug||'')?'https://polymarket.com/event/'+slug:null}
function walletUrl(w){return /^0x[a-f0-9]{40}$/.test(w||'')?'https://polymarket.com/profile/'+w:null}
function td(tr,item,cls){const e=mk('td',cls||'');e.append(item instanceof Node?item:document.createTextNode(String(item)));tr.append(e);return e}
function pill(t){return mk('span','pill '+t,tierText(t))}
function eventKey(m){return (m.s||(m.m&&!m.m.startsWith('Nieznany mecz')?m.m:m.c)).toLowerCase()}
const allEvents=new Map();
for(const m of MS){let key=eventKey(m),event=allEvents.get(key);if(!event){event={key,title:m.m,slug:m.s,tier:m.tier,markets:[],rows:[]};allEvents.set(key,event)}event.markets.push(m)}
for(const r of TR){const m=marketById.get(r.c);if(!m)continue;let e=allEvents.get(eventKey(m));if(!e){e={key:eventKey(m),title:m.m,slug:m.s,tier:m.tier,markets:[m],rows:[]};allEvents.set(e.key,e)}e.rows.push(r)}
function looksProp(m){const q=(m.q||'').toLowerCase();return /\b(map\s*[1-9]|mapa\s*[1-9]|rounds?|pistol|handicap|spread|total|over|under|first to|correct score|kills|overtime|odd\/even|1st|2nd|3rd)\b/i.test(q) || (m.o.length===2&&m.o.every(x=>/^(yes|no)$/i.test(x)))}
function rankMarket(m,rows){let score=0;if(!looksProp(m))score+=20;if(m.o.length===2)score+=10;if(/\b(vs?\.?|versus)\b/i.test(m.q||''))score+=5;score+=Math.min(10,rows.filter(r=>r.c===m.c).length);return score}
function pickMarket(event,rows){return [...event.markets].sort((a,b)=>rankMarket(b,rows)-rankMarket(a,rows))[0]}
function outcomes(m,rows){const obs=[...new Set(rows.map(r=>r.o))];const set=[...new Set([...(m?.o||[]),...obs].filter(Boolean))];return set.slice(0,8)}
let tab='matches',selected=null,selectedMarket=null,displayLimit=130,exportRows=[];
function marketKind(m){const q=(m.q||'').toLowerCase();if(/\b(map\s*[1-9]|mapa\s*[1-9])\b/i.test(q))return 'map';return looksProp(m)?'other':'winner'}
function filterRows(ignoreStake=false){
 const term=$('#search').value.trim().toLowerCase(),outcome=$('#outcome').value.trim().toLowerCase(),min=Number($('#min').value)||0,max=Number($('#max').value)||Infinity;
 const oddMin=Number($('#oddmin').value)||0,oddMax=Number($('#oddmax').value)||Infinity;
 const days=Number($('#range').value)||30,tier=$('#tier').value,marketKindFilter=$('#marketKind').value;
 const since=Math.floor(Date.now()/1000)-days*86400;
 return TR.filter(r=>{const m=marketById.get(r.c);if(!m||r.t<since||(!ignoreStake&&(r.a<min||r.a>max))||(tier!=='all'&&m.tier!==tier))return false;
 const odds=1/r.p;if(odds<oddMin-1e-8||odds>oddMax+1e-8)return false;
 if(marketKindFilter!=='all'&&marketKind(m)!==marketKindFilter)return false;
 if(outcome&&!String(r.o||'').toLowerCase().includes(outcome))return false;
 return !term||[m.m,m.q,r.o,r.w,r.n].some(x=>String(x||'').toLowerCase().includes(term))})}
function sortRows(rows){const mode=$('#sort').value;if(mode==='largest')return rows.sort((a,b)=>b.a-a.a||b.t-a.t);
 if(mode==='odds')return rows.sort((a,b)=>1/b.p-1/a.p||b.a-a.a);
 return rows.sort((a,b)=>b.t-a.t)}
function statCalc(rows){const wallets=new Set(rows.filter(r=>r.w).map(r=>r.w));const evs=new Set(rows.map(r=>eventKey(marketById.get(r.c))));$('#nTrades').textContent=rows.length.toLocaleString('pl-PL');$('#nUSD').textContent=money(rows.reduce((a,r)=>a+r.a,0));$('#nGames').textContent=String(evs.size);$('#nWallets').textContent=String(wallets.size)}
function sideStat(rows,o){const rr=rows.filter(r=>r.o===o);return {o,total:rr.reduce((a,r)=>a+r.a,0),fills:rr.length, wallets:new Set(rr.filter(r=>r.w).map(r=>r.w)).size,latest:Math.max(0,...rr.map(r=>r.t))}}
function getSides(m,rows){let opts=outcomes(m,rows);if(!opts.length)opts=['Brak danych'];return opts.map(o=>sideStat(rows,o))}
function sideCard(side,total,i){const c=mk('div','sidebox'+(i?' alt':''));child(c,mk('h3','',side.o),mk('div','large money',money(side.total)));const b=mk('div','bar'+(i?' alt':''));const fill=mk('span');fill.style.width=(total>0?(side.total/total*100):0).toFixed(1)+'%';b.append(fill);child(c,b,mk('div','foot',`${side.fills} transakcji • ${side.wallets} portfeli • ${total?Math.round(side.total/total*100):0}% zaobserwowanych zakupów`));return c}
function card(event,rows){const m=pickMarket(event,rows);if(!m)return null;const main=rows.filter(r=>r.c===m.c);const sides=getSides(m,main);const total=main.reduce((a,r)=>a+r.a,0);const last=Math.max(0,...rows.map(r=>r.t));let b=mk('button','match-card');b.type='button';const h=mk('div','card-head'),left=mk('div');child(left,mk('div','match-title',event.title),mk('div','kicker',`${event.markets.length} rynków • ${rows.length} zaobserwowanych zakupów`));const totalBox=mk('div','');child(totalBox,mk('div','card-total money',money(rows.reduce((a,r)=>a+r.a,0))),mk('div','small muted','Wszystkie rynki'));child(h,left,totalBox);b.append(h);const p=mk('div');p.style.marginTop='9px';p.append(pill(event.tier));b.append(p);
 for(const [i,s] of sides.slice(0,2).entries()){const line=mk('div','side-row'),name=mk('span','teamname',s.o),val=mk('span','money',money(s.total));child(line,name,val);b.append(line);const bar=mk('div','bar'+(i?' alt':'')),fill=mk('span');fill.style.width=(total>0?s.total/total*100:0)+'%';bar.append(fill);b.append(bar)}
 const f=mk('div','card-foot');child(f,mk('span','',`Ostatni zakup: ${niceTime(last)}`),mk('span','',`Szczegóły →`));b.append(f);b.onclick=()=>openMatch(event.key);return b}
function showMatches(rows){const target=$('#main');target.replaceChildren();const evs=[];for(const e of allEvents.values()){const rr=rows.filter(r=>eventKey(marketById.get(r.c))===e.key);if(rr.length)evs.push({e,rr,volume:rr.reduce((a,r)=>a+r.a,0),last:Math.max(...rr.map(r=>r.t))})}
 const sort=$('#sort').value;evs.sort((a,b)=>sort==='newest'?b.last-a.last:
 sort==='odds'?Math.max(...b.rr.map(r=>1/r.p))-Math.max(...a.rr.map(r=>1/r.p)):
 b.volume-a.volume);const p=mk('section');const head=mk('div','panel-title');child(head,mk('h2','','Mecze CS2'),mk('span','count',`${evs.length} meczów z zakupami w wybranym zakresie`));p.append(head);target.append(p);const grid=mk('div','matches');target.append(grid);for(const x of evs.slice(0,displayLimit))grid.append(card(x.e,x.rr));if(!evs.length)target.append(mk('div','empty','Brak meczów dla wybranych filtrów.'));
 if(evs.length>displayLimit){const more=mk('div','morebox'),button=mk('button','btn','Pokaż kolejne 100');button.onclick=()=>{displayLimit+=100;showMatches(rows)};more.append(button);target.append(more)}
}
function openMatch(key){selected=key;selectedMarket=null;window.location.hash='match='+encodeURIComponent(key);tab='detail';render()}
function closeDetail(){selected=null;selectedMarket=null;window.location.hash='';tab='matches';render()}
function showDetail(rows){const event=allEvents.get(selected);if(!event){closeDetail();return}const eRows=rows.filter(r=>eventKey(marketById.get(r.c))===selected);const target=$('#main');target.replaceChildren();const p=mk('div','panel detail');const h=mk('div','detail-head');const left=mk('div');const back=mk('button','back','← Wróć do meczów');back.onclick=closeDetail;left.append(back, mk('h2','',event.title));left.append(mk('div','detail-meta',`${eRows.length} zakupów w filtrze • ${event.markets.length} rynków • niepełna obserwacja ≥ 100 USD`));h.append(left);h.append(aLink('Polymarket ↗',matchUrl(event.slug)));p.append(h);
 const ctrl=mk('div','detail-controls');ctrl.append(mk('strong','','Rynek:'));const sel=mk('select');const best=pickMarket(event,eRows);for(const m of event.markets){const o=mk('option','',m.q||'Rynek bez nazwy');o.value=m.c;sel.append(o)}if(!selectedMarket||!event.markets.some(m=>m.c===selectedMarket))selectedMarket=best?.c||event.markets[0]?.c;sel.value=selectedMarket;sel.onchange=()=>{selectedMarket=sel.value;showDetail(filterRows())};ctrl.append(sel,pill(event.tier));p.append(ctrl);
 const chosen=marketById.get(selectedMarket),rr=eRows.filter(r=>r.c===selectedMarket),sides=getSides(chosen,rr),sum=rr.reduce((a,r)=>a+r.a,0);const s=mk('div','sides');for(let i=0;i<Math.max(2,sides.length);i++){const side=sides[i]||{o:'Druga strona — brak danych',total:0,fills:0,wallets:0};s.append(sideCard(side,sum,i))}p.append(s);
 const desc=mk('div','subsection small muted',`Wartości to suma zaobserwowanych zakupów BUY w wybranym rynku i czasie. Nie uwzględniamy sprzedaży, mniejszych zleceń ani brakujących danych API. Zmieniając rynek, możesz przejrzeć także mapy i inne typy zakładów.`);p.append(desc);
 const two=mk('div','subsection');two.append(mk('h3','','Portfele — obie strony rynku'));const box=mk('div','details-tables');for(let i=0;i<Math.min(sides.length,2);i++){const side=sides[i],wallets=new Map();for(const r of rr.filter(r=>r.o===side.o)){const key=r.w||'Brak portfela';let obj=wallets.get(key);if(!obj){obj={w:key,amount:0,fills:0,last:0,n:r.n};wallets.set(key,obj)}obj.amount+=r.a;obj.fills++;obj.last=Math.max(obj.last,r.t)}const v=[...wallets.values()].sort((a,b)=>b.amount-a.amount);const wrap=mk('div','panel'),title=mk('div','panel-title');child(title,mk('strong','',side.o),mk('span','small muted',`${v.length} portfeli`));wrap.append(title);const sc=mk('div','table-scroll compact'),t=mk('table'),thead=mk('thead'),tr=mk('tr');['Portfel','Zakupy','Liczba'].forEach(h=>child(tr,mk('th','',h)));thead.append(tr);t.append(thead);const tb=mk('tbody');for(const x of v.slice(0,25)){const r=mk('tr');td(r,x.w==='Brak portfela'?x.w:aLink(x.n||short(x.w),walletUrl(x.w)));td(r,money(x.amount),'money');td(r,x.fills);tb.append(r)}t.append(tb);sc.append(t);wrap.append(sc);if(!v.length)wrap.append(mk('div','empty','Brak zakupów tej strony w obserwowanych danych.'));box.append(wrap)}two.append(box);p.append(two);
 const trans=mk('div','subsection');trans.append(mk('h3','','Wszystkie zaobserwowane zakupy w tym rynku'));const sc=mk('div','table-scroll'),table=tradeTable(rr);sc.append(table);trans.append(sc);if(!rr.length)trans.append(mk('div','empty','Brak zakupów w tym rynku dla wybranych filtrów.'));if(rr.length>500)trans.append(mk('div','small muted',`Widocznych 500 z ${rr.length} zapisów. Pozostałe pobierz przez Eksport CSV.`));p.append(trans);
 const all=mk('div','subsection');all.append(mk('h3','',`Wszystkie rynki meczu — ${eRows.length} zakupów`));all.append(mk('div','small muted','Poniżej także zakupy na mapy i inne wyniki. Wiersze mają nazwę rynku, aby nie mieszać ich ze zwycięzcą meczu.'));const wrapAll=mk('div','table-scroll');wrapAll.append(tradeTable(eRows));all.append(wrapAll);if(eRows.length>500)all.append(mk('div','small muted',`Widocznych 500 z ${eRows.length} zapisów. Pozostałe pobierz przez Eksport CSV.`));p.append(all);target.append(p);exportRows=eRows;
}
function tradeTable(rows){const table=mk('table');const th=mk('tr');['Czas (PL)','Mecz / rynek','Strona','Zakup','Kurs (≈)','Portfel'].forEach(x=>th.append(mk('th','',x)));table.append(child(mk('thead'),th));const tb=mk('tbody');for(const r of rows.slice(0,500)){const m=marketById.get(r.c);if(!m)continue;const tr=mk('tr');td(tr,dt(r.t));const wrap=mk('div');child(wrap,mk('div','',m.m),mk('div','secondary',m.q));td(tr,wrap);td(tr,r.o);td(tr,money(r.a),'money');td(tr,(1/r.p).toFixed(2));td(tr,r.w?aLink(r.n||short(r.w),walletUrl(r.w)):'—');tb.append(tr)}table.append(tb);return table}
function showTrades(rows){const target=$('#main');target.replaceChildren();const panel=mk('section','panel'),h=mk('div','panel-title');child(h,mk('h2','','Zaobserwowane zakupy'),mk('span','count',`${rows.length} transakcji, pierwsze 500 poniżej`));panel.append(h);const wrap=mk('div','table-scroll');wrap.append(tradeTable(rows));panel.append(wrap);if(!rows.length)panel.append(mk('div','empty','Brak transakcji w wybranym filtrze.'));target.append(panel);exportRows=rows}
function showPositions(rows){
 const map=new Map();for(const r of rows){if(!r.w)continue;
 const key=[r.w,r.c,r.o].join('\u0001'),m=marketById.get(r.c);if(!m)continue;
 let p=map.get(key);if(!p){p={w:r.w,c:r.c,o:r.o,n:r.n,total:0,shares:0,buys:0,last:0};map.set(key,p)}
 p.total+=r.a;p.shares+=r.a/r.p;p.buys++;p.last=Math.max(p.last,r.t)}
 const min=Number($('#min').value)||0,max=Number($('#max').value)||Infinity;
 const items=[...map.values()].filter(p=>p.total>=min&&p.total<=max);const mode=$('#sort').value;
 items.sort((a,b)=>mode==='newest'?b.last-a.last:mode==='odds'?(b.shares/b.total)-(a.shares/a.total):b.total-a.total);
 const target=$('#main');target.replaceChildren();const panel=mk('section','panel'),head=mk('div','panel-title');
 child(head,mk('h2','','Pozycje — suma zaobserwowanych zakupów'),mk('span','count',`${items.length} kombinacji portfel / rynek / wynik`));panel.append(head);
 panel.append(mk('div','subsection small muted','Łączne zakupy BRUTTO jednego portfela na konkretny wynik. Kurs średni = nabyte udziały / wydane USD, nie średnia arytmetyczna kursów. Sprzedaż i saldo pozycji nie są uwzględnione.'));
 const wrap=mk('div','table-scroll'),t=mk('table'),tr=mk('tr');['Mecz / rynek','Wynik','Portfel','Suma zakupów','Zakupy','Śr. kurs (≈)','Ostatni'].forEach(x=>tr.append(mk('th','',x)));t.append(child(mk('thead'),tr));
 const tbody=mk('tbody');for(const x of items.slice(0,500)){const m=marketById.get(x.c),tr=mk('tr'),title=mk('div');child(title,mk('div','',m.m),mk('div','secondary',m.q));td(tr,title);td(tr,x.o);td(tr,aLink(x.n||short(x.w),walletUrl(x.w)));td(tr,money(x.total),'money');td(tr,x.buys);td(tr,(x.shares/x.total).toFixed(2));td(tr,dt(x.last));tbody.append(tr)}t.append(tbody);wrap.append(t);panel.append(wrap);
 if(!items.length)panel.append(mk('div','empty','Brak pozycji spełniających filtry.'));
 if(items.length>500)panel.append(mk('div','small muted',`Widocznych 500 z ${items.length}. Użyj filtrów lub eksportu CSV transakcji.`));target.append(panel);exportRows=rows;
}
function showWallets(rows){const list=new Map();for(const r of rows){if(!r.w)continue;let entry=list.get(r.w);if(!entry){entry={w:r.w,n:r.n,amount:0,fills:0,events:new Set(),last:0};list.set(r.w,entry)}entry.amount+=r.a;entry.fills++;entry.events.add(eventKey(marketById.get(r.c)));entry.last=Math.max(entry.last,r.t)}const ranked=[...list.values()].sort((a,b)=>b.amount-a.amount);const target=$('#main');target.replaceChildren();const panel=mk('section','panel'),head=mk('div','panel-title');child(head,mk('h2','','Portfele'),mk('span','count',`${ranked.length} unikatowych portfeli`));panel.append(head);const wrap=mk('div','table-scroll');const table=mk('table'),tr=mk('tr');['Portfel','Łączne zakupy','Transakcje','Mecze','Ostatni zakup'].forEach(x=>tr.append(mk('th','',x)));table.append(child(mk('thead'),tr));const tb=mk('tbody');for(const e of ranked.slice(0,500)){const r=mk('tr');td(r,aLink(e.n||short(e.w),walletUrl(e.w)));td(r,money(e.amount),'money');td(r,e.fills);td(r,e.events.size);td(r,dt(e.last));tb.append(r)}table.append(tb);wrap.append(table);panel.append(wrap);target.append(panel);exportRows=rows}
function exportCsv(){const cols=['timestamp_pl','mecz','rynek','wynik','zakup_usd','cena_udzialu','kurs_przyblizony','portfel','condition_id','polymarket_url'];const lines=[cols];for(const r of exportRows){const m=marketById.get(r.c)||{};lines.push([dt(r.t),m.m||'',m.q||'',r.o,r.a,r.p,(1/r.p).toFixed(4),r.w,r.c,matchUrl(m.s)||''])}const escape=x=>'"'+String(x??'').replaceAll('"','""')+'"';const content='\ufeff'+lines.map(row=>row.map(escape).join(';')).join('\r\n');const blob=new Blob([content],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob);const a=mk('a');a.href=url;a.download='cs2-radar-zakupy.csv';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000)}
function render(){const rows=sortRows(filterRows(tab==='positions'));statCalc(rows);for(const b of document.querySelectorAll('[data-tab]'))b.classList.toggle('active',b.dataset.tab===(tab==='detail'?'matches':tab));displayLimit=Math.max(130,displayLimit);if(tab==='detail')showDetail(rows);else if(tab==='trades')showTrades(rows);else if(tab==='wallets')showWallets(rows);else if(tab==='positions')showPositions(rows);else{exportRows=rows;showMatches(rows)}}
for(const tabButton of document.querySelectorAll('[data-tab]'))tabButton.onclick=()=>{tab=tabButton.dataset.tab;selected=null;selectedMarket=null;location.hash='';displayLimit=130;render()};
for(const name of ['#search','#outcome','#min','#max','#oddmin','#oddmax','#marketKind','#sort','#range','#tier'])$(name).addEventListener('input',()=>{displayLimit=130;render()});
$('#export').onclick=exportCsv;const scan=Number(D.lastScan||0);$('#stamp').textContent=dt(Math.floor(Date.parse(D.generated)/1000));$('#scan').textContent=scan?dt(scan):'brak potwierdzenia';
if(location.hash.startsWith('#match=')){const key=decodeURIComponent(location.hash.slice(7));if(allEvents.has(key)){selected=key;tab='detail'}}render();
})();
</script>'''


def build_page(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    html = '''<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#080d16"><title>CS2 Polymarket Radar v1.8 — filtry kursów</title>'''
    html += STYLE + '''</head><body><div class="shell">
<header class="brandbar"><div class="brand"><div class="logo" aria-hidden="true">◎</div><div><h1>CS2 Radar <span class="muted">/ Analiza rynków v1.8</span></h1><div class="small muted">Obserwowane zakupy Polymarket · Tier 2/3</div></div></div><div class="status"><span class="bullet"></span>Eksport po skanie GitHub Actions</div></header>
<div class="note"><b>Uwaga:</b> pokazujemy tylko zaobserwowane BUY od progu API (zwykle 100 USD). Domyślny filtr strony to kurs ≥ 1.20; wybierz „Kurs: wszystkie”, aby zobaczyć także zakupy po 1.01. Suma zakupów nie oznacza aktualnej pozycji portfela ani pełnego wolumenu Polymarket.</div>
<section class="kpis"><div class="kpi"><div class="hint">Zakupy w filtrze</div><div id="nTrades" class="big">—</div></div><div class="kpi"><div class="hint">Łączna wartość brutto</div><div id="nUSD" class="big">—</div></div><div class="kpi"><div class="hint">Mecze z aktywnością</div><div id="nGames" class="big">—</div></div><div class="kpi"><div class="hint">Portfele</div><div id="nWallets" class="big">—</div></div></section>
<nav class="tabs" aria-label="Widoki panelu"><button type="button" class="tab active" data-tab="matches">⚔ Mecze</button><button type="button" class="tab" data-tab="trades">▤ Transakcje</button><button type="button" class="tab" data-tab="positions">▥ Pozycje portfeli</button><button type="button" class="tab" data-tab="wallets">♙ Portfele</button></nav>
<div class="toolbar"><input id="search" type="search" placeholder="Mecz, drużyna, turniej, portfel…" aria-label="Szukaj">
<select id="range" aria-label="Zakres czasu"><option value="1">Ostatnie 24 h</option><option value="7" selected>Ostatnie 7 dni</option><option value="30">Ostatnie 30 dni</option></select>
<select id="tier" aria-label="Poziom turnieju"><option value="all">Wszystkie tiery</option><option value="lower">Niższy tier</option><option value="unknown">Nieustalony</option><option value="major">Duże turnieje</option></select>
<select id="marketKind" aria-label="Typ rynku"><option value="all">Wszystkie typy rynku</option><option value="winner">Zwycięzca meczu</option><option value="map">Zakłady na mapy</option><option value="other">Inne zakłady</option></select>
<select id="min" aria-label="Minimalna wartość zakupu"><option value="0">Kwota: bez filtra</option><option value="200">Od 200 USD</option><option value="500">Od 500 USD</option><option value="750">Od 750 USD</option><option value="1000">Od 1 000 USD</option><option value="2000">Od 2 000 USD</option><option value="5000">Od 5 000 USD</option></select>
<select id="max" aria-label="Maksymalna wartość zakupu"><option value="">Kwota max: bez limitu</option><option value="500">Do 500 USD</option><option value="1000">Do 1 000 USD</option><option value="2500">Do 2 500 USD</option><option value="5000">Do 5 000 USD</option><option value="10000">Do 10 000 USD</option></select>
<select id="oddmin" aria-label="Minimalny kurs"><option value="0">Kurs: wszystkie</option><option value="1.2" selected>Kurs od 1.20</option><option value="1.5">Kurs od 1.50</option><option value="1.7">Kurs od 1.70</option><option value="2">Kurs od 2.00</option><option value="3">Kurs od 3.00</option><option value="5">Kurs od 5.00</option></select>
<select id="oddmax" aria-label="Maksymalny kurs"><option value="">Kurs max: bez limitu</option><option value="2">Kurs do 2.00</option><option value="3">Kurs do 3.00</option><option value="5">Kurs do 5.00</option><option value="10">Kurs do 10.00</option></select>
<input id="outcome" type="search" placeholder="Wynik / drużyna…" aria-label="Filtr obstawionego wyniku">
<select id="sort" aria-label="Sortowanie"><option value="largest" selected>Sortuj: największe kwoty</option><option value="newest">Sortuj: najnowsze</option><option value="odds">Sortuj: najwyższe kursy</option></select>
<button id="export" type="button">↓ Eksport CSV</button></div>
<main id="main" aria-live="polite"></main>
<footer><p>Ostatni pełny skan: <b id="scan"></b> · Wygenerowano: <b id="stamp"></b> (czas polski). Odśwież stronę, aby zobaczyć najnowszy opublikowany raport.</p><p>Porównanie stron rynku opiera się wyłącznie na zarejestrowanych BUY: obie strony mogą nie mieć równych danych. Podział na niższe tiery jest heurystyczny. Transakcje bez pełnych metadanych mogą znajdować się w osobnej grupie „Nieznany mecz”.</p><p>Panel jest publiczny w GitHub Pages. Nie publikuje tokenu Telegram ani sekretów GitHuba.</p></footer></div>
<script>window.__RADAR_DATA__=''' + raw + ''';</script>''' + SCRIPT + '''</body></html>'''
    return html


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',type=Path,default=ROOT/'radar.sqlite3')
    parser.add_argument('--cache',type=Path,default=ROOT/'market_cache.json')
    parser.add_argument('--out',type=Path,default=ROOT/'public'/'index.html')
    args=parser.parse_args()
    data=fetch_data(args.db,args.cache)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(build_page(data),encoding='utf-8')
    print(f"Panel wygenerowany: {args.out}, {len(data['trades'])} transakcji, {len(data['markets'])} rynków")

if __name__=='__main__':main()
