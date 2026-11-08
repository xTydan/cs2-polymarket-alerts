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
    "test_mode": False
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
    if not 0 < number(config["relative_market_volume"]) <= 1:
        raise ValueError("relative_market_volume musi być w przedziale (0,1]")
    return config


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
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        result = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return result[0] if result else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def add_trade(self, trade: dict) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?)",
                (trade["id"], trade["timestamp"], trade["condition_id"], trade["token_id"],
                 trade["wallet"], trade["outcome"], trade["usd"], trade["price"]),
            )
        return cur.rowcount == 1

    def remember_market(self, market: EventMarket) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO trade_market_info VALUES (?,?,?,?,?)",
                (market.condition_id, market.title, market.market_title, market.event_slug, market.tier)
            )

    def daily_top_positions(self, since: int, until: int, limit: int) -> list[tuple]:
        # Obserwowane zakupy brutto, a nie saldo portfela czy prawdziwe otwarte pozycje.
        return self.conn.execute("""
            SELECT i.title, i.market_title, i.slug, t.outcome, t.wallet,
                   SUM(t.usd) AS total, COUNT(*) AS fills
            FROM trades t
            JOIN trade_market_info i ON i.condition_id = t.condition_id
            WHERE t.timestamp >= ? AND t.timestamp < ? AND i.tier = 'lower'
                  AND LENGTH(t.wallet) > 0
            GROUP BY t.condition_id, t.outcome, t.wallet
            ORDER BY total DESC, fills DESC LIMIT ?
        """, (since, until, limit)).fetchall()

    def recent(self, condition_id: str, outcome: str, since: int, wallet: str | None = None) -> tuple[int, float, int]:
        query = "SELECT COUNT(*), COALESCE(SUM(usd),0), COUNT(DISTINCT wallet) FROM trades WHERE condition_id=? AND outcome=? AND timestamp>=?"
        args: list[Any] = [condition_id, outcome, since]
        if wallet:
            query += " AND wallet=?"
            args.append(wallet)
        count, value, unique = self.conn.execute(query, args).fetchone()
        return int(count), float(value), int(unique)

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

    def cleanup(self, now: int) -> None:
        with self.conn:
            # Historia dashboardu: przechowuj 30 dni. 24h wystarcza do liczenia alertow.
            self.conn.execute("DELETE FROM trades WHERE timestamp<?", (now - 30 * 86400,))
            self.conn.execute("DELETE FROM cooldowns WHERE sent_at<?", (now - 7 * 86400,))
            self.conn.execute("DELETE FROM wallet_stats WHERE checked_at<?", (now - 2 * 86400,))


class Radar:
    def __init__(self, client: ApiClient, db: Database, config: dict, token: str = "", chat_id: str = "", dry_run: bool = False):
        self.client, self.db, self.cfg = client, db, config
        self.token, self.chat_id, self.dry_run = token, chat_id, dry_run
        self.markets: dict[str, EventMarket] = {}
        self.event_ids: list[str] = []
        self.tag_ids: list[str] = []
        self.started = int(time.time())
        self.alerts_queued = 0
        self.daily_reports_queued = 0
        self.last_discovery = 0.0
        self.csv_path = BASE / "alerty.csv"
        self.cache_path = BASE / "market_cache.json"
        self.load_market_cache()

    def cache_signature(self) -> str:
        # Odrzucenie cache po zmianie ustawień klasyfikacji.
        data = {k: self.cfg[k] for k in (
            "max_events", "exclude_major_tournaments", "max_market_volume_unknown_usd")}
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
            volume = number(ev.get("volume"), 0)
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
                    tier=tier, outcomes=[str(o) for o in raw_outcomes]
                )
                added += 1
            if added:
                ev_ids.add(eid)
                matches += 1
        self.markets, self.event_ids = markets, sorted(ev_ids, key=int)
        self.last_discovery = time.time()
        self.save_market_cache()
        LOG.info("Odkryto %s wydarzeń CS2 i %s rynków; tagi: %s", matches, len(markets), ",".join(self.tag_ids))

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
                wallet_note: str = "") -> str:
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
              total: float = 0, count: int = 0, unique: int = 0) -> bool:
        if not self.db.can_alert(key, now, int(self.cfg["alert_cooldown_seconds"])):
            return False
        note = self.wallet_history(trade["wallet"], now) if kind == "single" else ""
        msg = self.message(kind, m, trade, total, count, unique, note)
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
                                          int(self.cfg["daily_summary_top_n"]))
        title = f"📊 <b>CS2 RADAR — TOP ZAKUPY {date_str}</b>"
        lines = [title, "Największe zaobserwowane zakupy BRUTTO jednego portfela na jeden wynik w niższych ligach.", ""]
        for ix, (match, market_title, slug, outcome, wallet, total, fills) in enumerate(rows, 1):
            match = match[:125]
            outcome = outcome[:90]
            link = f"https://polymarket.com/event/{slug}"
            lines += [f"<b>{ix}. ${total:,.2f}</b> — {html.escape(outcome)} ({fills} zakupów)",
                      f"🎮 {html.escape(match)}",
                      f"👛 <code>{html.escape(self.wallet_short(wallet))}</code> · <a href=\"{html.escape(link, quote=True)}\">Mecz</a>", ""]
        if not rows:
            lines.append("Brak zapisanych zakupów ≥ progu API na rozpoznanych niższych ligach od północy.")
        lines += ["ℹ️ To tylko transakcje zaobserwowane przez radar od początku dnia (PL), od progu API. Nie są to aktualne otwarte pozycje; sprzedaży nie odejmujemy. Dane mogą być niepełne."]
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
        now = int(time.time())
        volume = m.market_volume
        relative = (volume is not None and volume > 0 and
                    trade["usd"] / volume >= number(self.cfg["relative_market_volume"]))
        tiny = volume is not None and volume <= number(self.cfg["low_volume_market_usd"])
        low = m.tier == "lower"
        threshold = number(self.cfg["single_buy_lower_usd"] if low
                           else self.cfg["single_buy_unknown_usd"])
        pos_threshold = number(self.cfg["position_lower_usd"] if low
                               else self.cfg["position_unknown_usd"])
        pos24_threshold = number(self.cfg["position_24h_lower_usd"] if low
                                 else self.cfg["position_24h_unknown_usd"])
        large_single = (trade["usd"] >= threshold or
                        trade["usd"] >= number(self.cfg["absolute_whale_usd"]) or
                        (trade["usd"] >= number(self.cfg["minimum_buy_usd"]) and (relative or tiny)))
        signals: list[str] = []
        reason = "poniżej progów"
        wallet = trade["wallet"]
        if wallet:
            since_position = trade["timestamp"] - int(self.cfg["position_window_seconds"])
            pos_count, pos_sum, _ = self.db.recent(trade["condition_id"], trade["outcome"], since_position, wallet)
            since_24 = trade["timestamp"] - int(self.cfg["position_24h_window_seconds"])
            pos24_count, pos24_sum, _ = self.db.recent(trade["condition_id"], trade["outcome"], since_24, wallet)
            key = f"walletactivity:{trade['condition_id']}:{wallet}:{trade['outcome']}"
            if large_single:
                if self.alert(key, "single", trade, m, now, pos_sum, pos_count, 1):
                    signals.append("single")
                else:
                    reason = "cooldown/alert już w kolejce"
            elif pos_count >= 2 and pos_sum >= pos_threshold:
                if self.alert(key, "position", trade, m, now, pos_sum, pos_count, 1):
                    signals.append("position6h")
                else:
                    reason = "cooldown/alert już w kolejce"
            elif pos24_count >= 2 and pos24_sum >= pos24_threshold:
                if self.alert(key, "position24", trade, m, now, pos24_sum, pos24_count, 1):
                    signals.append("position24h")
                else:
                    reason = "cooldown/alert już w kolejce"
            else:
                reason = f"zakup < ${threshold:,.0f}; portfel 6h ${pos_sum:,.0f}/${pos_threshold:,.0f}, 24h ${pos24_sum:,.0f}/${pos24_threshold:,.0f}"
        elif large_single:
            key = f"single-nonwallet:{trade['condition_id']}:{trade['id']}"
            if self.alert(key, "single", trade, m, now, trade["usd"], 1, 0):
                signals.append("single")
        else:
            reason = f"brak identyfikatora portfela; zakup < ${threshold:,.0f}"

        since = trade["timestamp"] - int(self.cfg["cluster_window_seconds"])
        n, total, unique = self.db.recent(trade["condition_id"], trade["outcome"], since)
        if unique >= int(self.cfg["multi_wallet_min_wallets"]) and total >= number(self.cfg["multi_wallet_cluster_usd"]):
            key = f"clustermulti:{trade['condition_id']}:{trade['outcome']}"
            if self.alert(key, "multi", trade, m, now, total, n, unique):
                signals.append("multi-wallet")
        if self.cfg["diagnostic_trade_logs"]:
            verdict = "ALERT " + ",".join(signals) if signals else "BRAK ALERTU " + reason
            LOG.info("TX | %s | %s | %s | $%.2f | portfel %s | %s",
                     m.tier, m.title[:90].replace("|", "/"), trade["outcome"][:45].replace("|", "/"),
                     trade["usd"], self.wallet_short(wallet), verdict)
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
        self.daily_summary(now)
        self.db.cleanup(now)
        self.flush_outbox()
        LOG.info("Skan: %d nowych transakcji >= $%s (%d wydarzeń), %d nowych sygnałów",
                 processed, self.cfg["api_minimum_trade_usd"], len(self.event_ids),
                 self.alerts_queued - alerts_before)

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
