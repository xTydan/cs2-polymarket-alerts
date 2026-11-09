"""Optional live/upcoming fixture sync from https://www.hltv.org/matches.

Only pairs with matching names AND compatible dates are linked to Polymarket.
An inaccessible/empty HLTV page never proves that a match has ended.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time

import requests

from hltv_results import norm, teams_from_title, VOID_ELEMENTS

MATCHES_URL = "https://www.hltv.org/matches"
START_TOLERANCE = 18 * 3600
STALE_AFTER = 20 * 60
DISAPPEAR_GRACE = 15 * 60
VALID_URL = re.compile(r"^https://www\.hltv\.org/matches/\d+/[A-Za-z0-9_-]+$")


def unix_timestamp(value) -> int:
    try:
        stamp = int(value)
        if stamp > 10**11:
            stamp //= 1000
        return stamp if 1_400_000_000 < stamp < 4_000_000_000 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


class MatchesParser(HTMLParser):
    """Read .match-wrapper blocks and the two .match-teamname elements.

    HLTV uses <div class="match-wrapper" live="true"> for live matches and
    the same wrapper without the live attribute for upcoming fixtures.
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.card = None
        self.records: list[dict] = []
        self.name_depth = None
        self.name_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())
        if self.card is None:
            if "match-wrapper" not in classes:
                return
            self.card = {
                "teams": [], "start": 0, "url": "",
                "status": "live" if attr.get("live", "").lower() == "true"
                            or "live-match-container" in classes else "upcoming",
            }
            self.depth = 0

        # A void element doesn't increase DOM nesting and has no closing tag.
        if tag not in VOID_ELEMENTS:
            self.depth += 1

        if "match-team-livescore" in classes or attr.get("live", "").lower() == "true":
            self.card["status"] = "live"

        if "match-teamname" in classes and self.name_depth is None:
            self.name_depth = self.depth
            self.name_parts = []

        # Live cards can have many timestamps; prefer the match-time field.
        if "match-time" in classes and attr.get("data-unix"):
            ts = unix_timestamp(attr["data-unix"])
            if ts:
                self.card["start"] = ts
        if not self.card["start"] and "data-unix" in attr:
            self.card["start"] = unix_timestamp(attr["data-unix"])

        href = attr.get("href") or ""
        if tag == "a" and href.startswith("/matches/"):
            url = "https://www.hltv.org" + href.split("?")[0]
            if VALID_URL.fullmatch(url):
                self.card["url"] = url

    def handle_data(self, data):
        if self.card is not None and self.name_depth is not None:
            self.name_parts.append(data)

    def handle_endtag(self, tag):
        if self.card is None or tag in VOID_ELEMENTS:
            return
        if self.name_depth is not None and self.depth == self.name_depth:
            name = norm("".join(self.name_parts))
            if name:
                self.card["teams"].append(name)
            self.name_depth = None
            self.name_parts = []
        self.depth -= 1
        if self.depth == 0:
            if len(self.card["teams"]) == 2 and self.card["teams"][0] != self.card["teams"][1] and self.card["url"]:
                self.records.append(self.card)
            self.card = None


def parse_matches(html: str) -> list[dict]:
    parser = MatchesParser()
    parser.feed(html)
    return parser.records


def match_schedule(events: list[dict], fixtures: list[dict], now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    found = {}
    for event in events:
        key = str(event.get("key") or "")
        pair = teams_from_title(event.get("title", ""))
        start = int(event.get("start") or 0)
        if not key or not pair or not start:
            continue
        candidates = []
        for fixture in fixtures:
            if set(pair) != set(fixture["teams"]):
                continue
            kickoff = int(fixture.get("start") or 0)
            if kickoff:
                delta = abs(kickoff - start)
                if delta > START_TOLERANCE:
                    continue
            else:
                # Some live cards have no kickoff. Only consider near-now events.
                if not start - 4 * 3600 <= now <= start + 10 * 3600:
                    continue
                delta = START_TOLERANCE
            candidates.append((delta, fixture))
        # Never merge two fixtures with the same pair into one Polymarket event.
        if len(candidates) != 1:
            continue
        fixture = candidates[0][1]
        found[key] = {
            "status": fixture["status"],
            "source": "hltv-matches",
            "url": fixture["url"],
            "start": int(fixture.get("start") or 0),
            "observedAt": now,
        }
    return found


def reconcile(previous: dict, found: dict, events: list[dict], now: int) -> dict:
    """Keep confirmed disappeared-from-LIVE matches; never infer from noncoverage."""
    valid = {str(e.get("key")) for e in events}
    merged = {}
    for key in valid:
        if key in found:
            merged[key] = dict(found[key])
            continue
        old = previous.get(key)
        if not isinstance(old, dict):
            continue
        status = old.get("status")
        if status == "archive" and old.get("source") == "hltv-live-ended":
            merged[key] = old
        elif status == "live":
            row = dict(old)
            first_missing = int(row.get("missingSince") or now)
            row["missingSince"] = first_missing
            if now - first_missing >= DISAPPEAR_GRACE:
                row["status"] = "archive"
                row["source"] = "hltv-live-ended"
                row["observedAt"] = now
                row["confirmed"] = False
                row.pop("missingSince", None)
            merged[key] = row
        # When an upcoming match vanishes, it can be rescheduled or renamed.
        # Don't claim it finished and don't keep an outdated 'upcoming' status.
    return merged


def refresh_matches(cache_path: Path, events: list[dict]) -> dict:
    now = int(time.time())
    try:
        old = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        if not isinstance(old, dict):
            old = {}
        records = old.get("records", {})
        if not isinstance(records, dict):
            records = {}
    except (OSError, ValueError, TypeError):
        records = {}

    try:
        response = requests.get(
            MATCHES_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; CS2Radar/1.0)",
                     "Accept": "text/html"},
            timeout=12,
        )
        response.raise_for_status()
        fixtures = parse_matches(response.text)
        # A healthy HLTV /matches document lists many upcoming matches. Avoid
        # confusing a challenge page or changed HTML with a completed schedule.
        if len(fixtures) < 3:
            raise ValueError(f"parser: {len(fixtures)} cards, match-wrapper="
                             f"{response.text.count('match-wrapper')}, HTML={len(response.content)} B")
        matched = match_schedule(events, fixtures, now)
        records = reconcile(records, matched, events, now)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"records": records, "lastOK": now}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        live = sum(f["status"] == "live" for f in fixtures)
        print(f"HLTV /matches: {len(fixtures)} meczow ({live} LIVE), {len(matched)} dopasowanych.")
    except (requests.RequestException, ValueError, OSError) as err:
        print(f"HLTV /matches: brak wiarygodnego terminarza ({type(err).__name__}: {str(err)[:160]}).")
        # Freshness is checked by the dashboard. Don't claim ended from failed requests.
    return {key: value for key, value in records.items()
            if isinstance(value, dict) and value.get("status") in ("live", "upcoming", "archive")
            and (value.get("status") == "archive"
                 or now - int(value.get("observedAt") or 0) <= STALE_AFTER)}
