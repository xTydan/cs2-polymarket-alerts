"""Optional live/upcoming fixture sync from https://www.hltv.org/matches.

Only pairs with matching names AND compatible dates are linked to Polymarket.
An inaccessible/empty HLTV page never proves that a match has ended.
"""
from __future__ import annotations

from html.parser import HTMLParser
from difflib import SequenceMatcher
from collections import defaultdict
import json
from pathlib import Path
import re
import time

import requests

from hltv_results import norm, teams_from_title, VOID_ELEMENTS

MATCHES_URL = "https://www.hltv.org/matches"
# Kickoff can slip by many hours (or a day) during long brackets.
# Pair and tournament identity take precedence over a fixed +/-18h cutoff.
MAX_RESCHEDULE_SECONDS = 72 * 3600
STALE_AFTER = 20 * 60
DISAPPEAR_GRACE = 15 * 60
UNCERTAIN_KEEP_SECONDS = 48 * 3600
MIN_CANDIDATE_MARGIN = 9.0
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
        self.event_depth = None
        self.event_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        classes = set((attr.get("class") or "").split())
        if self.card is None:
            if "match-wrapper" not in classes:
                return
            self.card = {
                "teams": [], "start": 0, "url": "", "event": "",
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
        if (classes & {"match-eventname", "match-event-name", "match-event"}
                and self.event_depth is None):
            self.event_depth = self.depth
            self.event_parts = []

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
        if self.card is not None and self.event_depth is not None:
            self.event_parts.append(data)

    def handle_endtag(self, tag):
        if self.card is None or tag in VOID_ELEMENTS:
            return
        if self.name_depth is not None and self.depth == self.name_depth:
            name = norm("".join(self.name_parts))
            if name:
                self.card["teams"].append(name)
            self.name_depth = None
            self.name_parts = []
        if self.event_depth is not None and self.depth == self.event_depth:
            self.card["event"] = " ".join("".join(self.event_parts).split())[:140]
            self.event_depth = None
            self.event_parts = []
        self.depth -= 1
        if self.depth == 0:
            if len(self.card["teams"]) == 2 and self.card["teams"][0] != self.card["teams"][1] and self.card["url"]:
                self.records.append(self.card)
            self.card = None


def parse_matches(html: str) -> list[dict]:
    parser = MatchesParser()
    parser.feed(html)
    return parser.records


# Do not merge academy/youth/female teams into their parent organisations.
TEAM_QUALIFIERS = frozenset({"academy", "junior", "juniors", "youth", "women",
                              "female", "fe", "nxt", "talent", "youngsters"})
EVENT_STOPWORDS = frozenset({"2025", "2026", "2027", "season", "series", "the",
                              "esports", "counter", "strike", "qualifier", "cup", "stage"})


def team_similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if (set(a.split()) & TEAM_QUALIFIERS) != (set(b.split()) & TEAM_QUALIFIERS):
        return 0.0
    if min(len(a), len(b)) < 6:
        return 0.0
    # Stylistic 0/o variant is a *candidate*, never an automatic exact alias.
    if a.replace("0", "o") == b.replace("0", "o") and len(a) >= 6:
        return 0.96
    ratio = SequenceMatcher(None, a, b).ratio()
    shared = {token for token in a.split() if len(token) >= 4} & {
        token for token in b.split() if len(token) >= 4
    }
    # Require near-identical long names and some common identity.
    if ratio >= 0.90 and (shared or a[:5] == b[:5]):
        return ratio
    return 0.0


def pair_similarity(poly_pair: tuple[str, str], hltv_pair: list[str]) -> float:
    if len(hltv_pair) != 2:
        return 0.0
    direct = (team_similarity(poly_pair[0], hltv_pair[0]),
              team_similarity(poly_pair[1], hltv_pair[1]))
    reverse = (team_similarity(poly_pair[0], hltv_pair[1]),
               team_similarity(poly_pair[1], hltv_pair[0]))
    best = max((direct, reverse), key=lambda values: min(values))
    return min(best) if min(best) >= 0.9 else 0.0


def event_name_similarity(a: str, b: str) -> float:
    first = set(norm(a).split()) - EVENT_STOPWORDS
    second = set(norm(b).split()) - EVENT_STOPWORDS
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def candidate_score(event: dict, fixture: dict, now: int,
                    previous_url: str = "") -> tuple[float, str] | None:
    pair = teams_from_title(event.get("title", ""))
    if not pair:
        return None
    similarity = pair_similarity(pair, fixture.get("teams", []))
    if similarity < .90:
        return None
    start = int(event.get("start") or 0)
    kickoff = int(fixture.get("start") or 0)
    status = fixture.get("status")
    if start and kickoff:
        gap = abs(start - kickoff)
        if gap > MAX_RESCHEDULE_SECONDS:
            return None
        time_bonus = max(1.0, 25 - gap / 3600 * .45)
        reliable_time = gap <= 18 * 3600
    elif start:
        # LIVE blocks often omit the start field; tolerate a bracket delay.
        # Keep the time window finite to avoid pairing last week's rematch.
        if status == "live":
            if not start - 6 * 3600 <= now <= start + MAX_RESCHEDULE_SECONDS:
                return None
        else:
            if not start - 7 * 86400 <= now <= start + 36 * 3600:
                return None
        time_bonus = 9.0
        reliable_time = status == "live" and 0 <= now - start <= 24 * 3600
    else:
        # No Polymarket kickoff; only very strong pair identity is enough
        # for a *probable* link, and the global uniqueness check still applies.
        if similarity < 0.999:
            return None
        time_bonus = 0.0
        reliable_time = False

    league = str(event.get("league") or "")
    competition = str(fixture.get("event") or "")
    event_bonus = 9.0 * event_name_similarity(league, competition) if league and competition else 0.0
    stable_bonus = 8.0 if previous_url and previous_url == fixture.get("url") else 0.0
    score = similarity * 100 + time_bonus + event_bonus + stable_bonus
    confidence = "confirmed" if similarity >= .999 and reliable_time else "probable"
    return score, confidence


def match_schedule(events: list[dict], fixtures: list[dict], now: int | None = None,
                   previous: dict | None = None) -> dict:
    """Conservative one-to-one matching: one HLTV id cannot map to two PM events.

    A delayed fixture can still match by pair, but vague/ambiguous links remain
    probable or unmatched and must not be portrayed as verified LIVE.
    """
    now = int(time.time()) if now is None else now
    previous = previous or {}
    candidates = {}
    for event in events:
        key = str(event.get("key") or "")
        if not key:
            continue
        options = []
        previous_url = str(previous.get(key, {}).get("url") or "")
        for fixture in fixtures:
            score = candidate_score(event, fixture, now, previous_url)
            if score is not None:
                options.append((score[0], fixture["url"], fixture, score[1]))
        options.sort(key=lambda item: (-item[0], item[1]))
        if not options:
            continue
        if len(options) > 1 and options[0][0] - options[1][0] < MIN_CANDIDATE_MARGIN:
            continue
        candidates[key] = options[0]

    fixture_competitors = defaultdict(list)
    for key, option in candidates.items():
        fixture_competitors[option[1]].append((key, option))
    found = {}
    for contenders in fixture_competitors.values():
        contenders.sort(key=lambda entry: (-entry[1][0], entry[0]))
        if len(contenders) > 1 and contenders[0][1][0] - contenders[1][1][0] < MIN_CANDIDATE_MARGIN:
            continue
        key, (score, _url, fixture, confidence) = contenders[0]
        found[key] = {
            "status": fixture["status"], "confidence": confidence,
            "source": "hltv-matches",
            "url": fixture["url"], "start": int(fixture.get("start") or 0),
            "event": fixture.get("event") or "",
            "observedAt": now,
        }
    return found


def reconcile(previous: dict, found: dict, events: list[dict], now: int) -> dict:
    """Missing from HLTV is NOT proof of start or finish, especially after delays."""
    valid = {str(e.get("key")) for e in events}
    merged = {}
    for key in valid:
        if key in found:
            merged[key] = dict(found[key])
            continue
        old = previous.get(key)
        if not isinstance(old, dict):
            continue
        if old.get("status") not in ("live", "upcoming", "uncertain", "archive"):
            continue
        row = dict(old)
        missing_since = int(row.get("missingSince") or now)
        row["missingSince"] = missing_since
        if row["status"] == "archive" and row.get("source") == "hltv-live-ended":
            # Legacy unverified archive is not a confirmed result.
            row["status"] = "uncertain"
        elif row["status"] in ("live", "upcoming") and now - missing_since >= DISAPPEAR_GRACE:
            row["status"] = "uncertain"
            row["source"] = "hltv-not-found"
            row["confirmed"] = False
        if now - int(row.get("observedAt") or 0) <= UNCERTAIN_KEEP_SECONDS:
            merged[key] = row
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
        matched = match_schedule(events, fixtures, now, previous=records)
        records = reconcile(records, matched, events, now)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"records": records, "lastOK": now}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        live = sum(f["status"] == "live" for f in fixtures)
        print(f"HLTV /matches: {len(fixtures)} meczow ({live} LIVE), {len(matched)} dopasowanych.")
        matched_urls = {row["url"] for row in matched.values()}
        missed_live = ["/".join(fixture["teams"]) for fixture in fixtures
                       if fixture["status"] == "live" and fixture["url"] not in matched_urls]
        if missed_live:
            print("HLTV /matches: LIVE bez dopasowania do Polymarket: "
                  + ", ".join(missed_live[:8]))
    except (requests.RequestException, ValueError, OSError) as err:
        print(f"HLTV /matches: brak wiarygodnego terminarza ({type(err).__name__}: {str(err)[:160]}).")
        # Freshness is checked by the dashboard. Don't claim ended from failed requests.
    return {key: value for key, value in records.items()
            if isinstance(value, dict) and value.get("status") in ("live", "upcoming", "uncertain")
            and (value.get("status") == "uncertain"
                 and now - int(value.get("observedAt") or 0) <= UNCERTAIN_KEEP_SECONDS
                 or value.get("status") in ("live", "upcoming")
                 and now - int(value.get("observedAt") or 0) <= STALE_AFTER)}
