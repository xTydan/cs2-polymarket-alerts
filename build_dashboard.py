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

from hltv_results import refresh as refresh_hltv
from hltv_matches import refresh_matches

ROOT = Path(__file__).resolve().parent
VALID_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,180}$")
VALID_WALLET = re.compile(r"^0x[a-fA-F0-9]{40}$")


def fetch_data(db_path: Path, cache_path: Path, maximum: int = 50000,
               hltv_cache_path: Path | None = None, hltv_matches_cache_path: Path | None = None) -> dict:
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
            start_col = 'match_start' if 'match_start' in market_cols else '0'
            league_col = 'league' if 'league' in market_cols else "''"
            closed_col = 'event_closed' if 'event_closed' in market_cols else '0'
            volume_col = 'event_volume' if 'event_volume' in market_cols else 'NULL'
            for r in con.execute(f'''SELECT condition_id,title,market_title,slug,tier,
                                     {outcomes_col} AS outcomes_json, {eid_col} AS event_id,
                                     {start_col} AS match_start, {league_col} AS league,
                                     {closed_col} AS event_closed,
                                     {volume_col} AS event_volume
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
        ev_vol = b.get('event_volume') if b.get('event_volume') is not None else a.get('event_volume')
        try: ev_vol = max(0.0, float(ev_vol)) if ev_vol is not None else None
        except (ValueError, TypeError, OverflowError): ev_vol = None
        markets[cid] = {'c':cid,'m':title or 'Nieznany mecz (brak metadanych)',
                        'q':question,'s':slug,'tier':tier,'o':outcomes,
                        'e':str(a.get('event_id') or b.get('event_id') or '')[:50],
                        'start':int(b.get('match_start') or a.get('match_start') or 0),
                        'volume':ev_vol,
                        'league':str(b.get('league') or a.get('league') or '')[:140],
                        'closed':bool(b['event_closed'] if 'event_closed' in b else a.get('event_closed') or False)}
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
    # W kalendarzu widoczne sa rowniez przyszle mecze bez zarejestrowanych zakupow.
    # Historię zamknietych rynkow bierzemy z SQLite tylko jeśli mają transakcje.
    view_markets = [m for m in markets.values()
                    if m['c'] in used or m['c'] in cached]
    view_markets.sort(key=lambda m:(m['m'],m['q']))
    # Each event corresponds to the dashboard's slug-or-title key.
    hltv_events = {}
    for m in view_markets:
        key = str(m['s'] or (m['m'] if not m['m'].startswith('Nieznany mecz') else m['c'])).lower()
        entry = hltv_events.setdefault(key, {'key': key, 'title': m['m'], 'start': 0})
        timestamp = int(m['start'] or 0)
        if timestamp and (not entry['start'] or timestamp < entry['start']):
            entry['start'] = timestamp
    hltv_cache = hltv_cache_path or ROOT / 'hltv_results_cache.json'
    match_cache = hltv_matches_cache_path or ROOT / 'hltv_matches_cache.json'
    current_matches = refresh_matches(match_cache, list(hltv_events.values()))
    hltv_results = {**current_matches, **refresh_hltv(hltv_cache, list(hltv_events.values()))}
    return {
        'hltvResults': hltv_results,
        'generated':datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'lastScan': meta.get('last_successful_poll',''),
        'minimum':100,
        'trades':trades,
        'markets':view_markets,
        'notice': 'Godziny meczu z gameStartTime/startTime Gamma, PL. Obrót PM to suma wymian na Polymarket, a nie suma zakupów zaobserwowanych przez radar. Domyślnie bez nowych rynków o znanym obrocie poniżej 100 USD; brak obrotu nie skutkuje usunięciem. Zakupy BUY nie są saldem pozycji.'
    }


STYLE = r'''<style>
:root{color-scheme:dark;--bg:#080d16;--surface:#111c2d;--panel:#131f33;--edge:#263750;--fg:#ecf3ff;--dim:#a0b1cb;--blue:#4bb5f7;--mint:#72e4c2;--gold:#eac382;--danger:#f7a8a8}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 85% 0%,#12314b 0,transparent 45%),linear-gradient(#0c1523,#080d16 65%);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Arial;min-height:100vh}
a{color:#90d5ff}button,input,select{font:inherit}button{cursor:pointer}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.shell{max-width:1440px;margin:0 auto;padding:26px 22px 70px}.brandbar{display:flex;justify-content:space-between;gap:18px;align-items:center;flex-wrap:wrap;margin-bottom:18px}.brand{display:flex;align-items:center;gap:13px}.logo{width:52px;height:52px;border:1px solid #39729e;border-radius:15px;display:grid;place-items:center;background:#123147;font-size:26px}.brand h1{font-size:24px;letter-spacing:-.65px;margin:0;line-height:1.2}.muted{color:var(--dim)}.small{font-size:12px}.status{font-size:12px;border:1px solid #356d68;padding:7px 12px;border-radius:99px;background:#10312f;color:#aff7dc}.bullet{display:inline-block;width:8px;height:8px;margin-right:6px;border-radius:50%;background:#5ce6b6}
.note{background:rgba(16,39,61,.8);border-left:3px solid #66abdf;border-radius:7px;padding:10px 15px;margin-bottom:18px;color:#d6e6f9;font-size:12px}.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px;margin-bottom:22px}.kpi{background:rgba(19,32,52,.92);border:1px solid var(--edge);border-radius:14px;padding:16px}.kpi .big{font-size:26px;font-weight:750;letter-spacing:-.6px}.kpi .hint{font-size:12px;color:var(--dim)}
.tabs{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap}.tab{padding:11px 18px;background:transparent;color:var(--dim);border:1px solid var(--edge);border-radius:10px}.tab:hover,.tab.active{color:var(--fg);background:#193c58;border-color:#39799d}.tab.active{font-weight:650}.toolbar{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px;align-items:center}.toolbar input,.toolbar select{background:#101b2d;color:var(--fg);border:1px solid #344765;border-radius:10px;padding:10px 12px;height:43px}.toolbar input[type=search]{flex:1 1 240px;min-width:150px}.toolbar select{flex:0 1 160px}.toolbar button,.btn{border:1px solid #41617e;border-radius:10px;background:#19324b;color:var(--fg);padding:10px 14px;text-decoration:none;display:inline-flex;align-items:center;gap:7px}.toolbar button:hover,.btn:hover{background:#234968}
.range-group{display:flex;gap:6px;align-items:center;border:1px solid #35516f;border-radius:11px;padding:6px 9px;flex:1 1 300px;flex-wrap:wrap;background:#0d1a2a;min-width:275px}.range-heading{font-size:10px;font-weight:800;letter-spacing:.06em;color:#a4bfdc;margin-right:6px;white-space:nowrap}.range-field{display:flex;align-items:center;gap:6px;flex:1 1 90px;min-width:80px}.range-field span{font-size:11px;color:#a8bbd1}.toolbar .range-field input{padding:7px 9px;width:100%;min-width:0;height:35px;background:#162943;font-variant-numeric:tabular-nums}.toolbar #reset-bounds{font-size:12px;min-height:43px}.pressure-controls .pressure-field{display:flex;gap:5px;align-items:center}.pressure-controls .pressure-field input{background:#102035;border:1px solid #435976;color:#edf4ff;border-radius:8px;padding:8px 9px;width:110px;min-height:35px;font-variant-numeric:tabular-nums}
@media(max-width:670px){.range-group{flex-basis:100%;min-width:0}.range-heading{flex-basis:100%;margin-bottom:2px}.pressure-controls .pressure-field{flex:1 1 145px;flex-wrap:wrap}.pressure-controls .pressure-field input{width:100%}}
.panel{background:rgba(18,29,47,.94);border:1px solid var(--edge);border-radius:16px;overflow:hidden}.panel-title{padding:16px 18px;border-bottom:1px solid var(--edge);display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}.panel-title h2{font-size:17px;margin:0}.count{font-size:12px;color:var(--dim)}.matches{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.match-card{padding:17px;background:rgba(19,32,52,.96);border:1px solid var(--edge);border-radius:15px;text-align:left;color:var(--fg);transition:background .12s,border-color .12s}.match-card:hover{background:#192e46;border-color:#487ba0}.card-head{display:flex;gap:14px;justify-content:space-between;align-items:flex-start}.match-title{font-size:15px;font-weight:700;line-height:1.4}.kicker{color:var(--dim);font-size:11px;margin-top:5px}.pill{display:inline-block;font-size:11px;border-radius:99px;border:1px solid #5e6d7e;padding:3px 8px;white-space:nowrap}.pill.lower{border-color:#80633e;color:#f5d09c;background:#34271a}.pill.unknown{color:#c5d4e6}.pill.major{color:#bddbff;border-color:#476b9d}.card-total{font-size:19px;font-weight:730;white-space:nowrap}.side-row{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;margin-top:11px}.teamname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.money{font-variant-numeric:tabular-nums;font-weight:700;color:#94ebca}.bar{background:#2b3d51;border-radius:99px;height:7px;overflow:hidden;margin-top:6px}.bar>span{display:block;background:linear-gradient(90deg,#4d8ad1,#70cadd);height:100%}.bar.alt>span{background:linear-gradient(90deg,#bb8ac9,#e1b9d4)}.card-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:14px;color:var(--dim);font-size:12px}
.detail{margin-bottom:17px}.detail-head{padding:18px 20px;border-bottom:1px solid var(--edge);display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}.detail-head h2{margin:6px 0 2px;font-size:21px}.detail-meta{font-size:12px;color:var(--dim)}.detail-controls{padding:13px 18px;border-bottom:1px solid var(--edge);display:flex;gap:12px;align-items:center;flex-wrap:wrap}.detail-controls select{max-width:100%;flex:1;background:#0e192a;color:var(--fg);border:1px solid #3b516e;border-radius:9px;padding:9px;min-width:160px}.sides{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:16px}.sidebox{background:#102338;border:1px solid #35506d;border-radius:12px;padding:16px;min-width:0}.sidebox.alt{background:#261c32;border-color:#655077}.sidebox h3{font-size:17px;margin:0 0 10px;overflow-wrap:anywhere}.sidebox .large{font-size:30px;font-weight:750;letter-spacing:-.6px}.sidebox .foot{font-size:12px;color:var(--dim);margin-top:6px}.subsection{padding:0 16px 16px}.subsection h3{margin:14px 0 9px;font-size:15px}.details-tables{display:grid;grid-template-columns:1fr 1fr;gap:12px}.table-scroll{overflow:auto;max-width:100%}table{width:100%;border-collapse:collapse;min-width:690px}th,td{padding:11px 13px;border-top:1px solid #26364d;text-align:left}th{font-size:11px;letter-spacing:.035em;text-transform:uppercase;color:#b4c6de;background:#142338;white-space:nowrap}td{max-width:360px;overflow-wrap:anywhere}.table-scroll.compact table{min-width:360px}td .secondary{font-size:11px;color:var(--dim)}.right{text-align:right}.center{text-align:center}tbody tr:hover{background:#1a2d43}.link{color:#8ed4ff;text-decoration:none}.link:hover{text-decoration:underline}.empty{padding:35px 16px;color:var(--dim);text-align:center}.morebox{padding:15px;text-align:center;border-top:1px solid var(--edge)}.back{border:none;background:transparent;color:#8ed4ff;font-size:13px;padding:0;margin-bottom:8px}.sectiongap{margin:14px 0}.tagtext{font-size:11px;color:var(--dim)}footer{color:var(--dim);font-size:12px;margin-top:24px;max-width:1120px}footer p{margin:7px 0}
@media(max-width:780px){.shell{padding:17px 11px 40px}.brand h1{font-size:20px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi .big{font-size:22px}.matches,.sides,.details-tables{grid-template-columns:1fr}.toolbar select{flex:1 1 120px}.toolbar input[type=search]{flex-basis:100%}.detail-head h2{font-size:18px}.card-head .card-total{font-size:17px}}


/* v1.9: compact match schedule */
.fixture-layout{display:grid;grid-template-columns:226px minmax(0,1fr);gap:16px;align-items:start}
.fixture-sidebar{border:1px solid var(--edge);border-radius:13px;background:#101b2d;padding:16px;position:sticky;top:10px;min-width:0}
.side-heading{font-size:11px;letter-spacing:.095em;color:#9fb8d3;font-weight:800;margin-bottom:12px}.gap-above{margin-top:22px}
.fixture-stage{width:100%;text-align:left;display:block;background:none;color:#bfd0e7;border:1px solid transparent;padding:10px 12px;border-radius:9px;margin-bottom:3px}
.fixture-stage:hover,.fixture-stage.active{background:#1c3e5b;color:#fff;border-color:#3e7094}.fixture-check{display:flex;gap:8px;align-items:center;margin-top:14px;color:#c1d3e6;font-size:12px}
.fixture-select{max-width:100%;width:100%;background:#112438;color:#eef5ff;border:1px solid #35516d;border-radius:9px;padding:10px 8px}
.fixture-aside-note{color:var(--dim);font-size:11px;line-height:1.55;margin:20px 2px 0}
.fixture-content{min-width:0}.fixture-main-head{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:8px 6px 17px}
.fixture-main-head h2{font-size:19px;margin:0 0 4px}.fixture-legacy{padding:0 6px 8px;margin:0}.fixture-count{background:#1a3b53;border:1px solid #355573;border-radius:40px;padding:7px 10px;white-space:nowrap;font-size:12px}
.fixtures-section-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:12px 0 8px;padding:6px 7px}.fixtures-section-head h3{font-size:15px;margin:0}
.fixture-list{border-radius:12px;border:1px solid var(--edge);background:#121e30;overflow:hidden;margin-bottom:22px}
.fixture-day{padding:8px 14px;background:#192941;border-top:1px solid #2d4057;color:#bed0e8;font-size:11px;font-weight:750;text-transform:capitalize;letter-spacing:.025em}
.fixture-day:first-child{border-top:0}.fixture-row{display:grid;grid-template-columns:85px minmax(135px,1.3fr) minmax(130px,1.2fr) minmax(172px,1.1fr) minmax(110px,.72fr) 84px 17px;align-items:center;column-gap:12px;padding:13px 14px;width:100%;text-align:left;background:transparent;border:0;border-top:1px solid #263851;color:var(--fg)}
.fixture-row:hover{background:#1c324c}.fixture-time{font-size:11px;color:#c4d8ed;text-align:center;padding-right:13px;border-right:1px solid #354862}.fixture-clock{font-size:15px;font-weight:750;font-variant-numeric:tabular-nums}
.fixture-when{font-size:9px;line-height:1.1;font-weight:800;letter-spacing:.04em;margin-top:4px;color:#97abc5}.fixture-when.started{color:#eac382}.fixture-when.upcoming{color:#78e4bc}.fixture-when.live{color:#ff798f}
.fixture-result{color:#8bd7bb;font-weight:750;font-size:12px;margin-top:5px}.fixture-teams{min-width:0}.fixture-team{font-weight:680;line-height:1.45;overflow-wrap:anywhere}.fixture-meta{min-width:0}.fixture-league{color:#b8cae1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.fixture-tags{display:flex;flex-wrap:wrap;align-items:center;gap:5px;margin-top:5px}.tiny-label{font-size:10px;color:#c4d3e7;border:1px solid #3c506d;padding:2px 6px;border-radius:4px}
.fixture-sides{display:grid;grid-template-columns:1fr 1fr;gap:8px;min-width:0}.side-stack{min-width:0}.side-caption{display:block;font-size:10px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.side-value{font-size:12px;font-weight:730;color:#96e8cf;white-space:nowrap}
.fixture-total{font-size:14px;white-space:nowrap}.fixture-volume .small{font-size:10px}.fixture-odds{font-size:12px;color:#e0c48b;white-space:nowrap;text-align:right}.fixture-arrow{font-size:24px;color:#a3c5db;line-height:1;text-align:right}.show-more{margin:-3px 0 22px 12px}
@media(max-width:1260px){.fixture-row{grid-template-columns:72px minmax(112px,1.4fr) minmax(110px,1fr) minmax(138px,1.05fr) minmax(95px,.85fr) 16px;gap:8px}.fixture-odds{display:none}.fixture-layout{grid-template-columns:195px minmax(0,1fr)} }
@media(max-width:880px){.fixture-layout{display:block}.fixture-sidebar{position:static;display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;padding:10px;margin-bottom:14px}.fixture-sidebar .side-heading,.fixture-sidebar .fixture-aside-note{display:none}.fixture-stage{width:auto;padding:8px 10px;margin:0;font-size:12px}.fixture-check{margin:0 8px}.fixture-select{width:auto;flex:1 1 150px;max-width:300px;font-size:12px}}
@media(max-width:670px){.fixture-row{grid-template-columns:59px minmax(0,1.25fr) minmax(100px,1fr) 12px;gap:8px;padding:12px 8px}.fixture-time{padding-right:7px}.fixture-clock{font-size:13px}.fixture-teams{font-size:12px}.fixture-meta{grid-column:2 / 4;grid-row:2;padding-top:3px}.fixture-sides{display:none}.fixture-volume{text-align:right}.fixture-total{font-size:12px}.fixture-row .fixture-arrow{grid-column:4;grid-row:1}.fixture-league{font-size:11px}.fixture-main-head h2{font-size:16px}.fixture-count{font-size:10px}.fixture-day{font-size:10px}}


/* Analiza rozkładu zakupów i dostępne sortowanie kolumn. */
.pressure-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:13px 16px;border-bottom:1px solid var(--edge)}
.pressure-controls select{background:#102035;border:1px solid #435976;color:#edf4ff;border-radius:8px;padding:7px 10px;min-height:35px}
.flow-note{color:var(--dim);font-size:12px;padding:10px 17px;margin:0;max-width:1040px}
.flow-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));gap:10px;padding:14px}
.flow-card{display:block;text-align:left;border:1px solid #345473;border-radius:11px;padding:13px 14px;background:#15273f;color:var(--fg);width:100%;cursor:pointer}
.flow-card:hover{border-color:#6db9e8;background:#1b3451}
.flow-card.compact{padding:11px 12px}
.flow-head{display:flex;align-items:start;justify-content:space-between;gap:10px;font-size:13px}
.flow-pct{background:#174a49;color:#aff4de;padding:2px 8px;border-radius:6px;white-space:nowrap;font-variant-numeric:tabular-nums;font-weight:800}
.flow-sub{font-size:12px;margin:7px 0;color:#d5e9fa}
.flow-bar{height:7px;border-radius:8px;background:#34455b;overflow:hidden}.flow-bar span{display:block;height:100%;background:#5bdfb9}
.flow-notes{display:flex;gap:7px 15px;flex-wrap:wrap;margin-top:7px;color:#adbed2;font-size:11px}
.flow-rise{color:#9cf6d3}.flow-caution{color:#f1bd84}
.flow-teaser{border:1px solid #355270;background:#102438;border-radius:10px;margin:12px 0 15px;padding:11px}
.flow-teaser-title{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:8px}
.flow-teaser-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,290px),1fr));gap:9px}
.flow-teaser button.show-all{color:#9fdbff;border:1px solid #427599;background:#153853;border-radius:7px;padding:6px 10px;cursor:pointer}
.flow-fixture-tag{display:inline-block;margin-top:3px;padding:2px 6px;border-radius:5px;background:#164a46;color:#b2f4dd;font-size:10px;font-weight:700}
.sort-head{display:inline-flex;align-items:center;gap:5px;background:transparent;border:0;padding:0;color:inherit;text-align:left;font-size:inherit;font-weight:inherit;cursor:pointer}
.sort-head:hover{color:#8ad4ff}.sort-head[aria-pressed="true"]{color:#8de9d1}
.sort-arrow{font-size:11px;min-width:10px}
@media(max-width:680px){.flow-grid{grid-template-columns:1fr;padding:9px}.pressure-controls{padding:10px}.pressure-controls select{flex:1 1 120px}.flow-teaser-grid{grid-template-columns:1fr}}
</style>'''

SCRIPT = r'''<script>
(()=>{'use strict';
const D=window.__RADAR_DATA__||{},TR=D.trades||[],MS=D.markets||[];
const marketById=new Map(MS.map(m=>[m.c,m]));
const $=s=>document.querySelector(s), money=n=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(n||0);
// Kwoty i kursy mozna wpisac takze po polsku (np. 1,70; 1 250,50).
function numericBound(raw,fallback){const clean=String(raw||'').trim().replace(/[\s\u00a0]/g,'').replace(',','.');
 if(!clean)return fallback;
 if(!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(clean))return fallback;
 const n=Number(clean);return Number.isFinite(n)&&n>=0?n:fallback;
}
function activeBounds(){return {amountMin:numericBound($('#min').value,0),amountMax:numericBound($('#max').value,Infinity),oddMin:numericBound($('#oddmin').value,0),oddMax:numericBound($('#oddmax').value,Infinity)}}
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
const hltvResults=D.hltvResults||{};
const allEvents=new Map();
for(const m of MS){let key=eventKey(m),event=allEvents.get(key);if(!event){event={key,title:m.m,slug:m.s,tier:m.tier,markets:[],rows:[]};allEvents.set(key,event)}event.markets.push(m)}
for(const r of TR){const m=marketById.get(r.c);if(!m)continue;let e=allEvents.get(eventKey(m));if(!e){e={key:eventKey(m),title:m.m,slug:m.s,tier:m.tier,markets:[m],rows:[]};allEvents.set(e.key,e)}e.rows.push(r)}
function looksProp(m){const q=(m.q||'').toLowerCase();return /\b(map\s*[1-9]|mapa\s*[1-9]|rounds?|pistol|handicap|spread|total|over|under|first to|correct score|kills|overtime|odd\/even|1st|2nd|3rd)\b/i.test(q) || (m.o.length===2&&m.o.every(x=>/^(yes|no)$/i.test(x)))}
function rankMarket(m,rows){let score=0;if(!looksProp(m))score+=20;if(m.o.length===2)score+=10;if(/\b(vs?\.?|versus)\b/i.test(m.q||''))score+=5;score+=Math.min(10,rows.filter(r=>r.c===m.c).length);return score}
function pickMarket(event,rows){return [...event.markets].sort((a,b)=>rankMarket(b,rows)-rankMarket(a,rows))[0]}
function outcomes(m,rows){const obs=[...new Set(rows.map(r=>r.o))];const set=[...new Set([...(m?.o||[]),...obs].filter(Boolean))];return set.slice(0,8)}
let tab='matches',selected=null,selectedMarket=null,displayLimit=130,exportRows=[];
let pressureMinutes=60, pressureMinUSD=250, pressureMinOdds=1.20;
function marketKind(m){const q=(m.q||'').toLowerCase();if(/\b(map\s*[1-9]|mapa\s*[1-9])\b/i.test(q))return 'map';return looksProp(m)?'other':'winner'}
function filterRows(ignoreStake=false){
 const term=$('#search').value.trim().toLowerCase(),outcome=$('#outcome').value.trim().toLowerCase();
 const {amountMin:min,amountMax:max,oddMin,oddMax}=activeBounds();
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
// ALERT INTERPRETATION: these are OBSERVED BUY fills, not all market trades and not net positions.
// Separate analysis uses unfiltered observations. Excluding low-odds fills from only one side
// would falsely exaggerate a market skew. Odds filter applies to the highlighted side only.
const ANALYSIS_NOW=Number.isFinite(Date.parse(D.generated)) ? Math.floor(Date.parse(D.generated)/1000) : Math.floor(Date.now()/1000);
function weightedOdds(rr){const total=rr.reduce((n,r)=>n+r.a,0);const shares=rr.reduce((n,r)=>n+r.a/r.p,0);return total>0?shares/total:0}
function eventPressure(e,minutes=pressureMinutes,minOdds=pressureMinOdds){
 const winner=pickMarket(e,e.rows);
 if(!winner||marketKind(winner)!=='winner')return null;
 const outs=outcomes(winner,e.rows.filter(r=>r.c===winner.c));
 if(outs.length!==2 || outs.every(o=>/^(yes|no)$/i.test(o)))return null;
 const recentStart=ANALYSIS_NOW-minutes*60,baseStart=recentStart-6*3600;
 const relevant=e.rows.filter(r=>r.c===winner.c && outs.includes(r.o));
 const recent=relevant.filter(r=>r.t>=recentStart && r.t<=ANALYSIS_NOW+60);
 const prior=relevant.filter(r=>r.t>=baseStart && r.t<recentStart);
 if(!recent.length)return null;
 const totals=outs.map(o=>recent.filter(r=>r.o===o).reduce((n,r)=>n+r.a,0));
 const sum=totals[0]+totals[1];if(!sum)return null;
 const index=totals[1]>totals[0]?1:0,top=outs[index],topRows=recent.filter(r=>r.o===top),avgOdds=weightedOdds(topRows);
 if(avgOdds<minOdds)return null;
 const prevT=outs.map(o=>prior.filter(r=>r.o===o).reduce((n,r)=>n+r.a,0));
 const oldSum=prevT[0]+prevT[1],share=totals[index]/sum,baseShare=oldSum>=200?prevT[index]/oldSum:null;
 const delta=baseShare===null?null:share-baseShare;
 const wallets=[...new Set(topRows.filter(r=>r.w).map(r=>r.w))].length;
 const byWallet=new Map();for(const r of topRows){const k=r.w||'nieznany';byWallet.set(k,(byWallet.get(k)||0)+r.a)}
 const walletTop=Math.max(0,...byWallet.values());
 // Ranking is a heuristic for attention, not the probability of manipulation.
 const confidence=recent.length>=3&&wallets>=2?1:0.75;
 const score=Math.round(Math.min(100,Math.max(0,(share-.5)*105+Math.log10(1+sum/100)*19+Math.max(0,delta||0)*65)*confidence));
 return {key:e.key,e,market:winner,top,side:outs,totals,amount:totals[index],sum,share,baseShare,delta,avgOdds,buys:recent.length,sideBuys:topRows.length,wallets,topWalletPct:totals[index]>0?walletTop/totals[index]:0,oldSum,score,minutes};
}
function pressureList(){const items=[];
 for(const e of allEvents.values()){
  if(e.title.startsWith('Nieznany mecz')||eventStatus(e)==='archive')continue;
  if($('#tier').value!=='all'&&e.tier!==$('#tier').value)continue;
  const p=eventPressure(e);if(!p||p.sum<pressureMinUSD)continue;
  if(p.share<.65)continue;
  const needle=$('#search').value.trim().toLowerCase();
  if(needle&&![e.title,leagueName(e),p.top].some(t=>t.toLowerCase().includes(needle)))continue;
  items.push(p)
 }
 return items.sort((a,b)=>b.score-a.score||b.sum-a.sum||b.amount-a.amount)
}
function pressureCard(p,compact=false){
 const item=mk('button','flow-card'+(compact?' compact':''));item.type='button';item.setAttribute('aria-label','Przejdź do meczu: '+p.e.title);
 const lead=mk('div','flow-head');lead.append(mk('strong','',p.e.title));lead.append(mk('span','flow-pct',(p.share*100).toFixed(0)+'%'));
 const course=mk('div','flow-sub',`${p.top} · ${money(p.amount)} na ${money(p.sum)} obserwowanych zakupów · kurs śr. ${p.avgOdds.toFixed(2)}`);
 const b=mk('div','flow-bar');const fill=mk('span');fill.style.width=(p.share*100).toFixed(1)+'%';b.append(fill);
 const notes=mk('div','flow-notes');notes.append(mk('span','',`${p.buys} zakupów · ${p.wallets} portfeli (wybrana strona)`));
 if(p.delta!==null){const change=Math.round(p.delta*100);notes.append(mk('span',change>=20?'flow-rise':'',`Zmiana udziału: ${change>=0?'+':''}${change} pp vs wcześniejsze 6 h`))}
 else notes.append(mk('span','', 'Brak wystarczającego punktu odniesienia (6 h)'));
 if(p.topWalletPct>=.7)notes.append(mk('span','flow-caution','Dominacja jednego portfela'));
 item.append(lead,course,b,notes);item.onclick=()=>openMatch(p.key);return item
}
function showPressure(rows){
 const target=$('#main');target.replaceChildren();const items=pressureList();const panel=mk('section','panel');
 const h=mk('div','panel-title');h.append(mk('h2','','Przechylenia zakupów na drużyny'),mk('span','count',`${items.length} meczów spełniających filtry`));panel.append(h);
 const controls=mk('div','pressure-controls');controls.append(mk('span','small muted','Ostatnie:'));
 const time=mk('select');for(const [v,t] of [['15','15 minut'],['60','60 minut'],['180','3 godziny']]){const op=mk('option','',t);op.value=v;time.append(op)}time.value=String(pressureMinutes);time.onchange=()=>{pressureMinutes=Number(time.value);render()};controls.append(time);
 const minAmountLabel=mk('label','pressure-field');minAmountLabel.append(mk('span','small muted','Zakupy w oknie od (USD)'));
 const vol=mk('input');vol.type='text';vol.inputMode='decimal';vol.autocomplete='off';vol.value=String(pressureMinUSD);vol.placeholder='np. 750';vol.setAttribute('aria-label','Minimalna suma zakupów w oknie w USD');
 vol.onchange=()=>{pressureMinUSD=numericBound(vol.value,0);render()};vol.onkeydown=e=>{if(e.key==='Enter')vol.blur()};minAmountLabel.append(vol);controls.append(minAmountLabel);
 const minOddsLabel=mk('label','pressure-field');minOddsLabel.append(mk('span','small muted','Średni kurs strony od'));
 const odds=mk('input');odds.type='text';odds.inputMode='decimal';odds.autocomplete='off';odds.value=Number(pressureMinOdds).toFixed(2);odds.placeholder='np. 1,70';odds.setAttribute('aria-label','Minimalny średni kurs dominującej strony');
 odds.onchange=()=>{pressureMinOdds=numericBound(odds.value,1);render()};odds.onkeydown=e=>{if(e.key==='Enter')odds.blur()};minOddsLabel.append(odds);controls.append(minOddsLabel);
 panel.append(controls);
 panel.append(mk('p','flow-note','Przechylenie = udział USD zakupów BUY na jedną drużynę w wybranym oknie. Porównanie z wcześniejszymi 6 h pojawia się tylko przy co najmniej 200 USD wcześniejszych obserwacji. To NIE jest udział w całym rynku Polymarket ani saldo pozycji. Filtry pojedynczych transakcji i kursów z górnego paska nie zmieniają podstawy obliczeń (żeby nie zniekształcać obu stron).'));
 const list=mk('div','flow-grid');for(const p of items.slice(0,100))list.append(pressureCard(p));panel.append(list);
 if(!items.length)panel.append(mk('div','empty','Brak wystarczających nowych zakupów w tym oknie. Spróbuj 3 godzin albo niższego minimum.'));
 target.append(panel);exportRows=rows;
}

// HLTV-style match schedule, powered only by available Gamma timestamps and recorded buys.
const timeFmt=new Intl.DateTimeFormat('pl-PL',{timeZone:'Europe/Warsaw',hour:'2-digit',minute:'2-digit'});
const dayFmt=new Intl.DateTimeFormat('pl-PL',{timeZone:'Europe/Warsaw',weekday:'long',day:'numeric',month:'long'});
const dateFmt=new Intl.DateTimeFormat('pl-PL',{timeZone:'Europe/Warsaw',year:'numeric',month:'2-digit',day:'2-digit'});
function parsedTeams(title){
  const core=String(title||'').replace(/^(?:Counter-Strike(?:\s*2)?|CS2):\s*/i,'');
  const hit=core.match(/^(.+?)\s+(?:vs\.?|versus)\s+(.+?)(?=\s*\(BO[135]\)|\s+-\s+|$)/i);
  if(!hit)return [core.slice(0,100),''];
  return [hit[1].trim().slice(0,80),hit[2].trim().slice(0,80)];
}
function leagueName(e){
  const direct=String(e.league||'').trim();if(direct)return direct;
  const t=e.title||'';const parts=t.split(/\s+-\s+/);return parts.length>1?parts.slice(1).join(' - ').slice(0,95):'Turniej nieustalony';
}
function eventStatus(e){
  const now=Date.now()/1000;
  if(e.hltv?.status==='finished'||e.hltv?.status==='archive')return 'archive';
  if(e.hltv?.status==='live' && e.hltv.observedAt && now-e.hltv.observedAt<1200)return 'live';
  if(e.hltv?.status==='upcoming' && e.hltv.observedAt && now-e.hltv.observedAt<1200)return 'upcoming';
  if(e.closed)return 'archive';
  if(!e.start)return 'undated';
  if(e.start>now)return 'upcoming';
  // Conservative backup when HLTV cannot confirm the end of a match.
  // Do not keep old BO3 matches in the active list for eight hours.
  const type=String(e.title||'').match(/\bBO([135])\b/i);
  const maxHours=type?.[1]==='1'?2.5:type?.[1]==='5'?7:4.5;
  return now-e.start<=maxHours*3600?'started':'archive';
}
function getRegion(e){const text=(leagueName(e)+' '+e.title).toLowerCase();
 if(/south america|latam|brazil|brazyl|argentin/.test(text))return 'Ameryka Płd.';
 if(/north america|\busa\b|united states|canada/.test(text))return 'Ameryka Płn.';
 if(/asia|china|korea|oceania|australia|pacific/.test(text))return 'Azja / Oceania';
 if(/europe|europa|european|poland|polska|nordic|sweden|france|germany/.test(text))return 'Europa';
 return 'Nieustalony';
}
for(const e of allEvents.values()){
 const starts=e.markets.map(m=>Number(m.start)||0).filter(t=>t>0);
 e.start=starts.length?Math.min(...starts):0;
 e.closed=e.markets.every(m=>!!m.closed);
 e.league=e.markets.map(m=>m.league||'').find(Boolean)||'';
 const known=e.markets.map(m=>m.volume).filter(v=>typeof v==='number'&&Number.isFinite(v));
 e.volume=known.length?Math.max(...known):null;
 e.teams=parsedTeams(e.title);
 e.region=getRegion(e);
  e.hltv=hltvResults[e.key]||null;
  if(e.hltv && ['live','upcoming'].includes(e.hltv.status) && e.hltv.start>0
    && (!e.start || Math.abs(e.hltv.start-e.start)<18*3600))e.start=e.hltv.start;
}
let stageChoice='active',leagueChoice='all',regionChoice='all',onlyWithTrades=false,minPMVolume=100;
function fixtureRow(x){
 const {e,rr}=x, m=pickMarket(e,rr),main=rr.filter(r=>r.c===m?.c),side=getSides(m,main);
 const sideA=side.find(s=>s.o.toLowerCase()===e.teams[0].toLowerCase())||side[0];
 const sideB=side.find(s=>s.o.toLowerCase()===e.teams[1].toLowerCase())||side.find(s=>s.o!==sideA?.o);
 const row=mk('button','fixture-row');row.type='button';row.setAttribute('aria-label',`Otwórz szczegóły meczu ${e.title}`);
 const t=mk('div','fixture-time');
 const stage=eventStatus(e);
 const when=e.start?timeFmt.format(e.start*1000):'—:—';
 t.append(mk('div','fixture-clock',when));
 t.title=e.start?'Planowane rozpoczęcie: '+dt(e.start)+' (czas polski)'+(e.hltv&&['live','upcoming'].includes(e.hltv.status)?' · HLTV':' · Polymarket'):'Brak godziny rozpoczęcia';
 const statusLabel=stage==='live'?'LIVE':stage==='archive'&&e.hltv?.status==='finished'?'KONIEC':stage==='started'?'PO STARCIE':stage==='upcoming'?'PLAN':stage==='archive'?'ARCHIWUM':'BEZ GODZ.';
  t.append(mk('div','fixture-when '+stage,statusLabel));
 const teams=mk('div','fixture-teams');
 const a=mk('div','fixture-team',e.teams[0]||e.title),b=mk('div','fixture-team',e.teams[1]||'Drugi zespół nieustalony');
 teams.append(a,b);
  if(e.hltv?.status==='finished'&&Array.isArray(e.hltv.score)&&e.hltv.score.length===2)teams.append(mk('div','fixture-result',`${e.hltv.score[0]} : ${e.hltv.score[1]} · HLTV`));
 const meta=mk('div','fixture-meta');meta.append(mk('div','fixture-league',leagueName(e)));const tags=mk('div','fixture-tags');
 tags.append(pill(e.tier));const bo=(e.title||'').match(/\bBO[135]\b/i);if(bo)tags.append(mk('span','tiny-label',bo[0].toUpperCase()));
 tags.append(mk('span','tiny-label',`${e.markets.length} rynków`));const pres=eventPressure(e);if(pres&&pres.sum>=pressureMinUSD&&pres.share>=.65){tags.append(mk('span','flow-fixture-tag',`↑ ${(pres.share*100).toFixed(0)}% → ${pres.top} (${money(pres.amount)})`))}meta.append(tags);
 const rvalue=mk('div','fixture-sides');
 const v1=mk('div','side-value',sideA?.fills?money(sideA.total):'—');
 const v2=mk('div','side-value',sideB?.fills?money(sideB.total):'—');
 rvalue.append(child(mk('div','side-stack'),mk('span','side-caption','Zakupy · '+(e.teams[0]||sideA?.o||'strona A')),v1),child(mk('div','side-stack'),mk('span','side-caption','Zakupy · '+(e.teams[1]||sideB?.o||'strona B')),v2));
 const sum=mk('div','fixture-volume');sum.append(mk('div','fixture-total money',e.volume===null?'Brak obrotu':money(e.volume)),mk('div','small muted','Obrót Polymarket · wszystkie rynki'),mk('div','small muted',`Radar: ${money(x.volume)} · ${rr.length} zakupów`));
 const best=rr.length?Math.max(...rr.map(r=>1/r.p)):0;
 const odds=mk('div','fixture-odds',best?`max ${best.toFixed(2)}`:'—');odds.title='Najwyższy kurs zaobserwowanego zakupu w zastosowanych filtrach; nie jest to obecny kurs';
 row.append(t,teams,meta,rvalue,sum,odds,mk('span','fixture-arrow','›'));
 row.onclick=()=>openMatch(e.key);
 return row;
}
function showMatches(rows){
 const target=$('#main');target.replaceChildren();
 const byEvent=new Map();for(const r of rows){const m=marketById.get(r.c);if(!m)continue;const key=eventKey(m);if(!byEvent.has(key))byEvent.set(key,[]);byEvent.get(key).push(r)}
 const matches=[];
 for(const e of allEvents.values()){
  if(!e.markets.length||e.title.startsWith('Nieznany mecz'))continue;
  if(e.volume!==null && e.volume<minPMVolume)continue;
  const rr=byEvent.get(e.key)||[];
  // Jesli filtrowane jest konkretne obstawienie lub portfel, nie pokazuj pustych meczow.
  const deepSearch=!!$('#outcome').value.trim()||(/0x[a-f0-9]{4,}/i).test($('#search').value)||activeBounds().amountMin>0||!!$('#max').value||!!$('#oddmax').value||($('#oddmin').value.trim()!=='1.20'&&!!$('#oddmin').value.trim());
  if((deepSearch||onlyWithTrades)&&!rr.length)continue;
  const eventStage=eventStatus(e);
  if(stageChoice==='active'&&eventStage==='archive')continue;
  if(stageChoice!=='all'&&stageChoice!=='active'&&eventStage!==stageChoice)continue;
  if(leagueChoice!=='all'&&leagueName(e)!==leagueChoice)continue;
  if(regionChoice!=='all'&&e.region!==regionChoice)continue;
  if($('#tier').value!=='all'&&e.tier!==$('#tier').value)continue;
  const query=$('#search').value.toLowerCase().trim();if(query && ![e.title,leagueName(e),e.teams.join(' ')].some(v=>v.toLowerCase().includes(query)) && !rr.length)continue;
  if($('#marketKind').value!=='all'&&!e.markets.some(m=>marketKind(m)===$('#marketKind').value))continue;
  if(eventStatus(e)==='archive' && !e.closed && !rr.length)continue; // brak danych historycznych
  matches.push({e,rr,volume:rr.reduce((a,r)=>a+r.a,0),last:rr.length?Math.max(...rr.map(r=>r.t)):0});
 }
 // Pasek narzedzi: statusy + liga + region; bez wplywu na inne zakladki.
 const panel=mk('div','fixture-layout');const sidebar=mk('aside','fixture-sidebar');
 sidebar.append(mk('div','side-heading','FILTRY MECZÓW'));
 const choices=[['active','Aktualne'],['live','LIVE (HLTV)'],['upcoming','Nadchodzące'],['started','Po starcie*'],['archive','Archiwum'],['undated','Bez terminu'],['all','Wszystkie']];
 for(const [key,label] of choices){const btn=mk('button','fixture-stage'+(stageChoice===key?' active':''),label);btn.type='button';
  btn.onclick=()=>{stageChoice=key;displayLimit=160;render()};sidebar.append(btn)}
 const toggle=mk('label','fixture-check');const cb=mk('input');cb.type='checkbox';cb.checked=onlyWithTrades;cb.onchange=()=>{onlyWithTrades=cb.checked;render()};toggle.append(cb,mk('span','','Tylko mecze z zakupami'));sidebar.append(toggle);
 sidebar.append(mk('div','side-heading gap-above','OBRÓT POLYMARKET'));
 const vol=mk('select','fixture-select');for(const [value,label] of [['0','Bez minimum'],['100','Min. 100 USD'],['250','Min. 250 USD'],['500','Min. 500 USD'],['1000','Min. 1 000 USD']]){const op=mk('option','',label);op.value=value;vol.append(op)}
 vol.value=String(minPMVolume);vol.onchange=()=>{minPMVolume=Number(vol.value);render()};sidebar.append(vol);
 sidebar.append(mk('p','fixture-aside-note','Kwota dotyczy całkowitego obrotu na Polymarket, a nie wyłącznie zakupów zarejestrowanych przez radar. Nieznany obrót nie jest wykluczany.'));
 sidebar.append(mk('div','side-heading gap-above','TURNIEJ'));
 const ligues=[...new Set([...allEvents.values()].map(leagueName))].sort((a,b)=>a.localeCompare(b,'pl'));
 const select=mk('select','fixture-select');const optAll=mk('option','', 'Wszystkie turnieje');optAll.value='all';select.append(optAll);
 for(const lg of ligues){const op=mk('option','',lg);op.value=lg;select.append(op)}select.value=leagueChoice;select.onchange=()=>{leagueChoice=select.value;render()};sidebar.append(select);
 sidebar.append(mk('div','side-heading gap-above','REGION (Z NAZWY)'));
 const reg=mk('select','fixture-select');for(const lg of ['all','Europa','Ameryka Płd.','Ameryka Płn.','Azja / Oceania','Nieustalony']){const op=mk('option','',lg==='all'?'Wszystkie regiony':lg);op.value=lg;reg.append(op)}reg.value=regionChoice;reg.onchange=()=>{regionChoice=reg.value;render()};sidebar.append(reg);
 sidebar.append(mk('p','fixture-aside-note','Status LIVE i dostępne terminy są sprawdzane na HLTV /matches. „Po starcie” oznacza jedynie upływ godziny z Polymarket bez potwierdzenia LIVE.'));
 const content=mk('section','fixture-content');const top=mk('div','fixture-main-head');top.append(child(mk('div'),mk('h2','','Mecze Counter-Strike 2'),mk('div','small muted',`${matches.length} spotkań • obrót PM ≥ ${money(minPMVolume)} (gdy znany) • godziny PL`)));
 const meta=mk('span','fixture-count',`${matches.filter(x=>x.rr.length).length} z zakupami`);top.append(meta);content.append(top);
 const missing=[...allEvents.values()].filter(e=>e.title.startsWith('Nieznany mecz')&&e.rows.length).length;
 if(missing)content.append(mk('p','small muted fixture-legacy',`Starsze wpisy bez nazw spotkań (${missing} rynków) znajdziesz w zakładce Transakcje.`));
 const pressure=pressureList().filter(x=>eventStatus(x.e)!=='archive').slice(0,4);if(pressure.length){const box=mk('div','flow-teaser');const h=mk('div','flow-teaser-title');h.append(mk('strong','','↗ Największe przechylenia zakupów ('+pressureMinutes+' min)'));const jump=mk('button','show-all','Pełny ranking →');jump.onclick=()=>{tab='pressure';render()};h.append(jump);box.append(h);const items=mk('div','flow-teaser-grid');for(const x of pressure)items.append(pressureCard(x,true));box.append(items);content.append(box)}
 const order=['live','started','upcoming','undated','archive'];const title={live:'Mecze na żywo · HLTV',started:'Po planowanym rozpoczęciu (niezweryfikowane)',upcoming:'Nadchodzące mecze',undated:'Mecze bez potwierdzonej godziny',archive:'Archiwum spotkań'};
 const buckets=new Map(order.map(k=>[k,[]]));for(const x of matches)buckets.get(eventStatus(x.e)).push(x);
 for(const [key,group] of buckets){if(!group.length)continue;const mode=$('#sort').value;
 group.sort((a,b)=>{if(mode==='pressure')return (eventPressure(b.e)?.score||0)-(eventPressure(a.e)?.score||0)||b.volume-a.volume;
  if(key==='upcoming')return (a.e.start||0)-(b.e.start||0);
   if(key==='live')return (a.e.start||0)-(b.e.start||0);
 if(key==='started')return (b.e.start||0)-(a.e.start||0);
 if(key==='archive')return (b.e.start||b.last)-(a.e.start||a.last);
 return mode==='newest'?b.last-a.last:mode==='odds'?Math.max(0,...b.rr.map(r=>1/r.p))-Math.max(0,...a.rr.map(r=>1/r.p)):b.volume-a.volume});
 const heading=mk('div','fixtures-section-head');heading.append(mk('h3','',title[key]),mk('span','count',group.length+' spotkań'));content.append(heading);
 let shown=0,lastDay='';const list=mk('div','fixture-list');
 for(const x of group){if(shown>=displayLimit)break;shown++;
 const day=x.e.start?dateFmt.format(x.e.start*1000):'';if(day&&day!==lastDay){lastDay=day;const h=mk('div','fixture-day',dayFmt.format(x.e.start*1000));list.append(h)}
 list.append(fixtureRow(x))}
 content.append(list);
 if(group.length>displayLimit){const more=mk('button','btn show-more',`Pokaż pozostałe (${group.length-shown})`);more.onclick=()=>{displayLimit+=150;render()};content.append(more)}
 }
 if(!matches.length)content.append(mk('div','empty','Brak meczów spełniających wybrane filtry. Zmień zakres lub włącz wszystkie kursy.'));
 panel.append(sidebar,content);target.append(panel);exportRows=rows;
}
function openMatch(key){selected=key;selectedMarket=null;window.location.hash='match='+encodeURIComponent(key);tab='detail';render()}
function closeDetail(){selected=null;selectedMarket=null;window.location.hash='';tab='matches';render()}
function showDetail(rows){const event=allEvents.get(selected);if(!event){closeDetail();return}const eRows=rows.filter(r=>eventKey(marketById.get(r.c))===selected);const target=$('#main');target.replaceChildren();const p=mk('div','panel detail');const h=mk('div','detail-head');const left=mk('div');const back=mk('button','back','← Wróć do meczów');back.onclick=closeDetail;left.append(back, mk('h2','',event.title));left.append(mk('div','detail-meta',`${eRows.length} zakupów w filtrze • ${event.markets.length} rynków • niepełna obserwacja ≥ 100 USD`));h.append(left);h.append(aLink('Polymarket ↗',matchUrl(event.slug)));p.append(h);const attention=eventPressure(event);if(attention&&attention.sum>=pressureMinUSD){const note=mk('div','subsection');note.append(mk('h3','','Przechylenie obserwowanych zakupów · '+pressureMinutes+' min'));note.append(pressureCard(attention));p.append(note)}
 if(event.hltv?.status==='finished'&&Array.isArray(event.hltv.score)){
   const score=mk('div','subsection small muted',`Wynik HLTV: ${event.hltv.score.join(':')} · mecz zakończony`);
   if(/^https:\/\/www\.hltv\.org\/matches\//.test(event.hltv.url||''))score.append(document.createTextNode(' · '),aLink('HLTV ↗',event.hltv.url));p.append(score);
  }
  if(event.hltv && ['live','upcoming'].includes(event.hltv.status) && /^https:\/\/www\.hltv\.org\/matches\/\d+\//.test(event.hltv.url||'')){
   const source=mk('div','subsection small muted','Status: '+(event.hltv.status==='live'?'LIVE':'Nadchodzący')+' · HLTV /matches');
   source.append(document.createTextNode(' · '),aLink('Otwórz w HLTV ↗',event.hltv.url));p.append(source);
  }
  const ctrl=mk('div','detail-controls');ctrl.append(mk('strong','','Rynek:'));const sel=mk('select');const best=pickMarket(event,eRows);for(const m of event.markets){const o=mk('option','',m.q||'Rynek bez nazwy');o.value=m.c;sel.append(o)}if(!selectedMarket||!event.markets.some(m=>m.c===selectedMarket))selectedMarket=best?.c||event.markets[0]?.c;sel.value=selectedMarket;sel.onchange=()=>{selectedMarket=sel.value;showDetail(filterRows())};ctrl.append(sel,pill(event.tier));p.append(ctrl);
 const chosen=marketById.get(selectedMarket),rr=eRows.filter(r=>r.c===selectedMarket),sides=getSides(chosen,rr),sum=rr.reduce((a,r)=>a+r.a,0);const s=mk('div','sides');for(let i=0;i<Math.max(2,sides.length);i++){const side=sides[i]||{o:'Druga strona — brak danych',total:0,fills:0,wallets:0};s.append(sideCard(side,sum,i))}p.append(s);
 const desc=mk('div','subsection small muted',`Wartości to suma zaobserwowanych zakupów BUY w wybranym rynku i czasie. Nie uwzględniamy sprzedaży, mniejszych zleceń ani brakujących danych API. Zmieniając rynek, możesz przejrzeć także mapy i inne typy zakładów.`);p.append(desc);
 const two=mk('div','subsection');two.append(mk('h3','','Portfele — obie strony rynku'));const box=mk('div','details-tables');for(let i=0;i<Math.min(sides.length,2);i++){const side=sides[i],wallets=new Map();for(const r of rr.filter(r=>r.o===side.o)){const key=r.w||'Brak portfela';let obj=wallets.get(key);if(!obj){obj={w:key,amount:0,fills:0,last:0,n:r.n};wallets.set(key,obj)}obj.amount+=r.a;obj.fills++;obj.last=Math.max(obj.last,r.t)}const v=[...wallets.values()].sort((a,b)=>b.amount-a.amount);const wrap=mk('div','panel'),title=mk('div','panel-title');child(title,mk('strong','',side.o),mk('span','small muted',`${v.length} portfeli`));wrap.append(title);const sc=mk('div','table-scroll compact'),t=mk('table'),thead=mk('thead'),tr=mk('tr');['Portfel','Zakupy','Liczba'].forEach(h=>child(tr,mk('th','',h)));thead.append(tr);t.append(thead);const tb=mk('tbody');for(const x of v.slice(0,25)){const r=mk('tr');td(r,x.w==='Brak portfela'?x.w:aLink(x.n||short(x.w),walletUrl(x.w)));td(r,money(x.amount),'money');td(r,x.fills);tb.append(r)}t.append(tb);sc.append(t);wrap.append(sc);if(!v.length)wrap.append(mk('div','empty','Brak zakupów tej strony w obserwowanych danych.'));box.append(wrap)}two.append(box);p.append(two);
 const trans=mk('div','subsection');trans.append(mk('h3','','Wszystkie zaobserwowane zakupy w tym rynku'));const sc=mk('div','table-scroll'),table=tradeTable(rr,'detail-winner');sc.append(table);trans.append(sc);if(!rr.length)trans.append(mk('div','empty','Brak zakupów w tym rynku dla wybranych filtrów.'));if(rr.length>500)trans.append(mk('div','small muted',`Widocznych 500 z ${rr.length} zapisów. Pozostałe pobierz przez Eksport CSV.`));p.append(trans);
 const all=mk('div','subsection');all.append(mk('h3','',`Wszystkie rynki meczu — ${eRows.length} zakupów`));all.append(mk('div','small muted','Poniżej także zakupy na mapy i inne wyniki. Wiersze mają nazwę rynku, aby nie mieszać ich ze zwycięzcą meczu.'));const wrapAll=mk('div','table-scroll');wrapAll.append(tradeTable(eRows,'detail-all'));all.append(wrapAll);if(eRows.length>500)all.append(mk('div','small muted',`Widocznych 500 z ${eRows.length} zapisów. Pozostałe pobierz przez Eksport CSV.`));p.append(all);target.append(p);exportRows=eRows;
}
const TRADE_COLS=[
 {id:'t',label:'Czas (PL)',field:r=>r.t,numeric:true},
 {id:'m',label:'Mecz / rynek',field:r=>{const m=marketById.get(r.c)||{};return `${m.m||''} ${m.q||''}`}},
 {id:'o',label:'Strona',field:r=>r.o},
 {id:'a',label:'Zakup',field:r=>r.a,numeric:true},
 {id:'k',label:'Kurs (≈)',field:r=>1/r.p,numeric:true},
 {id:'w',label:'Portfel',field:r=>r.n||r.w}
];
const tradeTableSorts=new Map();
function sortedTrades(rows,key){
 const state=tradeTableSorts.get(key)||{id:'t',dir:'desc'};
 const col=TRADE_COLS.find(c=>c.id===state.id)||TRADE_COLS[0],factor=state.dir==='asc'?1:-1;
 return [...rows].sort((a,b)=>{
  const aa=col.field(a),bb=col.field(b);
  const comp=col.numeric?Number(aa)-Number(bb):String(aa||'').localeCompare(String(bb||''),'pl',{sensitivity:'base'});
  return (comp*factor)||(b.t-a.t)||(String(a.id||'').localeCompare(String(b.id||'')));
 });
}
function tradeTable(rows,key='all-trades'){
 const table=mk('table');const header=mk('tr');const state=tradeTableSorts.get(key)||{id:'t',dir:'desc'};
 for(const col of TRADE_COLS){const th=mk('th');const active=state.id===col.id;th.setAttribute('aria-sort',active?(state.dir==='asc'?'ascending':'descending'):'none');
  const btn=mk('button','sort-head');btn.type='button';btn.setAttribute('aria-pressed',active?'true':'false');
  btn.append(mk('span','',col.label),mk('span','sort-arrow',active?(state.dir==='asc'?'↑':'↓'):'↕'));
  btn.title=`Sortuj według: ${col.label}`;
  btn.onclick=()=>{const old=tradeTableSorts.get(key)||{id:'t',dir:'desc'};
    const dir=old.id===col.id?(old.dir==='desc'?'asc':'desc'):(col.numeric?'desc':'asc');
    tradeTableSorts.set(key,{id:col.id,dir});render()};th.append(btn);header.append(th)
 }
 table.append(child(mk('thead'),header));const tb=mk('tbody');
 for(const r of sortedTrades(rows,key).slice(0,500)){
  const m=marketById.get(r.c);if(!m)continue;const tr=mk('tr');td(tr,dt(r.t));
  const wrap=mk('div');child(wrap,mk('div','',m.m),mk('div','secondary',m.q));td(tr,wrap);td(tr,r.o);td(tr,money(r.a),'money');td(tr,(1/r.p).toFixed(2));td(tr,r.w?aLink(r.n||short(r.w),walletUrl(r.w)):'—');tb.append(tr)
 }
 table.append(tb);return table
}
function showTrades(rows){const target=$('#main');target.replaceChildren();const panel=mk('section','panel'),h=mk('div','panel-title');child(h,mk('h2','','Zaobserwowane zakupy'),mk('span','count',`${rows.length} transakcji, pierwsze 500 poniżej`));panel.append(h);const wrap=mk('div','table-scroll');wrap.append(tradeTable(rows,'all-trades'));panel.append(wrap);if(!rows.length)panel.append(mk('div','empty','Brak transakcji w wybranym filtrze.'));target.append(panel);exportRows=rows}
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
function render(){const rows=sortRows(filterRows(tab==='positions'));statCalc(rows);for(const b of document.querySelectorAll('[data-tab]'))b.classList.toggle('active',b.dataset.tab===(tab==='detail'?'matches':tab));displayLimit=Math.max(130,displayLimit);if(tab==='detail')showDetail(rows);else if(tab==='trades')showTrades(rows);else if(tab==='wallets')showWallets(rows);else if(tab==='positions')showPositions(rows);else if(tab==='pressure')showPressure(rows);else{exportRows=rows;showMatches(rows)}}
for(const tabButton of document.querySelectorAll('[data-tab]'))tabButton.onclick=()=>{tab=tabButton.dataset.tab;selected=null;selectedMarket=null;location.hash='';displayLimit=130;render()};
for(const name of ['#search','#outcome','#min','#max','#oddmin','#oddmax','#marketKind','#sort','#range','#tier'])$(name).addEventListener('input',()=>{displayLimit=130;render()});
$('#reset-bounds').onclick=()=>{ $('#min').value='';$('#max').value='';$('#oddmin').value='1.20';$('#oddmax').value='';displayLimit=130;render()};
$('#export').onclick=exportCsv;const scan=Number(D.lastScan||0);$('#stamp').textContent=dt(Math.floor(Date.parse(D.generated)/1000));$('#scan').textContent=scan?dt(scan):'brak potwierdzenia';
if(location.hash.startsWith('#match=')){const key=decodeURIComponent(location.hash.slice(7));if(allEvents.has(key)){selected=key;tab='detail'}}render();
})();
</script>'''


def build_page(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    html = '''<!doctype html><html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#080d16"><title>CS2 Polymarket Radar v1.11 — analiza przechyleń</title>'''
    html += STYLE + '''</head><body><div class="shell">
<header class="brandbar"><div class="brand"><div class="logo" aria-hidden="true">◎</div><div><h1>CS2 Radar <span class="muted">/ Analiza v1.11</span></h1><div class="small muted">Monitoring napływu zakupów na drużyny · Tier 2/3</div></div></div><div class="status"><span class="bullet"></span>Eksport po skanie GitHub Actions</div></header>
<div class="note"><b>Uwaga:</b> pokazujemy tylko zaobserwowane BUY od progu API (zwykle 100 USD). Kurs na stronie to kurs zaobserwowanego zakupu, nie aktualna oferta. Kwoty i kursy można filtrować po dowolnej wpisanej wartości (np. 500 USD i 1,70). Domyślny filtr pojedynczych zakupów to ≥ 1.20. Analiza przechyleń uwzględnia obie strony bez tego filtra, aby uniknąć sztucznej przewagi. Obrót Polymarket to łączna wartość handlu w wydarzeniu; suma zakupów radaru to osobna, niepełna obserwacja. Rynki ze znanym obrotem poniżej 100 USD są domyślnie wyłączone.</div>
<section class="kpis"><div class="kpi"><div class="hint">Zakupy w filtrze</div><div id="nTrades" class="big">—</div></div><div class="kpi"><div class="hint">Łączna wartość brutto</div><div id="nUSD" class="big">—</div></div><div class="kpi"><div class="hint">Mecze z aktywnością</div><div id="nGames" class="big">—</div></div><div class="kpi"><div class="hint">Portfele</div><div id="nWallets" class="big">—</div></div></section>
<nav class="tabs" aria-label="Widoki panelu"><button type="button" class="tab active" data-tab="matches">⚔ Mecze</button><button type="button" class="tab" data-tab="pressure">↗ Przechylenia</button><button type="button" class="tab" data-tab="trades">▤ Transakcje</button><button type="button" class="tab" data-tab="positions">▥ Pozycje portfeli</button><button type="button" class="tab" data-tab="wallets">♙ Portfele</button></nav>
<div class="toolbar"><input id="search" type="search" placeholder="Mecz, drużyna, turniej, portfel…" aria-label="Szukaj">
<select id="range" aria-label="Zakres czasu"><option value="1">Ostatnie 24 h</option><option value="7" selected>Ostatnie 7 dni</option><option value="30">Ostatnie 30 dni</option></select>
<select id="tier" aria-label="Poziom turnieju"><option value="all">Wszystkie tiery</option><option value="lower">Niższy tier</option><option value="unknown">Nieustalony</option><option value="major">Duże turnieje</option></select>
<select id="marketKind" aria-label="Typ rynku"><option value="all">Wszystkie typy rynku</option><option value="winner">Zwycięzca meczu</option><option value="map">Zakłady na mapy</option><option value="other">Inne zakłady</option></select>
<div class="range-group" role="group" aria-label="Własny zakres kwoty zakupu"><span class="range-heading">ZAKUP (USD)</span>
<label class="range-field"><span>Od</span><input id="min" type="text" inputmode="decimal" autocomplete="off" placeholder="np. 500" aria-label="Minimalny zakup w USD"></label>
<label class="range-field"><span>Do</span><input id="max" type="text" inputmode="decimal" autocomplete="off" placeholder="bez limitu" aria-label="Maksymalny zakup w USD"></label></div>
<div class="range-group" role="group" aria-label="Własny zakres kursu"><span class="range-heading">KURS (≈)</span>
<label class="range-field"><span>Od</span><input id="oddmin" type="text" inputmode="decimal" autocomplete="off" value="1.20" placeholder="np. 1,70" aria-label="Minimalny kurs"></label>
<label class="range-field"><span>Do</span><input id="oddmax" type="text" inputmode="decimal" autocomplete="off" placeholder="bez limitu" aria-label="Maksymalny kurs"></label></div>
<button id="reset-bounds" type="button" title="Wyczyść zakresy; minimalny kurs wróci do 1.20">↺ Reset zakresów</button>
<input id="outcome" type="search" placeholder="Wynik / drużyna…" aria-label="Filtr obstawionego wyniku">
<select id="sort" aria-label="Sortowanie"><option value="newest" selected>Sortuj: najnowsze</option><option value="largest">Sortuj: największe kwoty</option><option value="odds">Sortuj: najwyższe kursy</option><option value="pressure">Sortuj mecze: przechylenie</option></select>
<button id="export" type="button">↓ Eksport CSV</button></div>
<main id="main" aria-live="polite"></main>
<footer><p>Ostatni pełny skan: <b id="scan"></b> · Wygenerowano: <b id="stamp"></b> (czas polski). Odśwież stronę, aby zobaczyć najnowszy opublikowany raport.</p><p>Godziny spotkań są planowanymi terminami z Gamma API (gameStartTime / startTime), wyświetlanymi w strefie Europe/Warsaw, jeśli dostępne. Nigdy nie używamy daty wystawienia rynku jako godziny meczu. „LIVE” pochodzi z HLTV /matches; „Po starcie” nie potwierdza gry LIVE. „Koniec” oznacza wynik dopasowany z HLTV /results. Gdy brak potwierdzenia HLTV, niektóre mecze trafiają do archiwum na podstawie czasu (BO1 po 2,5 h, BO3 po 4,5 h, BO5 po 7 h) — to ostrożna heurystyka, nie oficjalny wynik. Porównanie stron rynku opiera się wyłącznie na zarejestrowanych BUY: obie strony mogą nie mieć równych danych. Podział na niższe tiery jest heurystyczny. Transakcje bez pełnych metadanych mogą znajdować się w osobnej grupie „Nieznany mecz”.</p><p>Przechylenie opisuje wyłącznie zaobserwowane zakupy, nie łączny obrót, prawdopodobieństwo wyniku ani podejrzenie manipulacji. Panel jest publiczny w GitHub Pages. Nie publikuje tokenu Telegram ani sekretów GitHuba.</p></footer></div>
<script>window.__RADAR_DATA__=''' + raw + ''';</script>''' + SCRIPT + '''</body></html>'''
    return html


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',type=Path,default=ROOT/'radar.sqlite3')
    parser.add_argument('--cache',type=Path,default=ROOT/'market_cache.json')
    parser.add_argument('--out',type=Path,default=ROOT/'public'/'index.html')
    parser.add_argument('--hltv-cache',type=Path,default=ROOT/'hltv_results_cache.json')
    parser.add_argument('--hltv-matches-cache',type=Path,default=ROOT/'hltv_matches_cache.json')
    args=parser.parse_args()
    data=fetch_data(args.db,args.cache,hltv_cache_path=args.hltv_cache,hltv_matches_cache_path=args.hltv_matches_cache)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(build_page(data),encoding='utf-8')
    print(f"Panel wygenerowany: {args.out}, {len(data['trades'])} transakcji, {len(data['markets'])} rynków")

if __name__=='__main__':main()
