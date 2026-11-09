import unittest

from hltv_results import match_results, norm, parse_results, teams_from_title


class HLTVResultsTests(unittest.TestCase):
    def test_title_and_alias(self):
        self.assertEqual(teams_from_title("Counter-Strike: NAVI vs G2 (BO3)"), ("natus vincere", "g2"))
        self.assertEqual(norm("MOUZ-NXT"), "mouz nxt")

    def test_parse_one_result_card(self):
        html = ('<div class="result-con" data-zonedgrouping-entry-unix="1794222000000">'
                '<a href="/matches/123/team-a-vs-team-b">'
                '<div class="team1"><div class="team">Team A</div></div>'
                '<div class="result-score"><span>2</span><span>-</span><span>1</span></div>'
                '<div class="team2"><div class="team">Team B</div></div>'
                '</a></div>')
        records = parse_results(html)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["teams"], ["team a", "team b"])
        self.assertEqual(records[0]["score"], [2, 1])

    def test_real_hltv_table_structure_without_dash(self):
        html = ('<div class="result-con" data-zonedgrouping-entry-unix="1794222000000">'
                '<a class="a-reset" href="/matches/123/team-a-vs-team-b">'
                '<table><tr>'
                '<td class="team-cell"><div class="team">Team A</div></td>'
                '<td class="result-score"><span class="score-won">2</span>'
                '<span class="score-lost">1</span></td>'
                '<td class="team-cell"><div class="team">Team B</div></td>'
                '</tr></table></a></div>')
        records = parse_results(html)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["score"], [2, 1])
        self.assertEqual(records[0]["teams"], ["team a", "team b"])

    def test_match_result_reverse_teams(self):
        results = [{"teams": ["team b", "team a"], "score": [2, 1],
                    "timestamp": 1800000000, "url": "https://www.hltv.org/matches/1/test"}]
        events = [{"key": "event-a", "title": "Team A vs Team B (BO3)", "start": 1800000200}]
        actual = match_results(events, results, now=1800010000)
        self.assertEqual(actual["event-a"]["score"], [1, 2])

    def test_ambiguous_match_is_not_assumed(self):
        row = {"teams": ["team a", "team b"], "score": [2, 0],
               "timestamp": 1800000000, "url": "https://www.hltv.org/matches/1/test"}
        events = [{"key": "event-a", "title": "Team A vs Team B", "start": 1800000200}]
        self.assertEqual(match_results(events, [row, dict(row)], now=1800010000), {})


if __name__ == "__main__":
    unittest.main()
