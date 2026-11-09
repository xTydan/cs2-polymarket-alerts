"""Best-effort HLTV finished-match matcher. Not an official HLTV API.

Only unambiguous team pairs with a nearby published start and a match result
are marked as completed. A failed HLTV request never fails the dashboard.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time

import requests

HLTV_URL = "https://www.hltv.org/results"
MAX_AGE_SECONDS = 72 * 3600
MATCH_TOLERANCE_SECONDS = 18 * 3600
# HTML void elements do not have closing tags; counting them as nested
# elements prevents the result-con card from ever being finalized.
VOID_ELEMENTS = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
ALIAS = {
    "navi": "natus vincere",
    "na vi": "natus vincere",
    "virtus pro": "virtus pro",
    "vp": "virtus pro",
    "mouz nxt": "mouz nxt",
    # Polymarket uses a stylized zero, HLTV sometimes the letter o.
    # Match by exact canonical alias, never globally replace all 0/o characters.
    "underw0rld": "underworld",
}


def norm(name: str) -> str:
    name = str(name).casefold().replace("&", " and ")
    name = re.sub(r"[^\w\s]", " ", name, flags=re.UNICODE)
    name = " ".join(name.split())
    return ALIAS.get(name, name)


def teams_from_title(title: str) -> tuple[str, str] | None:
    text = re.sub(r"^(?:counter-strike(?:\s*2)?|cs2):\s*", "", str(title), flags=re.I)
    hit = re.search(r"^(.+?)\s+(?:vs\.?|versus)\s+(.+?)(?=\s*\(BO[135]\)|\s+-\s+|$)", text, re.I)
    if not hit:
        return None
    a, b = norm(hit.group(1)), norm(hit.group(2))
    return (a, b) if a and b and a != b else None


class ResultsParser(HTMLParser):
    """Extract published scores from result cards, not the page sidebar."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.card = None
        self.records = []
        self.field = None
        self.field_depth = 0
        self.score_fields = []
        self.timestamp = 0

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        classes = set(attr.get("class", "").split())
        if tag in VOID_ELEMENTS:
            return
        if "result-con" in classes and self.card is None:
            self.card = {"teams": [], "scores": [], "timestamp": 0, "url": ""}
            self.depth = 1
        elif self.card is not None:
            self.depth += 1
        else:
            return
        if self.card is None:
            return
        for key in ("data-zonedgrouping-entry-unix", "data-unix"):
            if key in attr:
                try:
                    val = int(attr[key])
                    if val > 10**11:
                        val //= 1000
                    if 1_400_000_000 < val < 4_000_000_000:
                        self.card["timestamp"] = val
                except (ValueError, TypeError):
                    pass
        if tag == "a" and attr.get("href", "").startswith("/matches/"):
            self.card["url"] = "https://www.hltv.org" + attr["href"].split("?")[0]
        if "team-cell" in classes or "team" in classes or "team1" in classes or "team2" in classes:
            if self.field is None:
                self.field = "team"
                self.field_depth = self.depth
        if "result-score" in classes or "result-score-container" in classes:
            if self.field is None:
                self.field = "score"
                self.field_depth = self.depth

    def handle_data(self, data):
        if self.card is not None and self.field and data.strip():
            target = "teams" if self.field == "team" else "scores"
            self.card[target].append(data.strip())

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            return
        if self.card is None:
            return
        if self.field and self.depth == self.field_depth:
            self.field = None
        self.depth -= 1
        if self.depth <= 0:
            self.records.append(self.card)
            self.card = None
            self.depth = 0


def parse_results(html: str) -> list[dict]:
    parser = ResultsParser()
    parser.feed(html)
    output = []
    for card in parser.records:
        teams = [norm(x) for x in card["teams"] if norm(x)]
        # HLTV has two named teams on a finished-match card.
        teams = list(dict.fromkeys(teams))
        # HLTV commonly renders <td class="result-score"><span>2</span><span>1</span></td>
        # without a literal dash; the old parser discarded these results.
        raw_score = " ".join(card["scores"])
        score_pair = re.fullmatch(r"\s*(\d+)\s*[-:]?\s+(\d+)\s*", raw_score)
        if score_pair is None:
            # Also supports a compact "2-1" with no spaces.
            score_pair = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", raw_score)
        if len(teams) != 2 or score_pair is None:
            continue
        a, b = (int(score_pair.group(1)), int(score_pair.group(2)))
        if a == b or max(a, b) > 5 or not card["url"]:
            continue
        output.append({"teams": teams, "score": [a, b], "timestamp": card["timestamp"], "url": card["url"]})
    return output


def match_results(events: list[dict], results: list[dict], now: int | None = None) -> dict:
    now = now or int(time.time())
    found = {}
    for event in events:
        event_id = str(event.get("key") or "")
        pair = teams_from_title(event.get("title", ""))
        start = int(event.get("start") or 0)
        if not event_id or not pair or not start or start > now + 300:
            continue
        candidates = []
        for result in results:
            if set(pair) != set(result["teams"]) or not result["timestamp"]:
                continue
            delta = abs(result["timestamp"] - start)
            if delta > MATCH_TOLERANCE_SECONDS or result["timestamp"] > now + 300:
                continue
            candidates.append((delta, result))
        candidates.sort(key=lambda item: item[0])
        # If two results for the same teams are near the planned start,
        # do not guess which match is the Polymarket event.
        if len(candidates) != 1:
            continue
        result = candidates[0][1]
        score = result["score"] if pair == tuple(result["teams"]) else result["score"][::-1]
        found[event_id] = {"status": "finished", "score": score, "url": result["url"], "confirmed": now}
    return found


def refresh(cache_path: Path, events: list[dict]) -> dict:
    now = int(time.time())
    try:
        old = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError):
        old = {}
    # Preserve confirmed results during temporary network errors, but keep
    # the cache bounded to events still used by the dashboard.
    valid_keys = {str(e.get("key")) for e in events}
    old = {k: v for k, v in old.items()
           if k in valid_keys and isinstance(v, dict) and v.get("status") == "finished"}
    try:
        response = requests.get(HLTV_URL, headers={
            "User-Agent": "Mozilla/5.0 (compatible; CS2Radar/1.0)",
            "Accept": "text/html",
        }, timeout=12)
        response.raise_for_status()
        results = parse_results(response.text)
        new = match_results(events, results, now)
        old.update(new)
        if not results:
            # A 200 response can still be a Cloudflare challenge or changed markup.
            # Treat zero results as unavailable, not as proof no matches finished.
            sample = response.text
            print(f"HLTV: 0 wynikow (HTTP {response.status_code}, HTML {len(response.content)} B, "
                  f"result-con={sample.count('result-con')}, team-cell={sample.count('team-cell')}, "
                  f"result-score={sample.count('result-score')}). Sprawdz Cloudflare/format strony.")
        else:
            print(f"HLTV: {len(results)} wynikow, {len(new)} dopasowanych meczow.")
    except (requests.RequestException, ValueError, UnicodeError) as err:
        print(f"HLTV: brak odczytu ({type(err).__name__}); zachowano potwierdzone wyniki.")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(old, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return old
