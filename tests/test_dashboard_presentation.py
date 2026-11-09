import unittest

from build_dashboard import SCRIPT, STYLE, build_page


class DashboardPresentationTests(unittest.TestCase):
    def test_unverified_sections_are_closed_by_default(self):
        self.assertIn("['live',true]", SCRIPT)
        self.assertIn("['upcoming',true]", SCRIPT)
        self.assertIn("['started',false]", SCRIPT)
        self.assertIn("['undated',false]", SCRIPT)
        self.assertIn("['archive',false]", SCRIPT)

    def test_sections_can_be_toggled_with_accessible_controls(self):
        self.assertIn("toggleSection.setAttribute('aria-expanded'", SCRIPT)
        self.assertIn("toggleSection.setAttribute('aria-controls'", SCRIPT)
        self.assertIn("sectionOpen.set(key,!expanded);render()", SCRIPT)
        self.assertIn("if(!expanded){list.hidden=true;content.append(list);continue;}", SCRIPT)
        self.assertIn(".fixtures-toggle", STYLE)

    def test_market_selection_is_by_condition_id_with_activity(self):
        self.assertIn("eRows.filter(r=>r.c===m.c)", SCRIPT)
        self.assertIn("o.value=item.m.c", SCRIPT)
        self.assertIn("r.c===selectedMarket", SCRIPT)
        self.assertIn("W aktualnych filtrach:", SCRIPT)
        self.assertIn("W zapisanej historii radaru:", SCRIPT)
        self.assertIn("Nie oznacza to zerowego obrotu", SCRIPT)

    def test_html_generation_remains_valid(self):
        html = build_page({"generated":"2026-11-09T19:00:00+00:00",
                           "lastScan":0,"trades":[],"markets":[],"hltvResults":{}})
        self.assertIn("const sectionOpen=new Map(", html)
        self.assertIn("</html>", html)


if __name__ == "__main__":
    unittest.main()
