"""The dashboard's UI toolkit and Live page structure. Several of these guard bugs that were only visible in a screenshot:
a hidden banner that still showed, label styles that leaked onto numbers, a double-escaped label, and cards that overflowed a
phone screen."""
import re
import unittest

from nifty_core.selector import readiness as RD
from tests._base import NoNetworkTestCase
from tools import charts, live_ui, ui_kit as K


class CssGuardTests(NoNetworkTestCase):
    def test_the_hidden_attribute_always_wins_over_display_rules(self):
        """The 'lost connection' banner is flex-styled AND hidden; without this rule it showed permanently."""
        self.assertIn("[hidden]{display:none!important}", K.CSS)

    def test_tile_labels_style_only_the_label_never_a_span_inside_a_value(self):
        for leaking in (".kpi span{", ".tile span{", ".fact span{"):
            self.assertNotIn(leaking, K.CSS)
        for direct in (".kpi>span", ".tile>span", ".fact>span"):
            self.assertIn(direct, K.CSS)

    def test_grid_children_can_shrink_so_nothing_overflows_a_phone_screen(self):
        self.assertIn("min-width:0", K.CSS)
        self.assertIn("grid-template-columns:minmax(0,1fr)", K.CSS)                  # the single-column layout
        self.assertEqual(len(re.findall(r"minmax\(min\(\d+px,100%\)", K.CSS)) >= 3, True)

    def test_both_themes_and_reduced_motion_are_supported(self):
        self.assertIn("prefers-color-scheme:dark", K.CSS)
        self.assertIn("prefers-reduced-motion:reduce", K.CSS)
        self.assertIn("color-scheme:light dark", K.CSS)

    def test_no_external_assets_so_it_works_offline(self):
        self.assertNotRegex(K.CSS, r"https?://")
        self.assertNotIn("@import", K.CSS)
        page = K.page("t", "<p>x</p>")
        self.assertNotRegex(page, r"(src|href)=['\"]?https?://")


class ComponentTests(NoNetworkTestCase):
    def test_labels_are_escaped_exactly_once(self):
        self.assertIn("Day P&amp;L", K.kpi("Day P&L", "1"))
        self.assertNotIn("&amp;amp;", K.kpi("Day P&L", "1"))
        self.assertNotIn("<script", K.badge("<script>x</script>", "bad"))
        self.assertNotIn("<img", K.pill("<img src=x>"))
        self.assertNotIn("<b>", K.fold("k", "<b>t</b>", "x", meta="<i>"))

    def test_every_status_has_a_word_and_an_icon_not_colour_alone(self):
        for status in ("PASSED", "FAILED", "ERROR", "PENDING", "N/A", "NOT CONFIRMED"):
            html = K.pill(status)
            self.assertIn(status, html)
            self.assertIn("aria-hidden=true", html)                                 # the icon is decorative; the word carries it
        for tone in ("ok", "warn", "bad", "info", "mute"):
            self.assertIn("<i aria-hidden=true>", K.badge("X", tone))

    def test_a_progress_bar_is_clamped_and_accessible(self):
        self.assertIn("width:50%", K.bar(1, 2))
        self.assertIn("width:100%", K.bar(9, 2))
        self.assertIn("width:0%", K.bar(0, 0))
        self.assertIn("role=progressbar", K.bar(1, 2))

    def test_a_fold_carries_a_key_so_the_page_can_keep_it_open(self):
        closed, opened = K.fold("chain", "T", "x"), K.fold("chain", "T", "x", open_=True)
        self.assertIn("data-k='chain'", closed)
        self.assertNotIn(" open>", closed)
        self.assertIn(" open>", opened)

    def test_the_stepper_marks_done_current_and_blocked_steps(self):
        steps = list(RD.STEPS)
        html = K.stepper({"steps": steps, "current": 2, "state": "waiting"})
        self.assertEqual((html.count("class='done'"), html.count("now waiting")), (2, 1))
        blocked = K.stepper({"steps": steps, "current": 0, "state": "blocked"})
        self.assertIn("now blocked", blocked)
        self.assertIn(">!<", blocked)
        done = K.stepper({"steps": steps, "current": 4, "state": "done"})
        self.assertEqual(done.count("class='done'"), 4)
        self.assertEqual(K.stepper(None), "")

    def test_a_segmented_bar_is_proportional_and_empty_when_there_is_nothing(self):
        html = K.segbar(3, 1, 0)
        self.assertIn("width:75.0%", html)
        self.assertIn("width:25.0%", html)
        self.assertEqual(K.segbar(0, 0, 0), "")


class BandChartTests(NoNetworkTestCase):
    ZONES = [{"label": "Mid-range band", "low": "24,332.78", "high": "24,405.72"},
             {"label": "Bullish trend level", "price": "24,400.00"}]

    def test_it_draws_one_row_per_zone_and_where_the_market_is_now(self):
        svg = charts.band_svg(self.ZONES, "24,386.50", "24,296.30", "24,442.20")
        self.assertIn("Mid-range band", svg)
        self.assertIn("Bullish trend level", svg)
        self.assertIn("24,332.78 – 24,405.72", svg)
        self.assertIn("now 24,386.50", svg)
        self.assertIn("role=img", svg)

    def test_nothing_is_drawn_without_zones_or_a_price(self):
        self.assertEqual(charts.band_svg([], "24,386.50"), "")
        self.assertEqual(charts.band_svg(self.ZONES, None), "")
        self.assertEqual(charts.band_svg([{"label": "x", "price": "bad"}], "1"), "")

    def test_labels_are_escaped(self):
        self.assertNotIn("<script", charts.band_svg([{"label": "<script>", "price": "1"}], "1"))


class LiveScriptTests(NoNetworkTestCase):
    def test_the_refresh_keeps_open_sections_and_skips_unchanged_pages(self):
        js = live_ui.LIVE_SCRIPT
        self.assertIn("details[data-k]", js)                      # remembers which folds the reader opened
        self.assertIn("if (t === last)", js)                      # nothing changed: leave the page alone
        self.assertIn("window.scrollTo(0, y)", js)                # and keep the scroll position
        self.assertIn("setInterval(tick, 1000)", js)
        self.assertEqual(js.count("fetch("), 1)                   # still GET-only, one fetch

    def test_the_connection_banner_starts_hidden(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        html = live_ui.render_live(os.path.join(d, "nothing.json"), d)
        self.assertRegex(html, r"<div id=conn class='alert' hidden>")


class ReadinessUiModelTests(NoNetworkTestCase):
    """The backend supplies the stepper, the groups and the disclaimer, so the page derives nothing."""

    def test_the_stepper_position_follows_the_status(self):
        cases = {RD.WAITING_SIGNAL: (0, "waiting"), RD.NO_VALID: (0, "idle"), RD.WAITING_CONFIRMATION: (1, "waiting"),
                 RD.READY_RISK: (2, "waiting"), RD.READY_EXECUTE: (3, "waiting"), RD.EXECUTED: (4, "done"), RD.BLOCKED: (0, "blocked")}
        for status, (current, state) in cases.items():
            p = RD._stepper(status)
            self.assertEqual((p["current"], p["state"], p["steps"]), (current, state, list(RD.STEPS)), status)

    def test_a_risk_block_and_a_refused_fill_block_at_the_right_step(self):
        self.assertEqual(RD._stepper(RD.BLOCKED, "risk")["current"], 2)
        self.assertEqual(RD._stepper(RD.BLOCKED, "fill")["current"], 3)
        self.assertEqual(RD._stepper(RD.BLOCKED, "technical")["current"], 0)

    def test_finalize_moves_the_stepper_with_the_outcome(self):
        from tests.unit.test_readiness import build
        from tests.unit.test_readiness import selection
        for status, current, state in (("ENTERED", 4, "done"), ("AWAITING_APPROVAL", 3, "waiting"), ("BLOCKED", 2, "blocked"),
                                       ("ENTRY_NOT_FILLED", 3, "blocked")):
            r = build(selection("bullish_trend", "12:30"), {"have": 2})
            RD.finalize(r, status, {"risk": {"reasons": []}, "reasons_not_filled": []})
            self.assertEqual((r["progress"]["current"], r["progress"]["state"]), (current, state), status)

    def test_every_checklist_row_has_a_group_and_the_order_is_gate_market_execution_extra(self):
        from tests.unit.test_readiness import build, selection
        r = build(selection("bullish_trend", "12:30"), {"have": 1})
        groups = [x["group"] for x in r["checklist"]]
        self.assertEqual(groups[0], "gate")
        self.assertEqual(set(groups), {"gate", "market", "execution", "extra"})
        order = {"gate": 0, "market": 1, "execution": 2, "extra": 3}
        key = [order[g] for g in groups]
        self.assertEqual(key[:key.index(2)], sorted(key[:key.index(2)]))
        self.assertTrue(all(x["group"] == "execution" for x in r["checklist"] if x["name"].startswith(("option structure", "confirmation", "risk gate", "worst case"))))
        vol = next(x for x in r["checklist"] if x["name"].startswith("volume"))
        self.assertEqual(vol["group"], "extra")

    def test_the_disclaimer_is_its_own_field_and_the_action_stays_complete(self):
        from tests.unit.test_readiness import build, selection
        r = build(selection("bullish_trend", "12:30"), {"have": 1})
        self.assertNotIn("promised", r["next_action_main"])
        self.assertIn("no time or price is promised", r["disclaimer"])
        self.assertEqual(r["next_action"], r["next_action_main"] + " " + r["disclaimer"])
        RD.finalize(r, "ENTERED", {})
        self.assertEqual(r["disclaimer"], "")                                       # nothing left to disclaim once it has traded


if __name__ == "__main__":
    unittest.main()
