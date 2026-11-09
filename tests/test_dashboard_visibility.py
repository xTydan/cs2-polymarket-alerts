import unittest

from build_dashboard import SCRIPT, STYLE, build_page


class DashboardVisibilityTests(unittest.TestCase):
    def test_clear_statuses_and_high_contrast(self):
        self.assertIn(".fixture-when.live{background:", STYLE)
        self.assertIn(".fixture-when.upcoming{background:", STYLE)
        self.assertIn(".fixture-when.possible{background:", STYLE)
        self.assertIn(".fixture-teams,.fixture-team{font-size:15px", STYLE)
        self.assertIn(".fixture-total{font-size:17px}", STYLE)
        self.assertIn(".fixture-stage.active", STYLE)

    def test_buy_totals_still_visible_on_mobile(self):
        # Old mobile stylesheet hides the side amounts: later overrides undo it.
        self.assertIn("@media(max-width:670px)", STYLE)
        self.assertIn(".fixture-sides{grid-column:1/-1;grid-row:3;display:grid;", STYLE)
        self.assertIn("@media(max-width:440px)", STYLE)
        self.assertIn(".fixture-sides{grid-column:1/-1;grid-row:4}", STYLE)
        self.assertIn(".fixture-odds{grid-column:2/4;grid-row:5;text-align:left}", STYLE)

    def test_columns_are_labeled_without_losing_sections(self):
        self.assertIn("fixture-column-head", SCRIPT)
        self.assertIn("'ZAKUPY BUY','OBRÓT PM','MAX KURS'", SCRIPT)
        self.assertIn("if(!expanded){list.hidden=true;content.append(list);continue;}", SCRIPT)

    def test_explanations_are_collapsed_but_available(self):
        markup = build_page({"generated":"2026-11-09T20:00:00+00:00",
                             "lastScan":0,"trades":[],"markets":[],"hltvResults":{}})
        self.assertIn('<details class="note disclosures">', markup)
        self.assertIn("Zakupy BUY ≠ pełny obrót", markup)
        self.assertIn("</details>", markup)
        self.assertIn("48 godzinach", markup)
        self.assertIn("window.__RADAR_DATA__", markup)


if __name__ == "__main__":
    unittest.main()
