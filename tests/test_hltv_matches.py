import unittest

from hltv_matches import match_schedule, parse_matches, reconcile


MATCHES_HTML = """
<section class="liveMatchesSection">
  <div class="match-wrapper" live="true">
    <a href="/matches/12345/aimclub-vs-alliance">
      <div class="match-team"><img src="/a.png">
        <div class="match-teamname">aimclub</div>
      </div>
      <div class="match-team-livescore">7 - 8</div>
      <div class="match-team"><img src="/b.png">
        <div class="match-teamname">Alliance</div>
      </div>
    </a>
  </div>
</section>
<div class="upcomingMatchesSection">
  <div class="match-wrapper" live="false">
    <a href="/matches/12346/9ine-vs-fokus">
      <span class="match-time" data-unix="1800000200000">15:00</span>
      <span class="match-team"><span class="match-teamname">9INE</span></span>
      <span class="match-team"><span class="match-teamname">FOKUS</span></span>
    </a>
  </div>
</div>
<div class="match-wrapper">
  <a href="/matches/12347/old-match">
    <span class="match-time" data-unix="1800000200000">15:00</span>
    <span class="match-teamname">Other A</span>
    <span class="match-teamname">Other B</span>
  </a>
</div>
"""


class HltvMatchesTests(unittest.TestCase):
    def test_parse_live_and_upcoming(self):
        rows = parse_matches(MATCHES_HTML)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["teams"], ["aimclub", "alliance"])
        self.assertEqual(rows[0]["status"], "live")
        self.assertEqual(rows[1]["teams"], ["9ine", "fokus"])
        self.assertEqual(rows[1]["start"], 1800000200)
        self.assertEqual(rows[1]["status"], "upcoming")
        self.assertEqual(rows[2]["status"], "upcoming")

    def test_bestia_underw0rld_polymarket_matches_live_underworld_hltv(self):
        now = 1800000000
        html = ('<div class="match-wrapper" live="true">'
                '<a href="/matches/2400097/bestia-academy-vs-underworld-cct-2026-south-america-series-8">'
                '<div class="match-team"><span class="match-teamname">BESTIA Academy</span></div>'
                '<div class="match-team-livescore">5 - 3</div>'
                '<div class="match-team"><span class="match-teamname">underworld</span></div>'
                '</a></div>')
        fixtures = parse_matches(html)
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0]["status"], "live")
        self.assertEqual(fixtures[0]["teams"], ["bestia academy", "underworld"])
        events = [{"key": "cs2-bestia-underw0rld",
                   "title": "Counter-Strike: BESTIA Academy vs underw0rld (BO3) - CCT South America",
                   "start": now - 600}]
        matched = match_schedule(events, fixtures, now)
        self.assertIn("cs2-bestia-underw0rld", matched)
        self.assertEqual(matched["cs2-bestia-underw0rld"]["status"], "live")
        self.assertIn("/2400097/", matched["cs2-bestia-underw0rld"]["url"])
        # Different academy teams must not match an alias accidentally.
        other = [{"key": "different", "title": "BESTIA Academy vs underworld Academy (BO3)",
                  "start": now - 600}]
        self.assertEqual(match_schedule(other, fixtures, now), {})

    def test_same_teams_not_wrong_event(self):
        fixtures = parse_matches(MATCHES_HTML)
        events = [
            {"key": "aimclub-alliance", "title": "Counter-Strike: aimclub vs Alliance (BO3)", "start": 1800000000},
            {"key": "aimclub-nine", "title": "aimclub vs 9INE (BO3)", "start": 1800000000},
            {"key": "nine-fokus", "title": "9INE vs FOKUS (BO3)", "start": 1800000000},
        ]
        found = match_schedule(events, fixtures, now=1800000500)
        self.assertEqual(set(found), {"aimclub-alliance", "nine-fokus"})
        self.assertEqual(found["aimclub-alliance"]["status"], "live")
        self.assertEqual(found["nine-fokus"]["status"], "upcoming")

    def test_do_not_archive_unmatched_upcoming(self):
        previous = {"a": {"status": "upcoming", "source": "hltv-matches", "observedAt": 100}}
        self.assertEqual(reconcile(previous, {}, [{"key": "a"}], 1000), {})

    def test_archive_only_previously_seen_live_after_grace(self):
        events = [{"key": "a"}]
        previous = {"a": {"status": "live", "source": "hltv-matches", "observedAt": 100}}
        once = reconcile(previous, {}, events, 1000)
        self.assertEqual(once["a"]["status"], "live")
        self.assertEqual(once["a"]["missingSince"], 1000)
        later = reconcile(once, {}, events, 1950)
        self.assertEqual(later["a"]["status"], "archive")
        self.assertEqual(later["a"]["source"], "hltv-live-ended")
        self.assertFalse(later["a"]["confirmed"])

    def test_ambiguous_pair_is_not_matched(self):
        f = parse_matches(MATCHES_HTML)
        events = [{"key": "a", "title": "9INE vs FOKUS", "start": 1800000000}]
        self.assertEqual(match_schedule(events, [f[1], f[1]], now=1800000000), {})


if __name__ == "__main__":
    unittest.main()
