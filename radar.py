#!/usr/bin/env python3
"""CS2 Polymarket Radar - read-only, public-API trade monitoring."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

import requests

from market_flow import assess_market_flow, BASELINE_SECONDS

BASE = Path(__file__).resolve().parent
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
TG = "https://api.telegram.org"
LOG = logging.getLogger("cs2-radar")

LOW_LEAGUE = [
    "cct", "esea", "esl challenger league", "esl challenger cup", "united21",
    "european pro league", "exort", "open qualifier", "closed qualifier",
    "academy", "qualifier", "qualifications", "regional", "dust2", "lpl",
    "res showdown", "hellcase", "yaLLa compass", "epl world series", "epl season",
    "esl impact", "thunderpick qualifier", "a1 gaming", "ecl europe cup",
]
HIGH_LEAGUE = [
    "iem ", "intel extreme masters", "blast premier", "blast rivals", "blast bounty",
    "esl pro league", "pgl major", "starladder major", "esports world cup",
    "pgl astana", "pgl bucharest", "pgl cluj", "pgl singapore",
]
CS2 = re.compile(r"(counter[\s-]?strike|\bcs2\b|\bcs:go\b)", re.I)
MATCH = re.compile(r"\b(?:vs\.?|versus)\b", re.I)
VALID_CONDITION = re.compile(r"^0x[0-9a-fA-F]{64}$")

DEFAULT = {
    "poll_seconds": 25,
    "discover_seconds": 600,
    "minimum_event_volume_usd": 100,  # calkowity obrot wydarzenia na Polymarket
    "minimum_buy_usd": 1000,
    "absolute_whale_usd": 5000,
    "relative_market_volume": 0.25,
    "low_volume_market_usd": 5000,
    "api_minimum_trade_usd": 100,
    # Alerty w stylu pokazanego profilu: mniejsze zakupy na niszowych meczach
    # oraz suma kilku zakupow tego samego portfela na ten sam wynik.
    "single_buy_lower_usd": 500,
    "single_buy_unknown_usd": 1000,
    "position_lower_usd": 900,
    "position_unknown_usd": 1700,
    "position_window_seconds": 21600,  # 6 godzin, zakupy BRUTTO a nie aktualna pozycja
    "position_24h_lower_usd": 1500,
    "position_24h_unknown_usd": 3500,
    "position_24h_window_seconds": 86400,
    "diagnostic_trade_logs": True,
    "daily_summary_enabled": True,
    "daily_summary_hour_local": 21,
    "daily_summary_top_n": 10,

    "wallet_cluster_usd": 2500,
    "multi_wallet_cluster_usd": 3000,
    "multi_wallet_min_wallets": 3,
    "cluster_window_seconds": 600,
    "alert_cooldown_seconds": 1800,
    "lookback_seconds": 300,
    "max_pages_per_batch": 12,
    "max_events": 3000,
    "max_market_volume_unknown_usd": 40000,
    "exclude_major_tournaments": True,
    "wallet_history_check": True,
    "wallet_low_trade_count": 5,
    "send_startup_notification": True,
    "test_mode": False,
    # v1.7: priorytety. Duze zakupy natychmiast, zwykle zbierane do paczki.
    "priority_single_lower_usd": 2000,
    "priority_single_unknown_usd": 3000,
    "priority_position_usd": 4000,
    "priority_cluster_usd": 6000,
    "priority_cooldown_seconds": 7200,
    "normal_cooldown_seconds": 3600,
    "normal_digest_interval_seconds": 900,
    "normal_digest_top_n": 10,
    # v1.8: polityka alertow. Wszystkie zakupy zachowujemy, alerty tylko >= 1.20.
    "alert_min_odds": 1.20,
    "alert_odds_bands": [
        {"min_odds": 1.20, "min_usd": 5000},
        {"min_odds": 1.50, "min_usd": 2500},
        {"min_odds": 1.70, "min_usd": 1500},
        {"min_odds": 2.00, "min_usd": 1000},
        {"min_odds": 3.00, "min_usd": 750},
        {"min_odds": 5.00, "min_usd": 500},
    ],
    "unknown_tier_threshold_multiplier": 1.5,
    "position_6h_threshold_multiplier": 0.60,
    "position_24h_threshold_multiplier": 0.90,
    "priority_threshold_multiplier": 1.50,
    "priority_min_usd": 1800,
    "flow_enabled": True,
    "flow_window_seconds": 900,
    "flow_min_buy_usd": 2500,
    "flow_min_wallets": 3,
    "flow_min_share": 0.80,
    "flow_burst_multiplier": 4.0,
    "flow_baseline_min_usd": 500,
    "flow_priority_cooldown_seconds": 3600,
    "flow_normal_cooldown_seconds": 3600,
}


def number(value: Any, fallback: float = 0) -> float:
    try:
        if value is None or value == "":
            return fallback
        return float(value)
    except (ValueError, TypeError):
        return fallback


def is_cs2(text: str) -> bool:
    return bool(CS2.search(text))


def classify(title: str) -> str:
    t = title.casefold()
    if any(w.casefold() in t for w in LOW_LEAGUE):
        return "lower"
    if any(w.casefold() in t for w in HIGH_LEAGUE):
        return "major"
    return "unknown"


def load_env(filename: Path) -> dict[str, str]:
    values = dict(os.environ)
    if filename.exists():
        for line in filename.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(path: Path) -> dict:
    config = DEFAULT.copy()
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        unknown = set(raw) - set(config)
        if unknown:
            LOG.warning("Nierozpoznane ustawienia w config.json: %s", ", ".join(sorted(unknown)))
        config.update({k: v for k, v in raw.items() if k in config})
    if number(config["api_minimum_trade_usd"]) <= 0:
        raise ValueError("api_minimum_trade_usd musi być dodatnie")
    if number(config["api_minimum_trade_usd"]) > min(
        number(config["single_buy_lower_usd"]), number(config["single_buy_unknown_usd"])
    ):
        raise ValueError("api_minimum_trade_usd nie moze przekraczac progu pojedynczego zakupu")
    if not 300 <= number(config["position_window_seconds"]) <= 86400:
        raise ValueError("position_window_seconds musi byc w zakresie 300-86400")
    for key in ("single_buy_lower_usd", "single_buy_unknown_usd", "position_lower_usd", "position_unknown_usd", "position_24h_lower_usd", "position_24h_unknown_usd"):
        if number(config[key]) <= 0:
            raise ValueError(f"{key} musi byc dodatnie")
    if not 21600 <= number(config["position_24h_window_seconds"]) <= 86400:
        raise ValueError("position_24h_window_seconds musi być między 21600 a 86400")
    if not 0 <= int(config["daily_summary_hour_local"]) <= 23:
        raise ValueError("daily_summary_hour_local musi być w zakresie 0..23")
    if not 1 <= int(config["daily_summary_top_n"]) <= 10:
        raise ValueError("daily_summary_top_n musi być w zakresie 1..10")
    for key in ("poll_seconds", "discover_seconds", "cluster_window_seconds"):
        if number(config[key]) < 5:
            raise ValueError(f"{key} nie może być mniejsze niż 5")
    if number(config["minimum_event_volume_usd"], -1) < 0:
        raise ValueError("minimum_event_volume_usd musi byc >= 0")
    if not 0 < number(config["relative_market_volume"]) <= 1:
        raise ValueError("relative_market_volume musi być w przedziale (0,1]")
    for key in ("priority_single_lower_usd", "priority_single_unknown_usd", "priority_position_usd",
                "priority_cluster_usd", "priority_cooldown_seconds", "normal_cooldown_seconds",
                "normal_digest_interval_seconds"):
        if number(config[key]) <= 0:
            raise ValueError(f"{key} musi byc dodatnie")
    if not 1 <= int(config["normal_digest_top_n"]) <= 15:
        raise ValueError("normal_digest_top_n musi byc w zakresie 1..15")
    if not 1 <= number(config["alert_min_odds"]) <= 20:
        raise ValueError("alert_min_odds musi byc w zakresie 1..20")
    bands = config["alert_odds_bands"]
    if (not isinstance(bands, list) or not bands or
        any(not isinstance(b,dict) or number(b.get('min_odds')) < 1 or
            number(b.get('min_usd')) < number(config['api_minimum_trade_usd'])
            for b in bands)):
        raise ValueError("alert_odds_bands: niepoprawna lista progow")
    if sorted(float(b['min_odds']) for b in bands) != [float(b['min_odds']) for b in bands]:
        raise ValueError("alert_odds_bands musi byc posortowane po min_odds")
    if float(bands[0]['min_odds']) > float(config['alert_min_odds']):
        raise ValueError("alert_odds_bands musi zawierac prog nie wyzszy niz alert_min_odds")
    if not 300 <= int(config["flow_window_seconds"]) <= 3600:
        raise ValueError("flow_window_seconds musi być 300..3600")
    if not 0.50 < number(config["flow_min_share"]) <= 1:
        raise ValueError("flow_min_share musi być w zakresie (0.5,1]")
    if int(config["flow_min_wallets"]) < 2:
        raise ValueError("flow_min_wallets musi być co najmniej 2")
    for key in ("flow_min_buy_usd", "flow_burst_multiplier", "flow_baseline_min_usd",
                "flow_priority_cooldown_seconds", "flow_normal_cooldown_seconds"):
        if number(config[key]) <= 0:
            raise ValueError(f"{key} musi być dodatnie")
    for key in ('unknown_tier_threshold_multiplier', 'position_6h_threshold_multiplier',
                'position_24h_threshold_multiplier','priority_threshold_multiplier','priority_min_usd'):
        if number(config[key]) <= 0:
            raise ValueError(f"{key} musi byc dodatnie")
    return config


def min_alert_usd(odds: float, tier: str, config: dict) -> float:
    """Próg pojedynczego sygnału na podstawie kursu wejścia i tieru.
    Zwraca inf, gdy kurs jest ponizej minimum.
    """
    if odds < number(config["alert_min_odds"]):
        return float('inf')
    amount = number(config['alert_odds_bands'][0]['min_usd'])
    for band in config['alert_odds_bands']:
        if odds >= number(band['min_odds']):
            amount = number(band['min_usd'])
        else:
            break
    if tier != 'lower':
        amount *= number(config['unknown_tier_threshold_multiplier'])
    return amount


class ApiClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "CS2-Polymarket-Radar/1.0 (read-only)"})

    def get(self, base: str, path: str, params: dict | None = None) -> Any:
        for retry in range(5):
            try:
                response = self.session.get(base + path, params=params, timeout=(8, 25))
                if response.status_code in (429, 500, 502, 503, 504):
                    delay = min(45, number(response.headers.get("Retry-After"), 2 ** retry + 1))
                    LOG.warning("HTTP %s %s, ponawiam za %.0f s", response.status_code, path, delay)
                    time.sleep(delay)
                    continue
                # 4xx (poza 429) to zwykle błędne parametry. Ponawianie nie pomoże.
                if 400 <= response.status_code < 500:
                    details = response.text[:550].replace("\n", " ")
                    raise RuntimeError(
                        f"API odrzuciło zapytanie {base}{path} "
                        f"(HTTP {response.status_code}): {details}"
                    )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as err:
                if retry == 4:
                    raise RuntimeError(f"Błąd API {base}{path}: {err}") from err
                time.sleep(min(20, 2 ** retry + 1))
        raise RuntimeError(f"API nie odpowiedziało: {base}{path}")

    def post_telegram(self, token: str, chat_id: str, message: str) -> None:
        # Nie ujawniaj tokenu w logach w razie HTTPError (requests umieszcza URL w bledzie).
        try:
            response = self.session.post(
                f"{TG}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=(8, 25),
            )
            if not response.ok:
                raise RuntimeError(f"Telegram API: HTTP {response.status_code} (sprawdź token, chat ID i uprawnienia bota)")
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API: odpowiedź ok=false (sprawdź dane bota)")
        except requests.RequestException as error:
            raise RuntimeError(f"Błąd połączenia z Telegramem: {type(error).__name__}") from None


def sports_start_timestamp(value: Any) -> int:
    """Termin meczu z Gamma, nigdy `startDate` (utworzenie rynku).

    Formaty: ISO 8601 z TZ, UTC `Z`, `+00`, epoch seconds/ms, a takze
    ISO bez TZ (domyslnie UTC, jak w API sportowym). Date-only odrzucamy.
    Brak wystarczajacych danych = 0 i napis `Bez terminu` na stronie.
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            stamp = float(value)
            if stamp > 1e11:  # timestamp w milisekundach
                stamp /= 1000
            return int(stamp) if 1577836800 <= stamp <= 4102444800 else 0
        if not isinstance(value, str):
            return 0
        clean = value.strip().replace(' ', 'T', 1)
        if not re.search(r'T\d{1,2}:\d{2}', clean):
            return 0
        clean = re.sub(r'Z$', '+00:00', clean, flags=re.I)
        clean = re.sub(r'([+-]\d{2})$', r'\1:00', clean)
        stamp = datetime.fromisoformat(clean)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)  # daty bez strefy w Gamma sa UTC
        epoch = stamp.timestamp()
        return int(epoch) if 1577836800 <= epoch <= 4102444800 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def event_turnover(event: dict) -> float | None:
    """Skumulowany volume wydarzenia, NIE liquidity ani zakupy naszego radaru.

    Gdy volume wydarzenia jest opóźnione, bierzemy większą z wartości:
    event.volume i suma market.volumeNum (po jednym wpisie na rynek).
    Nieznany obrót => None: nie pomijamy wydarzenia bez dowodu niskiego obrotu.
    """
    candidates: list[float] = []
    for key in ('volume', 'volumeNum'):
        val = event.get(key)
        if val is not None and val != '':
            v = number(val, -1)
            if v >= 0:
                candidates.append(v)
    volumes = []
    for market in event.get('markets') or []:
        if not isinstance(market, dict):
            continue
        raw = market.get('volumeNum')
        if raw is None or raw == '':
            raw = market.get('volume')
        if raw is not None and raw != '':
            v = number(raw, -1)
            if v >= 0:
                volumes.append(v)
    if volumes:
        candidates.append(sum(volumes))
    return max(candidates) if candidates else None


def match_kickoff(event: dict, market: dict) -> int:
    """Najpierw godzina konkretnego rynku sportowego, potem wydarzenia.

    Pole endDate to termin rozliczenia, startDate to wystawienie rynku.
    Nie traktujemy ich jako godziny meczu, nawet gdy inne pola sa puste.
    """
    for value in (market.get('gameStartTime'), market.get('startTime'),
                  event.get('startTime'), event.get('gameStartTime')):
        ts = sports_start_timestamp(value)
        if ts:
            return ts
    return 0


def series_name(value: Any) -> str:
    """Opcjonalna nazwa serii dostarczona przez Gamma, bez domyslania ligi."""
    if isinstance(value, dict):
        return str(value.get('title') or value.get('name') or '')[:140]
    if isinstance(value, list):
        for item in value:
            name = series_name(item)
            if name:
                return name
    return ''


@dataclass
class EventMarket:
    condition_id: str
    event_id: str
    title: str
    market_title: str
    event_slug: str
    market_volume: float | None
    liquidity: float | None
    tier: str
    outcomes: list[str]
    match_start: int = 0
    league: str = ''
    event_closed: bool = False
    event_volume: float | None = None

    @property
    def link(self) -> str:
        return f"https://polymarket.com/event/{self.event_slug}"


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, timestamp INTEGER NOT NULL,
                condition_id TEXT NOT NULL, token_id TEXT,
                wallet TEXT, outcome TEXT NOT NULL, usd REAL NOT NULL, price REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_recent ON trades (timestamp, condition_id, outcome);
            CREATE TABLE IF NOT EXISTS cooldowns (key TEXT PRIMARY KEY, sent_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wallet_stats (wallet TEXT PRIMARY KEY, count INTEGER, checked_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (key TEXT PRIMARY KEY, queued_at INTEGER NOT NULL,
                html_message TEXT NOT NULL, csv_row TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trade_market_info (
                condition_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                market_title TEXT NOT NULL, slug TEXT NOT NULL, tier TEXT NOT NULL
            );
        """)
        # Migracja w miejscu: nie usuwamy starszych danych i artefaktow GitHub Actions.
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS signal_candidates (
                id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, condition_id TEXT NOT NULL,
                outcome TEXT NOT NULL, wallet TEXT, kind TEXT NOT NULL,
                usd REAL NOT NULL, batch_key TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_signals_batch ON signal_candidates(batch_key, created_at);
            CREATE TABLE IF NOT EXISTS signal_throttle (
                key TEXT PRIMARY KEY, last_at INTEGER NOT NULL
            );
        """)
        self._add_column("trades", "side", "TEXT NOT NULL DEFAULT 'BUY'")
        self._add_column("trades", "trader_name", "TEXT NOT NULL DEFAULT ''")
        self._add_column("signal_candidates", "avg_odds", "REAL NOT NULL DEFAULT 0")
        # Dla sygnalow z v1.7 odzyskaj kurs z pierwotnej transakcji.
        # Bez tego stare pozycje mialyby w nowym digescie kurs 0.00.
        self.conn.execute("""UPDATE signal_candidates SET avg_odds=COALESCE(
            (SELECT 1.0 / t.price FROM trades t WHERE t.id=signal_candidates.id),0)
            WHERE avg_odds=0""")
        self._add_column("trade_market_info", "outcomes_json", "TEXT NOT NULL DEFAULT '[]'")
        self._add_column("trade_market_info", "event_id", "TEXT NOT NULL DEFAULT ''")
        self._add_column("trade_market_info", "match_start", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("trade_market_info", "league", "TEXT NOT NULL DEFAULT ''")
        self._add_column("trade_market_info", "event_closed", "INTEGER NOT NULL DEFAULT 0")
        self._add_column("trade_market_info", "event_volume", "REAL")
        self.conn.commit()

    def _add_column(self, table: str, column: str, spec: str) -> None:
        existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")

    def get_meta(self, key: str) -> str | None:
        result = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return result[0] if result else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def add_trade(self, trade: dict) -> bool:
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO trades
                   (id,timestamp,condition_id,token_id,wallet,outcome,usd,price,side,trader_name)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (trade["id"], trade["timestamp"], trade["condition_id"], trade["token_id"],
                 trade["wallet"], trade["outcome"], trade["usd"], trade["price"],
                 "BUY", trade.get("trader_name", "")),
            )
        return cur.rowcount == 1

    def remember_market(self, market: EventMarket) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO trade_market_info
                   (condition_id,title,market_title,slug,tier,outcomes_json,event_id,
                    match_start,league,event_closed,event_volume)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (market.condition_id, market.title, market.market_title, market.event_slug,
                 market.tier, json.dumps(market.outcomes, ensure_ascii=False), market.event_id,
                 market.match_start, market.league, int(market.event_closed), market.event_volume)
            )

    def daily_top_positions(self, since: int, until: int, limit: int, min_odds: float = 1.2) -> list[tuple]:
        # Obserwowane zakupy brutto, a nie saldo portfela czy prawdziwe otwarte pozycje.
        return self.conn.execute("""
            SELECT i.title, i.market_title, i.slug, t.outcome, t.wallet,
                   SUM(t.usd) AS total, COUNT(*) AS fills,
                   SUM(t.usd / t.price) / NULLIF(SUM(t.usd),0) AS avg_odds
            FROM trades t
            JOIN trade_market_info i ON i.condition_id = t.condition_id
            WHERE t.timestamp >= ? AND t.timestamp < ? AND i.tier = 'lower'
                  AND LENGTH(t.wallet) > 0 AND t.price <= 1.0 / ?
            GROUP BY t.condition_id, t.outcome, t.wallet
            ORDER BY total DESC, fills DESC LIMIT ?
        """, (since, until, min_odds, limit)).fetchall()

    def recent(self, condition_id: str, outcome: str, since: int, wallet: str | None = None) -> tuple[int, float, int]:
        query = "SELECT COUNT(*), COALESCE(SUM(usd),0), COUNT(DISTINCT wallet) FROM trades WHERE condition_id=? AND outcome=? AND timestamp>=?"
        args: list[Any] = [condition_id, outcome, since]
        if wallet:
            query += " AND wallet=?"
            args.append(wallet)
        count, value, unique = self.conn.execute(query, args).fetchone()
        return int(count), float(value), int(unique)

    def recent_qualified(self, condition_id: str, outcome: str, since: int,
                         min_odds: float, wallet: str | None = None) -> tuple[int, float, int, float]:
        """Liczy tylko zakupy o kursie >= minimum; zwraca sredni kurs bazowy.
        Wazenie udzialami: sredni kurs = suma nabytych udzialow / USD.
        """
        query = """SELECT COUNT(*), COALESCE(SUM(usd),0), COUNT(DISTINCT wallet),
                   COALESCE(SUM(usd / price),0)
                   FROM trades WHERE condition_id=? AND outcome=?
                   AND timestamp>=? AND price<=?"""
        args: list[Any] = [condition_id, outcome, since, 1.0 / min_odds]
        if wallet:
            query += ' AND wallet=?'
            args.append(wallet)
        count, usd, wallets, shares = self.conn.execute(query, args).fetchone()
        return int(count), float(usd), int(wallets), float(shares) / float(usd) if usd else 0.0

    def market_flow_trades(self, condition_id: str, since: int, until: int) -> list[tuple]:
        """Unique recorded BUY fills in one exact market; PK dedup."""
        return self.conn.execute(
            """SELECT timestamp, outcome, wallet, usd, price FROM trades
               WHERE condition_id=? AND timestamp>=? AND timestamp<=?
               ORDER BY timestamp, id""", (condition_id, since, until)
        ).fetchall()

    def observed_buy_summary(self, condition_id: str, outcome: str, since: int, until: int) -> tuple:
        """Real recorded buys, not accumulated signal_candidate.usd values."""
        n, amount, wallets, shares = self.conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(usd),0),
                      COUNT(DISTINCT NULLIF(wallet,'')), COALESCE(SUM(usd/price),0)
               FROM trades WHERE condition_id=? AND outcome=?
               AND timestamp>=? AND timestamp<?""",
            (condition_id, outcome, since, until)
        ).fetchone()
        return int(n), float(amount), int(wallets), float(shares) / float(amount) if amount else 0.0

    def recently_alerted_market(self, condition_id: str, outcome: str, now: int,
                                seconds: int = 15 * 60) -> bool:
        """Suppress duplicate flow urgency after a priority trade/cluster alert."""
        prefix = f"priority:{condition_id}:"
        multi = f"priority-multi:{condition_id}:{outcome}"
        suffix = f":{outcome}"
        rows = list(self.conn.execute(
            "SELECT key FROM cooldowns WHERE sent_at>=?", (now - seconds,)
        )) + list(self.conn.execute("SELECT key FROM outbox WHERE key LIKE 'priority%'"))
        return any(key == multi or (key.startswith(prefix) and key.endswith(suffix))
                   for (key,) in rows)

    def queue_market_flow_signal(self, condition_id: str, outcome: str,
                                 now: int, cooldown: int, avg_odds: float) -> bool:
        """Only a marker for digest; monetary totals come from raw trades."""
        key = f"normal-flow:{condition_id}:{outcome}"
        synthetic_id = "flow:" + hashlib.sha256(
            f"{condition_id}|{outcome}|{now // 900}".encode("utf-8")
        ).hexdigest()
        with self.conn:
            last = self.conn.execute(
                "SELECT last_at FROM signal_throttle WHERE key=?", (key,)
            ).fetchone()
            if last and now - int(last[0]) < cooldown:
                return False
            self.conn.execute("INSERT OR REPLACE INTO signal_throttle VALUES (?,?)", (key, now))
            self.conn.execute(
                """INSERT OR IGNORE INTO signal_candidates
                   (id,created_at,condition_id,outcome,wallet,kind,usd,batch_key,avg_odds)
                   VALUES (?,?,?,?,'','flow',0,'',?)""",
                (synthetic_id, now, condition_id, outcome, avg_odds)
            )
        return True

    def can_alert(self, key: str, now: int, cooldown: int) -> bool:
        pending = self.conn.execute("SELECT 1 FROM outbox WHERE key=?", (key,)).fetchone()
        if pending:
            return False
        result = self.conn.execute("SELECT sent_at FROM cooldowns WHERE key=?", (key,)).fetchone()
        return not result or now - int(result[0]) >= cooldown

    def queue_alert(self, key: str, now: int, message: str, csv_row: list) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO outbox VALUES (?,?,?,?)",
                              (key, now, message, json.dumps(csv_row, ensure_ascii=False)))

    def pending_alerts(self, limit: int = 40) -> list[tuple[str, str, list]]:
        rows = self.conn.execute("SELECT key, html_message, csv_row FROM outbox ORDER BY queued_at LIMIT ?", (limit,)).fetchall()
        return [(key, msg, json.loads(row)) for key, msg, row in rows]

    def finish_alert(self, key: str, now: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM outbox WHERE key=?", (key,))
            self.conn.execute("INSERT OR REPLACE INTO cooldowns VALUES (?,?)", (key, now))

    def record_alert(self, key: str, now: int) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO cooldowns VALUES (?,?)", (key, now))

    def wallet_count(self, wallet: str, now: int) -> tuple[bool, int | None]:
        result = self.conn.execute("SELECT count,checked_at FROM wallet_stats WHERE wallet=?", (wallet,)).fetchone()
        if result and now - int(result[1]) <= 3600:
            return True, int(result[0]) if result[0] is not None else None
        return False, None

    def set_wallet_count(self, wallet: str, count: int | None, now: int) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO wallet_stats VALUES (?,?,?)", (wallet, count, now))

    def queue_normal_signal(self, trade: dict, kind: str, now: int, cooldown: int,
                            total_usd: float | None = None, avg_odds: float | None = None) -> bool:
        # Ograniczamy powtorzenia jednego portfela / wyniku, ale trzymamy zakup w trades.
        wallet = trade.get("wallet") or trade["id"]
        key = f"normal:{trade['condition_id']}:{trade['outcome']}:{wallet}"
        with self.conn:
            last = self.conn.execute("SELECT last_at FROM signal_throttle WHERE key=?", (key,)).fetchone()
            if last and now - int(last[0]) < cooldown:
                return False
            self.conn.execute("INSERT OR REPLACE INTO signal_throttle VALUES (?,?)", (key, now))
            self.conn.execute("""INSERT OR IGNORE INTO signal_candidates
                (id,created_at,condition_id,outcome,wallet,kind,usd,batch_key,avg_odds)
                VALUES (?,?,?,?,?,?,?,'',?)""",
                (trade['id'], now, trade['condition_id'], trade['outcome'],
                 trade.get('wallet') or '', kind,
                 float(trade['usd']),  # store the actual BUY, never a cumulative position
                 float(avg_odds if avg_odds is not None else 1 / trade['price'])))
            return True

    def discard_old_low_odds_signals(self, minimum_odds: float) -> int:
        # Zwykle alerty z poprzedniej wersji po 1.01 nie powinny wracac.
        with self.conn:
            cur=self.conn.execute("DELETE FROM signal_candidates WHERE batch_key='' AND avg_odds<?", (minimum_odds,))
        return cur.rowcount

    def pending_normal_signals(self) -> list[tuple]:
        return self.conn.execute("""SELECT s.condition_id, s.outcome, s.kind, s.usd,
            COALESCE(i.title,'Nieznany mecz'), COALESCE(i.slug,''), s.wallet, s.avg_odds,
            COALESCE(i.market_title,''), s.created_at
            FROM signal_candidates s
            LEFT JOIN trade_market_info i ON i.condition_id=s.condition_id
            WHERE s.batch_key='' ORDER BY s.created_at, s.id""").fetchall()

    def queue_normal_digest(self, now: int, message: str) -> int:
        with self.conn:
            key = f"normal-digest:{now}"
            cur = self.conn.execute("UPDATE signal_candidates SET batch_key=? WHERE batch_key=''", (key,))
            if cur.rowcount:
                self.conn.execute("INSERT OR IGNORE INTO outbox VALUES (?,?,?,?)",
                                  (key, now, message, '[]'))
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  ('last_normal_digest', str(now)))
            return cur.rowcount

    def cleanup(self, now: int) -> None:
        with self.conn:
            # Historia dashboardu: przechowuj 30 dni. 24h wystarcza do liczenia alertow.
            self.conn.execute("DELETE FROM trades WHERE timestamp<?", (now - 30 * 86400,))
            self.conn.execute("DELETE FROM cooldowns WHERE sent_at<?", (now - 7 * 86400,))
            self.conn.execute("DELETE FROM wallet_stats WHERE checked_at<?", (now - 2 * 86400,))
            self.conn.execute("DELETE FROM signal_throttle WHERE last_at<?", (now - 2 * 86400,))
            self.conn.execute("DELETE FROM signal_candidates WHERE created_at<? AND batch_key<>''", (now - 7 * 86400,))


class Radar:
    def __init__(self, client: ApiClient, db: Database, config: dict, token: str = "", chat_id: str = "", dry_run: bool = False):
        self.client, self.db, self.cfg = client, db, config
        self.token, self.chat_id, self.dry_run = token, chat_id, dry_run
        self.markets: dict[str, EventMarket] = {}
        self.event_ids: list[str] = []
        self.tag_ids: list[str] = []
        self.started = int(time.time())
        self.alerts_queued = 0
        self.normal_signals_queued = 0
        self.daily_reports_queued = 0
        self.flow_conditions: set[str] = set()
        self.last_discovery = 0.0
        self.csv_path = BASE / "alerty.csv"
        self.cache_path = BASE / "market_cache.json"
        skipped = self.db.discard_old_low_odds_signals(number(self.cfg['alert_min_odds']))
        if skipped:
            LOG.info('Odrzucono %d starych sygnalow ponizej minimalnego kursu %.2f',
                     skipped, number(self.cfg['alert_min_odds']))
        self.load_market_cache()

    def cache_signature(self) -> str:
        # Odrzucenie cache po zmianie ustawień klasyfikacji.
        data = {k: self.cfg[k] for k in (
            "max_events", "exclude_major_tournaments", "max_market_volume_unknown_usd",
            "minimum_event_volume_usd") }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def load_market_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            updated = int(payload["updated_at"])
            if (payload.get("version") != 1 or
                    payload.get("signature") != self.cache_signature() or
                    time.time() - updated > number(self.cfg["discover_seconds"])):
                return
            markets = {
                entry["condition_id"]: EventMarket(**entry)
                for entry in payload.get("markets", [])
            }
            self.markets = markets
            self.event_ids = list(payload.get("event_ids", []))
            self.tag_ids = list(payload.get("tag_ids", []))
            self.last_discovery = updated
            LOG.info("Załadowano cache: %d wydarzeń, %d rynków.",
                     len(self.event_ids), len(self.markets))
        except (OSError, ValueError, TypeError, KeyError):
            return

    def save_market_cache(self) -> None:
        payload = {
            "version": 1,
            "signature": self.cache_signature(),
            "updated_at": int(self.last_discovery),
            "event_ids": self.event_ids,
            "tag_ids": self.tag_ids,
            "markets": [vars(m) for m in self.markets.values()],
        }
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.cache_path)

    def find_tags(self) -> list[str]:
        ids = set()
        sports = self.client.get(GAMMA, "/sports")
        if isinstance(sports, list):
            for sport in sports:
                if not isinstance(sport, dict):
                    continue
                name = str(sport.get("sport") or "")
                if name.casefold() in ("cs2", "csgo", "counter-strike", "counter-strike-2", "counterstrike") or is_cs2(name):
                    raw_tags = sport.get("tags") or ""
                    if isinstance(raw_tags, str):
                        ids.update(t.strip() for t in raw_tags.split(",") if t.strip().isdigit())
                    elif isinstance(raw_tags, list):
                        ids.update(str(t) for t in raw_tags if str(t).isdigit())
        if ids:
            # /sports moze oddawac tag nadrzedny "Sports" (ID 1), ktory ma
            # dziesiatki tysiecy wydarzen z innych dyscyplin. Wybieraj
            # najbardziej szczegolowe tagi CS2, o ile API pozwala je rozpoznac.
            specific: set[str] = set()
            for tag_id in sorted(ids, key=int):
                try:
                    tag = self.client.get(GAMMA, f"/tags/{tag_id}")
                    if isinstance(tag, dict):
                        label = str(tag.get("label") or "") + " " + str(tag.get("slug") or "")
                        if is_cs2(label):
                            specific.add(tag_id)
                except Exception as err:
                    LOG.debug("Nie udalo sie pobrac etykiety tagu %s: %s", tag_id, err)
            if specific:
                LOG.info("Wybrano wyspecjalizowane tagi CS2: %s", ",".join(sorted(specific, key=int)))
                return sorted(specific, key=int)
            # Potwierdzone: tag 1 = 'Sports'. Nie skanuj całego sportu, gdy
            # dostepne sa bardziej szczegolowe tagi podane przez /sports.
            non_generic = ids - {"1"}
            return sorted(non_generic if non_generic else ids, key=int)
        # Fallback, jeśli sport nie jest opisany w /sports.
        offset = 0
        while offset < 5000:
            tags = self.client.get(GAMMA, "/tags", {"limit": 100, "offset": offset})
            if not isinstance(tags, list) or not tags:
                break
            for tag in tags:
                if not isinstance(tag, dict):
                    continue
                label = (str(tag.get("label", "")) + " " + str(tag.get("slug", ""))).casefold()
                if is_cs2(label) and tag.get("id"):
                    ids.add(str(tag["id"]))
            offset += len(tags)
            if len(tags) < 100:
                break
        return sorted(ids, key=int)

    def discover(self) -> None:
        if not self.tag_ids:
            self.tag_ids = self.find_tags()
        if not self.tag_ids:
            LOG.error("Nie znaleziono tagu CS2. Sprawdź API /sports i /tags; brak monitorowanych rynków.")
            self.event_ids, self.markets = [], {}
            return
        events: dict[str, dict] = {}
        # /events z offset >= ~2000 zwraca HTTP 422. Keyset skanuje następne
        # strony przez after_cursor, bez tego ograniczenia.
        page_size = 100
        for tag in self.tag_ids:
            cursor = None
            seen_cursors = set()
            examined = 0
            while examined < int(self.cfg["max_events"]):
                params = {
                    "tag_id": tag, "closed": "false", "limit": min(
                        page_size, int(self.cfg["max_events"]) - examined
                    ),
                    "order": "id", "ascending": "false"
                }
                if cursor:
                    params["after_cursor"] = cursor
                payload = self.client.get(GAMMA, "/events/keyset", params)
                if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
                    raise RuntimeError("Gamma /events/keyset nie zwróciło {events: [...]} ")
                page = payload["events"]
                examined += len(page)
                for event in page:
                    if (isinstance(event, dict) and event.get("id") and
                            event.get("active", True) is not False):
                        events[str(event["id"])] = event
                next_cursor = payload.get("next_cursor")
                if not page or not next_cursor:
                    break
                next_cursor = str(next_cursor)
                if next_cursor == cursor or next_cursor in seen_cursors:
                    LOG.warning("Powtórzony kursor Gamma /events/keyset dla tagu %s — zatrzymuję paginację.", tag)
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            if examined >= int(self.cfg["max_events"]):
                LOG.warning("Osiągnięto max_events=%s dla tagu %s: część wydarzeń może zostać pominięta",
                            self.cfg["max_events"], tag)

        markets: dict[str, EventMarket] = {}
        ev_ids: set[str] = set()
        matches = 0
        ignored_low_volume = 0
        unknown_volume = 0
        missing_kickoffs = 0
        for eid, ev in events.items():
            title = str(ev.get("title") or "")
            if not MATCH.search(title):
                continue   # monitorujemy wyłącznie mecze A vs B, nie propsy/transfery
            ev_tags = " ".join(str(t.get("label", "")) for t in ev.get("tags", []) if isinstance(t, dict))
            # W sportowych wydarzeniach nazwa może zawierać tylko drużyny.
            # Polymarket stosuje wtedy często slug zaczynający się od cs2-.
            slug_hint = str(ev.get("slug") or "")
            if not is_cs2(title + " " + ev_tags + " " + slug_hint):
                # Nie polegaj wyłącznie na odziedziczonym tagu: tag "Esports" może
                # obejmować również inne gry (Valorant, LoL itp.).
                continue
            tier = classify(title)
            volume_value = event_turnover(ev)
            if volume_value is None:
                unknown_volume += 1  # nie odrzucaj rynku przy nieznanym obrocie
            elif volume_value < number(self.cfg['minimum_event_volume_usd']):
                ignored_low_volume += 1
                continue  # nie pobieramy transakcji tych wydarzen
            volume = volume_value if volume_value is not None else 0
            if self.cfg["exclude_major_tournaments"] and tier == "major":
                continue
            if (tier == "unknown" and self.cfg["exclude_major_tournaments"] and
                    volume > number(self.cfg["max_market_volume_unknown_usd"])):
                # Istotne: obrót jest tylko przybliżeniem popularności, NIE oficjalnym tierem.
                continue
            slug = str(ev.get("slug") or "").strip()
            if not slug:
                continue
            added = 0
            # `startDate` to zwykle utworzenie rynku. Nie stosujemy go jako godziny meczu.
            event_match_start = sports_start_timestamp(ev.get('startTime')) or sports_start_timestamp(ev.get('gameStartTime'))
            league = series_name(ev.get('series')) or series_name(ev.get('seriesItem'))
            for m in ev.get("markets") or []:
                if not isinstance(m, dict) or m.get("closed") is True:
                    continue
                cond = str(m.get("conditionId") or m.get("condition_id") or "")
                if not VALID_CONDITION.fullmatch(cond):
                    continue
                raw_outcomes = m.get("outcomes") or []
                if isinstance(raw_outcomes, str):
                    try:
                        raw_outcomes = json.loads(raw_outcomes)
                    except ValueError:
                        raw_outcomes = []
                if not isinstance(raw_outcomes, list):
                    raw_outcomes = []
                market_volume = m.get("volumeNum")
                if market_volume is None:
                    market_volume = m.get("volume")
                liquidity = m.get("liquidityNum")
                if liquidity is None:
                    liquidity = m.get("liquidity")
                markets[cond.casefold()] = EventMarket(
                    condition_id=cond.casefold(), event_id=eid, title=title,
                    market_title=str(m.get("question") or title), event_slug=slug,
                    market_volume=number(market_volume) if market_volume is not None else None,
                    liquidity=number(liquidity) if liquidity is not None else None,
                    tier=tier, outcomes=[str(o) for o in raw_outcomes],
                    match_start=match_kickoff(ev,m) or event_match_start,
                    league=league, event_closed=bool(ev.get('closed', False)),
                    event_volume=volume_value
                )
                if not markets[cond.casefold()].match_start:
                    missing_kickoffs += 1
                added += 1
            if added:
                ev_ids.add(eid)
                matches += 1
        self.markets, self.event_ids = markets, sorted(ev_ids, key=int)
        self.last_discovery = time.time()
        self.save_market_cache()
        LOG.info("Odkryto %s wydarzeń CS2 i %s rynków; odfiltrowano %s wydarzeń z obrotem < $%.0f; brak obrotu w %s; brak terminu w %s rynkach; tagi: %s",
                 matches, len(markets), ignored_low_volume,
                 number(self.cfg['minimum_event_volume_usd']), unknown_volume,
                 missing_kickoffs, ",".join(self.tag_ids))

    @staticmethod
    def normalize(trade: dict) -> dict | None:
        try:
            cond = str(trade.get("condition_id") or trade.get("conditionId") or "").casefold()
            timestamp = int(trade["timestamp"])
            shares = number(trade.get("size"), -1)
            price = number(trade.get("price"), -1)
            if not VALID_CONDITION.fullmatch(cond) or shares <= 0 or not 0 < price <= 1:
                return None
            if str(trade.get("side", "")).upper() != "BUY":
                return None
            wallet = str(trade.get("proxy_wallet") or trade.get("proxyWallet") or "").lower()
            token = str(trade.get("token_id") or trade.get("asset") or "")
            outcome = str(trade.get("outcome") or "?")
            trader_name = str(trade.get("name") or trade.get("pseudonym") or "").strip()[:70]
            tx = str(trade.get("transaction_hash") or trade.get("transactionHash") or "").lower()
            # API nie gwarantuje unikalnego ID fill: bardzo podobne fill-e w jednej transakcji
            # mogą zostać zgrupowane w jeden wpis. To ograniczenie monitoringu.
            signature = "|".join([tx, cond, token, wallet, outcome, "BUY", str(timestamp),
                                  f"{shares:.10f}", f"{price:.10f}"])
            digest = hashlib.sha256(signature.encode()).hexdigest()
            return {"id": digest, "timestamp": timestamp, "condition_id": cond,
                    "wallet": wallet, "token_id": token, "outcome": outcome,
                    "trader_name": trader_name, "price": price, "usd": round(shares * price, 4)}
        except (KeyError, TypeError, ValueError):
            return None

    def wallet_history(self, wallet: str, now: int) -> str:
        if not wallet or not self.cfg["wallet_history_check"]:
            return ""
        cached, count = self.db.wallet_count(wallet, now)
        if not cached:
            try:
                info = self.client.get(DATA, "/v2/user-volume", {"user": wallet})
                payload = info.get("data") if isinstance(info, dict) else None
                raw_count = payload.get("trade_count") if isinstance(payload, dict) else None
                count = int(raw_count) if raw_count is not None else None
                self.db.set_wallet_count(wallet, count, now)
            except Exception as e:
                LOG.debug("Nie mogę pobrać historii portfela: %s", e)
                return ""
        if count is not None and count <= int(self.cfg["wallet_low_trade_count"]):
            return f"\n🆕 Mało transakcji w historii API: {count} (nie dowodzi nowego konta)"
        return ""

    def message(self, kind: str, market: EventMarket, trade: dict, total: float, n: int, unique: int,
                wallet_note: str = "", avg_odds: float | None = None) -> str:
        e = html.escape
        odds = (1 / trade["price"]) if trade["price"] > 0 else 0
        volume = market.market_volume
        ratio_str = "brak danych"
        if volume is not None and volume > 0:
            ratio_str = f"{100 * trade['usd'] / volume:.1f}% (przybliżenie)"
        tier_name = {"lower": "niższa liga (heurystyka)", "unknown": "poziom nieustalony", "major": "duży turniej"}[market.tier]
        date_txt = datetime.fromtimestamp(trade["timestamp"], ZoneInfo("Europe/Warsaw")).strftime("%d.%m %H:%M:%S")
        wallet = trade["wallet"]
        wallet_link = f"https://polymarket.com/profile/{wallet}" if wallet.startswith("0x") else ""
        heading = {
            "single": "💰 DUŻY ZAKUP NA MECZU CS2",
            "position": "🔁 PORTFEL ZBUDOWAŁ DUŻY WOLUMEN ZAKUPÓW",
            "position24": "🕘 DUŻE ZAKUPY PORTFELA W CIĄGU 24H",
            "wallet": "🔁 SERIA ZAKUPÓW PORTFELA",
            "multi": "👥 KILKA PORTFELI NA TEN SAM WYNIK",
        }[kind]
        lines = [
            f"<b>{heading} — CS2</b>",
            f"🎮 <b>{e(market.title)}</b>",
            f"🏷️ {e(tier_name)}",
            f"🎯 Rynek: {e(market.market_title)}",
            f"📌 Wynik/udziały: <b>{e(trade['outcome'])}</b>",
            f"💵 Ostatni zakup: <b>${trade['usd']:,.2f}</b>",
            f"📈 Cena udziału: {trade['price']:.3f} (kurs ≈ {odds:.2f}, bez opłat)",
            f"📊 Wolumen rynku wg Gamma: {('$' + format(volume, ',.0f')) if volume is not None else 'brak danych'}",
            f"📐 Zakup / wolumen z ostatniego odczytu: {ratio_str}",
        ]
        if avg_odds is not None and avg_odds >= number(self.cfg['alert_min_odds']):
            lines.append(f"⚖️ Kurs średni kwalifikujących się zakupów ≈ {avg_odds:.2f}")
        if kind in ("single", "position", "position24"):
            hours = (int(self.cfg["position_24h_window_seconds"] / 3600)
                     if kind == "position24" else int(self.cfg["position_window_seconds"] / 3600))
            lines.append(
                f"📦 Zakupy tego portfela na ten wynik w ostatnich "
                f"{hours} h: "
                f"<b>${total:,.2f}</b> (liczba transakcji odczytanych przez radar: {n})"
            )
        elif kind in ("wallet", "multi"):
            lines.append(f"⏱️ Ostatnie {int(self.cfg['cluster_window_seconds']/60)} min: <b>${total:,.2f}</b>, {n} zakupów, {unique} portfeli")
        if trade.get("trader_name"):
            lines.append(f"👤 Trader: <b>{e(trade['trader_name'])}</b>")
        if wallet:
            lines.append(f"👛 Portfel: <code>{e(wallet[:10])}…{e(wallet[-6:])}</code>")
        lines.append(f"🕓 {date_txt} (czas polski)")
        if wallet_note:
            lines.append(wallet_note)
        lines += ["", f'<a href="{e(market.link, quote=True)}">🔗 Otwórz mecz na Polymarket</a>']
        if wallet_link:
            lines.append(f'<a href="{e(wallet_link, quote=True)}">👛 Profil portfela</a>')
        lines += ["", "ℹ️ Sumy oznaczają zaobserwowane ZAKUPY BRUTTO; nie uwzględniają sprzedaży i mogą być niepełne. Sygnał nie dowodzi ustawienia meczu."]
        return "\n".join(lines)

    def write_csv(self, row: list) -> None:
        is_new = not self.csv_path.exists()
        with self.csv_path.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            if is_new:
                writer.writerow(["czas_utc", "typ", "mecz", "rynek", "wynik", "usd_zakup", "usd_5min", "portfele_5min", "portfel", "url"])
            writer.writerow(row)

    def flush_outbox(self) -> None:
        if self.dry_run:
            return
        for key, message, row in self.db.pending_alerts():
            try:
                self.client.post_telegram(self.token, self.chat_id, message)
            except Exception as e:
                LOG.warning("Nie udało się wysłać alertu: %s. Zostaje w kolejce.", e)
                break
            # Kolejka chroni przed utratą wiadomości; nie jest to exactly-once:
            # przy awarii po wysłaniu, przed potwierdzeniem DB, alert może się powtórzyć.
            if row:  # Raport dzienny nie jest alertem transakcyjnym do CSV.
                self.write_csv(row)
            self.db.finish_alert(key, int(time.time()))

    def alert(self, key: str, kind: str, trade: dict, m: EventMarket, now: int,
              total: float = 0, count: int = 0, unique: int = 0,
              cooldown: int | None = None, avg_odds: float | None = None) -> bool:
        if not self.db.can_alert(key, now, int(cooldown or self.cfg["alert_cooldown_seconds"])):
            return False
        note = self.wallet_history(trade["wallet"], now) if kind == "single" else ""
        msg = self.message(kind, m, trade, total, count, unique, note, avg_odds)
        row = [datetime.fromtimestamp(trade["timestamp"], timezone.utc).isoformat(), kind,
               m.title, m.market_title, trade["outcome"], trade["usd"], total,
               unique, trade["wallet"], m.link]
        self.alerts_queued += 1
        if self.dry_run:
            LOG.info("DRY-RUN: %s | %s | $%.2f", kind, m.title, trade["usd"])
            print("\n" + re.sub(r"<[^>]*>", "", msg) + "\n")
            self.write_csv(row)
            self.db.record_alert(key, now)
            return True
        self.db.queue_alert(key, now, msg, row)
        return True

    @staticmethod
    def wallet_short(wallet: str) -> str:
        return wallet[:8] + "…" + wallet[-4:] if len(wallet) >= 16 else (wallet or "brak")

    def market_flow_message(self, market: EventMarket, flow: dict) -> str:
        """Actionable alert with explicit denominator and no manipulation claims."""
        escape = html.escape
        burst = flow["burst_ratio"]
        delta = flow["delta_pp"]
        heading = "🚨 NAGŁE PRZECHYLENIE ZAKUPÓW BUY — CS2"
        lines = [
            f"<b>{heading}</b>",
            f"🎮 <b>{escape(market.title)}</b>",
            f"🎯 Rynek: <b>{escape(market.market_title)}</b>",
            f"📌 Strona: <b>{escape(flow['outcome'])}</b>",
            f"💵 Zakupy BUY w ostatnich {flow['window_minutes']} min: "
            f"<b>{flow['amount']:,.2f} USD</b> na tę stronę / "
            f"{flow['total']:,.2f} USD na cały rynek",
            f"⚖️ Udział obserwowanych zakupów: <b>{flow['share']*100:.0f}%</b>",
            f"👛 {flow['wallets']} portfeli po tej stronie, {flow['buys']} zakupów",
            f"🔎 Największy portfel: {flow['top_wallet_share']*100:.0f}% zakupów strony",
            f"📈 Średni kurs kupna (ważony udziałami): ≈ {flow['avg_odds']:.2f}",
        ]
        if burst is not None:
            lines.append(f"⚡ Tempo zakupów całego rynku: {burst:.1f}× vs poprzednie {flow['baseline_minutes']} min")
        else:
            lines.append("⚡ Zbyt mało wcześniejszych zakupów do porównania tempa")
        if delta is not None:
            lines.append(f"📊 Zmiana udziału strony: {delta:+.0f} pp vs wcześniejsze obserwacje")
        lines.extend([
            f"🔗 <a href=\"{escape(market.link, quote=True)}\">Polymarket: otwórz rynek</a>",
            "",
            "ℹ️ To wyłącznie zaobserwowane zakupy BUY od progu radaru. "
            "Nie są to pozycje netto, pełny obrót rynku ani dowód manipulacji.",
        ])
        return "\n".join(lines)

    def market_flow_alerts(self, now: int) -> None:
        """Evaluate each changed condition once per scan; avoid whale-duplicate spam."""
        conditions = sorted(self.flow_conditions)
        self.flow_conditions.clear()
        if not self.cfg["flow_enabled"]:
            return
        for condition_id in conditions:
            market = self.markets.get(condition_id)
            if not market or market.event_closed:
                continue
            rows = self.db.market_flow_trades(condition_id, now - BASELINE_SECONDS, now + 5)
            flows = assess_market_flow(
                rows, market.outcomes, now,
                min_buy_usd=number(self.cfg["flow_min_buy_usd"]),
                minimum_wallets=int(self.cfg["flow_min_wallets"]),
                min_share=number(self.cfg["flow_min_share"]),
                burst_multiplier=number(self.cfg["flow_burst_multiplier"]),
                baseline_min_usd=number(self.cfg["flow_baseline_min_usd"]),
                window_seconds=int(self.cfg["flow_window_seconds"]),
            )
            for flow in flows:
                outcome = flow["outcome"]
                if self.db.recently_alerted_market(condition_id, outcome, now):
                    LOG.info("FLOW: istnieje świeży alert priorytetowy, nie duplikuję %s / %s",
                             market.market_title, outcome)
                    continue
                key = f"market-flow:{condition_id}:{outcome}"
                if flow["urgent"]:
                    if self.db.can_alert(key, now, int(self.cfg["flow_priority_cooldown_seconds"])):
                        self.db.queue_alert(key, now, self.market_flow_message(market, flow), [])
                        self.alerts_queued += 1
                        LOG.info("FLOW PILNE: %s / %s | %.0f USD | %.0f%% | %d portfeli",
                                 market.market_title, outcome, flow["amount"],
                                 flow["share"] * 100, flow["wallets"])
                    continue
                if self.db.queue_market_flow_signal(
                    condition_id, outcome, now,
                    int(self.cfg["flow_normal_cooldown_seconds"]), flow["avg_odds"]
                ):
                    self.normal_signals_queued += 1
                    LOG.info("FLOW DO RAPORTU: %s / %s | %.0f USD | %.0f%% | %d portfeli",
                             market.market_title, outcome, flow["amount"],
                             flow["share"] * 100, flow["wallets"])

    def normal_digest(self, now: int) -> bool:
        """Group signal markers by exact market+outcome; sum each stored BUY once."""
        rows = self.db.pending_normal_signals()
        if not rows:
            return False
        last = int(self.db.get_meta("last_normal_digest") or 0)
        if last and now - last < int(self.cfg["normal_digest_interval_seconds"]):
            LOG.info("DIGEST: oczekuje %d sygnalow do nastepnej paczki", len(rows))
            return False
        # Consecutive digest windows do not overlap. When starting without a
        # prior digest, use at most an hour of recently observed purchases.
        since = max(now - 6 * 3600, last + 1 if last else now - 3600)
        window_minutes = max(1, (now - since + 60) // 60)
        grouped: dict[tuple[str, str], dict] = {}
        for condition, outcome, kind, _usd, title, slug, _wallet, _avg_odds, market_title, _created in rows:
            group = grouped.setdefault((condition, outcome), {
                "condition": condition, "outcome": outcome, "title": title,
                "market_title": market_title, "slug": slug, "kinds": set(), "signals": 0,
            })
            group["kinds"].add(kind)
            group["signals"] += 1
        ranked = []
        for entry in grouped.values():
            n, amount, wallets, avg_odds = self.db.observed_buy_summary(
                entry["condition"], entry["outcome"], since, now + 1
            )
            entry.update({"buys": n, "usd": amount, "wallets": wallets, "avg_odds": avg_odds})
            ranked.append(entry)
        ranked.sort(key=lambda entry: (-entry["usd"], -entry["signals"], entry["title"]))
        lines = [
            f"📋 <b>CS2 RADAR — RAPORT ZAKUPÓW ({len(rows)} sygn.)</b>",
            f"Unikalne zaobserwowane zakupy BUY w oknie około {window_minutes} min, "
            "pogrupowane po konkretnym rynku i stronie.",
            "",
        ]
        for ix, entry in enumerate(ranked[:int(self.cfg["normal_digest_top_n"])], 1):
            label = "📊 PRZECHYLENIE" if "flow" in entry["kinds"] else "💰 ZAKUPY"
            lines.append(f"<b>{ix}. {label}: {html.escape(str(entry['outcome'])[:45])}</b> "
                         f"— {entry['usd']:,.0f} USD")
            lines.append(f"🎮 {html.escape(str(entry['title'])[:70])}")
            if entry["market_title"]:
                lines.append(f"🎯 {html.escape(str(entry['market_title'])[:70])}")
            lines.append(f"🔎 {entry['buys']} zakupów, {entry['wallets']} portfeli"
                         + (f", średni kurs ≈ {entry['avg_odds']:.2f}" if entry["buys"] else ""))
            slug = str(entry["slug"])
            if re.fullmatch(r"[A-Za-z0-9_-]{1,180}", slug):
                lines.append(f'<a href="https://polymarket.com/event/{slug}">Otwórz rynek</a>')
            if len("\n".join(lines)) > 3200:
                break
            lines.append("")
        if len(ranked) > int(self.cfg["normal_digest_top_n"]):
            lines.append(f"… i {len(ranked) - int(self.cfg['normal_digest_top_n'])} pozostałych kombinacji rynku i strony.")
        lines.append("ℹ️ Kwoty to pojedynczo policzone zakupy BUY z bazy, nie sumy pozycji 6/24h. "
                     "Brak sprzedaży, drobnych transakcji i pełnego orderbooka.")
        message = "\n".join(lines)
        if len(message) > 3750:
            message = "\n".join(lines[:3] + ["Szczegóły i wszystkie zakupy dostępne na stronie radaru."])
        if self.dry_run:
            LOG.info("DRY-RUN digest: %d sygnalow (bez wysylania na Telegram)", len(rows))
            with self.db.conn:
                self.db.conn.execute("UPDATE signal_candidates SET batch_key=? WHERE batch_key=''",
                                     (f"dryrun:{now}",))
                self.db.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                     ("last_normal_digest", str(now)))
            return True
        queued = self.db.queue_normal_digest(now, message)
        LOG.info("DIGEST: %d sygnalow, %d kombinacji rynku/wyniku, %d minut",
                 queued, len(ranked), window_minutes)
        return bool(queued)

    def daily_summary(self, now: int) -> bool:
        if not self.cfg["daily_summary_enabled"]:
            return False
        zone = ZoneInfo("Europe/Warsaw")
        local_now = datetime.fromtimestamp(now, zone)
        if local_now.hour < int(self.cfg["daily_summary_hour_local"]):
            return False
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        date_str = local_now.strftime("%Y-%m-%d")
        key = "daily-summary:" + date_str
        if not self.db.can_alert(key, now, 86400 * 3):
            return False
        rows = self.db.daily_top_positions(int(local_midnight.timestamp()), now + 1,
                                          int(self.cfg["daily_summary_top_n"]),
                                          float(self.cfg["alert_min_odds"]))
        title = f"📊 <b>CS2 RADAR — TOP ZAKUPY {date_str}</b>"
        lines = [title, "Największe zaobserwowane zakupy BRUTTO jednego portfela na jeden wynik w niższych ligach.", ""]
        for ix, (match, market_title, slug, outcome, wallet, total, fills, avg_odds) in enumerate(rows, 1):
            match = match[:125]
            outcome = outcome[:90]
            link = f"https://polymarket.com/event/{slug}"
            lines += [f"<b>{ix}. ${total:,.2f}</b> — {html.escape(outcome)} ({fills} zakupów, kurs śr. ≈ {avg_odds:.2f})",
                      f"🎮 {html.escape(match)}",
                      f"👛 <code>{html.escape(self.wallet_short(wallet))}</code> · <a href=\"{html.escape(link, quote=True)}\">Mecz</a>", ""]
        if not rows:
            lines.append("Brak zapisanych zakupów po kursie >= minimum alertow na rozpoznanych niższych ligach od północy.")
        lines += [f"ℹ️ Raport zawiera tylko zaobserwowane BUY po kursie >= {self.cfg['alert_min_odds']:.2f}, od początku dnia (PL). Nie są to aktualne otwarte pozycje ani pełny obrót; sprzedaży nie odejmujemy."]
        message = "\n".join(lines)
        if self.dry_run:
            LOG.info("DRY-RUN: raport dzienny: %d pozycji", len(rows))
            self.db.record_alert(key, now)
        else:
            self.db.queue_alert(key, now, message, [])
        self.daily_reports_queued += 1
        LOG.info("RAPORT DZIENNY: %d portfeli/rynków, data %s (kolejka Telegram)", len(rows), date_str)
        return True

    def process(self, raw: dict, cutoff: int) -> bool:
        trade = self.normalize(raw)
        if not trade or trade["timestamp"] < cutoff:
            return False
        m = self.markets.get(trade["condition_id"])
        if not m or trade["usd"] < number(self.cfg["api_minimum_trade_usd"]):
            return False
        if not self.db.add_trade(trade):
            return False
        self.db.remember_market(m)
        self.flow_conditions.add(trade["condition_id"])
        now = int(time.time())
        odds = 1.0 / trade['price']
        minimum_odds = number(self.cfg['alert_min_odds'])
        wallet = trade['wallet']
        signals: list[str] = []
        if odds < minimum_odds:
            # Nadal zapisujemy w bazie i w panelu; nie liczymy do klastrow alertowych.
            if self.cfg['diagnostic_trade_logs']:
                LOG.info('TX | %s | %s | %s | $%.2f | kurs %.2f | portfel %s | BRAK ALERTU: kurs < %.2f',
                         m.tier, m.title[:90].replace('|','/'), trade['outcome'][:45],
                         trade['usd'], odds, self.wallet_short(wallet), minimum_odds)
            return True
        threshold = min_alert_usd(odds, m.tier, self.cfg)
        count6, usd6, odds6, count24, usd24, odds24 = 0, 0., 0., 0, 0., 0.
        if wallet:
            count6, usd6, _, odds6 = self.db.recent_qualified(
                trade['condition_id'], trade['outcome'],
                trade['timestamp'] - int(self.cfg['position_window_seconds']), minimum_odds, wallet)
            count24, usd24, _, odds24 = self.db.recent_qualified(
                trade['condition_id'], trade['outcome'],
                trade['timestamp'] - int(self.cfg['position_24h_window_seconds']), minimum_odds, wallet)
        threshold6 = min_alert_usd(odds6, m.tier, self.cfg) * number(self.cfg['position_6h_threshold_multiplier'])
        threshold24 = min_alert_usd(odds24, m.tier, self.cfg) * number(self.cfg['position_24h_threshold_multiplier'])
        single = trade['usd'] >= threshold
        position6 = bool(wallet and count6 >= 2 and usd6 >= threshold6)
        position24 = bool(wallet and count24 >= 2 and usd24 >= threshold24)
        # Priorytet nie moze omijac filtra kursu i progow kwotowych.
        instant_limit = max(threshold * number(self.cfg['priority_threshold_multiplier']),
                            number(self.cfg['priority_min_usd']))
        instant_single = trade['usd'] >= instant_limit
        instant_position = bool(wallet and count6 >= 2 and usd6 >= 2 * threshold6)
        reason = f'kwota ${trade["usd"]:,.0f}/${threshold:,.0f}; 6h ${usd6:,.0f}/${threshold6:,.0f}; 24h ${usd24:,.0f}/${threshold24:,.0f}'
        if instant_single or instant_position:
            kind = 'single' if instant_single else 'position'
            key = f"priority:{trade['condition_id']}:{wallet or trade['id']}:{trade['outcome']}"
            if self.alert(key,kind,trade,m,now,usd6 or trade['usd'],count6 or 1,1,
                          cooldown=int(self.cfg['priority_cooldown_seconds']),
                          avg_odds=odds6 or odds):
                signals.append('PILNE')
            else: reason='priorytet juz wyslany (cooldown)'
        elif single or position6 or position24:
            kind = 'single' if single else ('position' if position6 else 'position24')
            displayed = trade['usd'] if single else (usd6 if position6 else usd24)
            effective = odds if single else (odds6 if position6 else odds24)
            if self.db.queue_normal_signal(trade,kind,now,int(self.cfg['normal_cooldown_seconds']),
                                           total_usd=displayed,avg_odds=effective):
                signals.append('DO ZBIORCZEGO')
                self.normal_signals_queued += 1
            else: reason='sygnal juz zarejestrowany dla portfela/wyniku (cooldown)'
        # Klaster liczymy tylko z zakupow o kursie >= alert_min_odds.
        since=trade['timestamp'] - int(self.cfg['cluster_window_seconds'])
        n,total,unique,cluster_odds=self.db.recent_qualified(
            trade['condition_id'],trade['outcome'],since,minimum_odds)
        cluster_min=max(number(self.cfg['multi_wallet_cluster_usd']),
                        min_alert_usd(cluster_odds,m.tier,self.cfg))
        if unique>=int(self.cfg['multi_wallet_min_wallets']) and total>=cluster_min:
            if total>=max(number(self.cfg['priority_cluster_usd']),cluster_min*1.5):
                key=f"priority-multi:{trade['condition_id']}:{trade['outcome']}"
                if self.alert(key,'multi',trade,m,now,total,n,unique,
                              cooldown=int(self.cfg['priority_cooldown_seconds']),
                              avg_odds=cluster_odds):
                    signals.append('PILNE KLASTER')
            elif not signals:
                if self.db.queue_normal_signal(trade,'multi',now,
                                               int(self.cfg['normal_cooldown_seconds']),
                                               total_usd=total,avg_odds=cluster_odds):
                    signals.append('KLASTER DO ZBIORCZEGO')
                    self.normal_signals_queued += 1
        if self.cfg['diagnostic_trade_logs']:
            verdict = 'ALERT '+','.join(signals) if signals else 'BRAK ALERTU '+reason
            LOG.info('TX | %s | %s | %s | $%.2f | kurs %.2f | portfel %s | %s',
                     m.tier,m.title[:90].replace('|','/'),trade['outcome'][:45].replace('|','/'),
                     trade['usd'],odds,self.wallet_short(wallet),verdict)
        return True

    def poll(self) -> None:
        if not self.event_ids:
            LOG.warning("Brak meczów CS2 do monitorowania (możliwe, że API nie zwróciło aktywnych rynków).")
            self.flush_outbox()
            return
        now = int(time.time())
        last_run = self.db.get_meta("last_successful_poll")
        # Po pierwszym uruchomieniu nie spamujemy zakładami historycznymi.
        # W GitHub Actions zadania bywają opóźnione. Odzyskaj do 1 godziny
        # zaległości, o ile API udostępnia jeszcze odpowiednie transakcje.
        cutoff = (max(now - 3600, min(now - int(self.cfg["lookback_seconds"]), int(last_run) - 90))
                  if last_run else max(self.started - 10, now - 60))
        processed = 0
        alerts_before = self.alerts_queued
        normal_before = self.normal_signals_queued
        coverage_complete = True
        # ID wydarzenia pozwala w jednym zapytaniu odczytać wszystkie zakłady z jego rynków.
        for start in range(0, len(self.event_ids), 20):
            chunk = self.event_ids[start:start + 20]
            cursor: str | None = None
            batch_done = False
            batch_rows: list[dict] = []
            for page_idx in range(int(self.cfg["max_pages_per_batch"])):
                params: dict[str, Any] = {
                    "event_id": ",".join(chunk), "taker_only": "false", "side": "BUY",
                    "filter_type": "CASH", "filter_amount": self.cfg["api_minimum_trade_usd"],
                    "limit": 500,
                }
                if cursor:
                    params["cursor"] = cursor
                payload = self.client.get(DATA, "/v2/trades", params)
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise RuntimeError("Data API /v2/trades: nieoczekiwany format odpowiedzi")
                rows: list[dict] = payload["data"]
                # Zbierz pełną paczkę stron: API zwraca od najnowszych.
                # Przetwarzanie chronologiczne po CAŁEJ paczce chroni
                # przed błędami łączenia zakupów w klastrach i oknach 6/24h.
                fresh = [r for r in rows if isinstance(r, dict) and int(number(r.get("timestamp"))) >= cutoff]
                batch_rows.extend(fresh)
                older_found = any(int(number(r.get("timestamp"))) < cutoff for r in rows if isinstance(r, dict))
                page_info = payload.get("pagination") or {}
                next_cursor = page_info.get("next_cursor")
                if older_found or not next_cursor or not rows:
                    batch_done = True
                    break
                cursor = str(next_cursor)
            for item in sorted(batch_rows, key=lambda r: int(number(r.get("timestamp")))):
                processed += int(self.process(item, cutoff))
            if not batch_done:
                LOG.warning("Limit paginacji osiągnięty w paczce eventów %s. Możliwa utrata części zakładów. Zwiększ max_pages_per_batch.", ",".join(chunk[:3]))
                coverage_complete = False
        if coverage_complete:
            self.db.set_meta("last_successful_poll", str(now))
        self.market_flow_alerts(now)
        self.normal_digest(now)
        self.daily_summary(now)
        self.db.cleanup(now)
        self.flush_outbox()
        LOG.info("Skan: %d nowych transakcji >= $%s (%d wydarzeń), PILNE: %d, do zbiorczego: %d, oczekujące: %d",
                 processed, self.cfg["api_minimum_trade_usd"], len(self.event_ids),
                 self.alerts_queued - alerts_before, self.normal_signals_queued - normal_before,
                 len(self.db.pending_normal_signals()))

    def run(self, once: bool = False) -> None:
        LOG.info("Uruchamiam CS2 Radar (%s)", "dry-run" if self.dry_run else "Telegram")
        while True:
            try:
                if not self.last_discovery or time.time() - self.last_discovery >= number(self.cfg["discover_seconds"]):
                    self.discover()
                self.poll()
                if once:
                    return
            except KeyboardInterrupt:
                raise
            except Exception:
                LOG.exception("Błąd skanowania. Spróbuję ponownie podczas kolejnego cyklu.")
                if once:
                    raise
            try:
                time.sleep(number(self.cfg["poll_seconds"]))
            except KeyboardInterrupt:
                break


def main():
    parser = argparse.ArgumentParser(description="CS2 Polymarket Radar: monitor nietypowych zakupów")
    parser.add_argument("--once", action="store_true", help="Pojedynczy skan API")
    parser.add_argument("--dry-run", action="store_true", help="Bez Telegrama, drukuje alerty na ekran")
    parser.add_argument("--test-alert", action="store_true", help="Wyślij testowy alert na Telegram, bez pobierania kursów")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout), RotatingFileHandler(BASE / "radar.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")])
    config = load_config(BASE / "config.json")
    env = load_env(BASE / ".env")
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if not args.dry_run and (not token or not chat):
        print("Brak tokena lub CHAT_ID. Uruchom najpierw konfiguracja.bat")
        raise SystemExit(2)
    client = ApiClient()
    if args.test_alert:
        client.post_telegram(token, chat, "✅ <b>CS2 Radar działa!</b>\nTo tylko test połączenia z Telegramem. Nie wykryto żadnej transakcji.")
        print("Wysłano test na Telegram.")
        return
    store = Database(str(BASE / "radar.sqlite3"))
    radar = Radar(client, store, config, token, chat, args.dry_run)
    if config["send_startup_notification"] and not args.dry_run and not args.once:
        try:
            client.post_telegram(token, chat, "🟢 <b>CS2 Polymarket Radar uruchomiony.</b>\nMonitoruję transakcje z publicznego API. Pierwszy skan nie wysyła historycznych alertów.")
        except Exception as e:
            LOG.warning("Nie udało się wysłać wiadomości startowej: %s", e)
    try:
        radar.run(once=args.once)
    except KeyboardInterrupt:
        LOG.info("Zatrzymano")
    finally:
        store.conn.close()


if __name__ == "__main__":
    main()
