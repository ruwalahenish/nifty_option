"""Strategy engine tests: fixtures end to end, then hand-modified chains for every filter and boundary."""
import datetime
import json
import os
import unittest
from decimal import Decimal as Dec

from nifty_core.data.fixture_provider import FixtureProvider, Scenario
from nifty_core.data.models import OptionChain, OptionContract, OptionQuote, make_event
from nifty_core.errors import ConfigError
from nifty_core.market.ist import IST, UTC, parse_ist
from nifty_core.regime import engine as RE
from nifty_core.regime import params as RP
from nifty_core.strategy import engine as E
from nifty_core.strategy import params as SP
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RPARAMS = RP.load_params()
DEFAULT = SP.load_params()
BULL, BEAR = "bullish_trend", "bearish_trend"


def params(**override):
    v = {f: getattr(DEFAULT, f) for f in SP.FIELDS}
    v.update(override)
    return SP.build_params(v)


def at(name, hhmm):
    return parse_ist("%s %s" % (sc.scenario(name).day.isoformat(), hhmm))


def provider(name):
    return FixtureProvider(sc.scenario(name))


def decide_at(name, hhmm, p=DEFAULT, cfg=None):
    return E.decide_at(provider(name), sc.calendar(), sc.specs(), cfg or sc.config(), p, RPARAMS, at(name, hhmm))


def parts(name, hhmm):
    """(regime, chain, events, as_of) exactly as decide_at would fetch them."""
    prov, as_of = provider(name), at(name, hhmm)
    regime = RE.classify_at(prov, sc.calendar(), as_of, RPARAMS)
    return regime, prov.get_option_chain(as_of=as_of), prov.get_event_calendar(as_of.date()), as_of


def decide(regime, chain, events, as_of, p=DEFAULT, cfg=None):
    return E.decide(regime, chain, events, as_of, cfg or sc.config(), p, RPARAMS, sc.specs(), sc.calendar())


def edit_chain(chain, fn=None, spot=None, drop=()):
    """A copy of a chain with each quote passed through fn(quote) -> quote (or None to drop it)."""
    quotes = []
    for q in chain.quotes:
        if q.contract in drop:
            continue
        q2 = fn(q) if fn else q
        if q2 is not None:
            quotes.append(q2)
    return OptionChain(chain.underlying, chain.snapshot_time, spot or chain.spot, quotes, chain.data_label, chain.source)


def codes(decision):
    return [r.code for r in decision.reasons]


def all_rejection_codes(decision):
    return {r.code for c in decision.considered for r in c.rejections}


def with_quote(chain, contract, **changes):
    return edit_chain(chain, lambda q: q._replace(**changes) if q.contract == contract else q)


class FixtureDecisionTests(NoNetworkTestCase):
    def test_bullish_fixture_gives_a_bull_call_candidate(self):
        d = decide_at(BULL, "12:30")
        self.assertEqual(d.outcome, E.CANDIDATE)
        c = d.candidate
        self.assertEqual(c.strategy_type, "BULL_CALL_DEBIT")
        self.assertEqual(c.regime_at_entry["label"], "BULLISH")
        self.assertEqual(c.decision_time, at(BULL, "12:30"))
        self.assertEqual((c.long_contract.option_type, c.short_contract.option_type), ("CE", "CE"))
        self.assertLess(c.long_contract.strike, c.short_contract.strike)
        self.assertEqual(c.long_contract.expiry, c.short_contract.expiry)

    def test_bearish_fixture_gives_a_bear_put_candidate(self):
        d = decide_at(BEAR, "14:30")
        self.assertEqual(d.outcome, E.CANDIDATE)
        c = d.candidate
        self.assertEqual(c.strategy_type, "BEAR_PUT_DEBIT")
        self.assertEqual(c.regime_at_entry["label"], "BEARISH")
        self.assertEqual((c.long_contract.option_type, c.short_contract.option_type), ("PE", "PE"))
        self.assertGreater(c.long_contract.strike, c.short_contract.strike)

    def test_candidate_carries_the_information_the_risk_engine_needs(self):
        regime, chain, events, as_of = parts(BULL, "12:30")
        c = decide(regime, chain, events, as_of).candidate
        lq, sq = chain.get(c.long_contract), chain.get(c.short_contract)
        self.assertEqual(c.entry_price_long, lq.ask)        # pay the ask on the long leg
        self.assertEqual(c.entry_price_short, sq.bid)       # receive the bid on the short leg
        self.assertEqual(c.net_debit_points, lq.ask - sq.bid)
        self.assertEqual(c.width_points, c.short_contract.strike - c.long_contract.strike)
        lot = sc.specs().lot_size(c.expiry, "fixture").value
        self.assertEqual(c.lot_size, lot)
        self.assertEqual(c.net_debit_inr, c.net_debit_points * lot * c.lots)
        self.assertEqual(c.chain_snapshot_time, chain.snapshot_time)
        self.assertEqual(c.spread.net_debit_points, c.net_debit_points)
        self.assertEqual(c.data_label, "FIXTURE_SYNTHETIC")
        self.assertEqual(c.liquidity["long"]["symbol"], c.long_contract.symbol)
        self.assertEqual(c.liquidity["short"]["side"], "SELL")
        self.assertEqual(c.assumptions["strategy_thresholds"]["fingerprint"], DEFAULT.fingerprint)
        self.assertEqual(c.assumptions["regime_thresholds_fingerprint"], RPARAMS.fingerprint)
        self.assertEqual(c.assumptions["entry_window_ist"], ["10:15", "14:30"])
        self.assertIn("not been sized", c.note)

    def test_journal_record_is_plain_json(self):
        d = decide_at(BULL, "12:30")
        text = json.dumps(E.decision_to_journal(d))
        back = json.loads(text)
        self.assertEqual(back["outcome"], "CANDIDATE")
        self.assertEqual(back["candidate"]["strategy_type"], "BULL_CALL_DEBIT")
        self.assertEqual(len(back["considered"]), 4)
        no = json.loads(json.dumps(E.decision_to_journal(decide_at("sideways", "12:30"))))
        self.assertIsNone(no["candidate"])
        self.assertEqual(no["reasons"][0]["code"], "REGIME_SIDEWAYS")

    def test_the_candidate_has_no_sizing_or_risk_verdict_fields(self):
        names = " ".join(E.TradeCandidate._fields + E.StrategyDecision._fields).lower()
        for word in ("approved", "risk", "stop", "target", "profit", "sizing", "position", "order", "fill"):
            self.assertNotIn(word, names, word)

    def test_strategy_code_never_touches_risk_limits_or_the_cost_engine(self):
        for fname in ("engine.py", "params.py"):
            with open(os.path.join(ROOT, "nifty_core", "strategy", fname), encoding="utf-8") as fh:
                text = fh.read()
            for word in ("risk_per_trade", "max_daily_loss", "account_capital", "defined_risk", "CostEngine",
                         "max_trades_per_day", "max_lots", "slippage"):
                self.assertNotIn(word, text, "%s mentions %s" % (fname, word))

    def test_every_non_candidate_regime_gives_no_trade_with_a_reason(self):
        cases = (("sideways", "12:30", "SIDEWAYS"), ("high_volatility", "12:30", "HIGH_VOLATILITY"),
                 ("missing_invalid_data", "12:30", "NO_DATA"), ("illiquid_options", "12:30", "SIDEWAYS"),
                 (BULL, "14:30", "NO_TRADE"), (BULL, "10:30", "SIDEWAYS"))
        for name, hhmm, label in cases:
            d = decide_at(name, hhmm)
            self.assertEqual(d.regime.label, label, (name, hhmm))
            self.assertEqual(d.outcome, E.NO_TRADE, (name, hhmm))
            self.assertIsNone(d.candidate)
            self.assertEqual(codes(d), ["REGIME_" + label], (name, hhmm))
            self.assertTrue(d.reasons[0].text)

    def test_no_trade_regime_reason_carries_the_regime_detail(self):
        d = decide_at(BULL, "14:30")
        self.assertIn("INSUFFICIENT_EVIDENCE", d.reasons[0].text)
        d = decide_at(BULL, "14:45")                                   # after the window: both reasons are listed
        self.assertEqual(codes(d), ["OUTSIDE_ENTRY_WINDOW", "REGIME_NO_TRADE"])
        self.assertIn("CONFLICTING_EVIDENCE", d.reasons[1].text)
        d = decide_at("missing_invalid_data", "12:30")
        self.assertIn("GAP_IN_CANDLES", d.reasons[0].text)

    def test_bullish_and_bearish_regimes_can_still_give_no_trade(self):
        regime, chain, events, as_of = parts(BULL, "12:30")
        thin = edit_chain(chain, lambda q: q._replace(volume=10, open_interest=10))
        d = decide(regime, thin, events, as_of)
        self.assertEqual(regime.label, "BULLISH")
        self.assertEqual((d.outcome, codes(d)), (E.NO_TRADE, ["NO_SUITABLE_CANDIDATE"]))
        self.assertEqual(len(d.considered), 4)
        self.assertTrue(all(c.rejections for c in d.considered))

    def test_a_decision_is_deterministic(self):
        a = json.dumps(E.decision_to_journal(decide_at(BULL, "12:30")), sort_keys=True)
        b = json.dumps(E.decision_to_journal(decide_at(BULL, "12:30")), sort_keys=True)
        self.assertEqual(a, b)

    def test_sweep_every_five_minutes_on_every_fixture_obeys_the_invariants(self):
        cfg = sc.config()
        start, end = cfg.entry_window_start_ist, cfg.entry_window_end_ist
        found = {"BULL_CALL_DEBIT": 0, "BEAR_PUT_DEBIT": 0}
        for name in sc.NAMES:
            for minutes in range(9 * 60 + 15, 15 * 60 + 40, 5):
                hhmm = "%02d:%02d" % divmod(minutes, 60)
                d = decide_at(name, hhmm)
                self.assertTrue(d.outcome in (E.CANDIDATE, E.NO_TRADE))
                if d.outcome == E.NO_TRADE:
                    self.assertTrue(d.reasons, (name, hhmm))
                    self.assertIsNone(d.candidate)
                    continue
                c = d.candidate
                self.assertTrue(start <= hhmm <= end, (name, hhmm))
                self.assertIn(d.regime.label, ("BULLISH", "BEARISH"))
                self.assertEqual(c.strategy_type, "BULL_CALL_DEBIT" if d.regime.label == "BULLISH" else "BEAR_PUT_DEBIT")
                self.assertEqual(c.chain_snapshot_time <= c.decision_time, True)
                self.assertEqual(c.spread.long_leg.is_long, True)
                self.assertEqual(c.spread.short_leg.is_long, False)
                found[c.strategy_type] += 1
        self.assertGreater(found["BULL_CALL_DEBIT"], 0)
        self.assertGreater(found["BEAR_PUT_DEBIT"], 0)


class EntryWindowTests(NoNetworkTestCase):
    def setUp(self):
        super(EntryWindowTests, self).setUp()
        self.regime, self.chain, self.events, self.as_of = parts(BULL, "12:30")

    def run_with(self, **changes):
        return decide(self.regime, self.chain, self.events, self.as_of, cfg=sc.config()._replace(**changes))

    def test_window_start_and_end_are_inclusive(self):
        self.assertEqual(self.run_with(entry_window_start_ist="12:30").outcome, E.CANDIDATE)
        self.assertEqual(self.run_with(entry_window_end_ist="12:30").outcome, E.CANDIDATE)

    def test_one_minute_outside_the_window_gives_no_trade(self):
        d = self.run_with(entry_window_start_ist="12:31")
        self.assertEqual((d.outcome, codes(d)), (E.NO_TRADE, ["OUTSIDE_ENTRY_WINDOW"]))
        self.assertIn("before", d.reasons[0].text)
        d = self.run_with(entry_window_end_ist="12:29")
        self.assertEqual((d.outcome, codes(d)), (E.NO_TRADE, ["OUTSIDE_ENTRY_WINDOW"]))
        self.assertIn("after", d.reasons[0].text)

    def test_after_the_window_even_a_bearish_regime_gives_no_trade(self):
        d = decide_at(BEAR, "14:35")
        self.assertEqual(d.regime.label, "BEARISH")
        self.assertEqual((d.outcome, codes(d)), (E.NO_TRADE, ["OUTSIDE_ENTRY_WINDOW"]))

    def test_default_window_opens_when_the_regime_engine_can_first_classify(self):
        cfg = sc.config()
        self.assertEqual((cfg.entry_window_start_ist, cfg.entry_window_end_ist, cfg.flat_by_ist),
                         ("10:15", "14:30", "15:15"))
        session = sc.calendar().session(sc.scenario(BULL).day)
        E.check_alignment(cfg, RPARAMS, session)                        # no error
        first = datetime.datetime.combine(session.date, session.open) + datetime.timedelta(
            minutes=RPARAMS.min_bars * RPARAMS.interval_minutes)
        self.assertEqual(first.strftime("%H:%M"), cfg.entry_window_start_ist)

    def test_a_window_that_opens_before_the_regime_can_classify_is_a_config_error(self):
        session = sc.calendar().session(sc.scenario(BULL).day)
        with self.assertRaises(ConfigError):
            E.check_alignment(sc.config()._replace(entry_window_start_ist="10:10"), RPARAMS, session)
        with self.assertRaises(ConfigError):
            self.run_with(entry_window_start_ist="09:45")
        E.check_alignment(sc.config()._replace(entry_window_start_ist="10:20"), RPARAMS, session)

    def test_window_is_configurable_not_hard_coded(self):
        d = self.run_with(entry_window_start_ist="10:15", entry_window_end_ist="13:00", flat_by_ist="13:30")
        self.assertEqual(d.outcome, E.CANDIDATE)

    def test_closed_market_gives_no_trade(self):
        sat = parse_ist("2026-09-19 12:30")
        d = E.decide(self.regime, self.chain, (), sat, sc.config(), DEFAULT, RPARAMS, sc.specs(), sc.calendar())
        self.assertEqual(codes(d), ["MARKET_CLOSED"])


class QuoteFilterTests(NoNetworkTestCase):
    """Each filter is checked at its exact boundary on a modified copy of the bullish 12:30 chain."""

    def setUp(self):
        super(QuoteFilterTests, self).setUp()
        self.regime, self.chain, self.events, self.as_of = parts(BULL, "12:30")
        self.base = decide(self.regime, self.chain, self.events, self.as_of).candidate
        self.long_c, self.short_c = self.base.long_contract, self.base.short_contract

    def only_candidate_decision(self, chain, p=None):
        """Restrict the search to the base candidate's exact (offset, width) so one leg's quote decides the outcome."""
        offset = self.base.selection["offset_steps"]
        p = p or params(long_strike_offsets_steps=[offset], width_points_options=[int(self.base.width_points)])
        return decide(self.regime, chain, self.events, self.as_of, p=p)

    def rejections(self, decision):
        return [r.code for c in decision.considered for r in c.rejections]

    def test_the_base_case_passes(self):
        self.assertEqual(self.only_candidate_decision(self.chain).outcome, E.CANDIDATE)

    def test_one_sided_quotes_are_rejected(self):
        for field in ("bid", "ask"):
            for contract in (self.long_c, self.short_c):
                chain = with_quote(self.chain, contract, **{field: None})
                d = self.only_candidate_decision(chain)
                self.assertEqual(d.outcome, E.NO_TRADE, (field, contract.symbol))
                self.assertTrue(set(self.rejections(d)) & {"NO_BID", "NO_ASK", "ONE_SIDED_QUOTE"})

    def test_zero_bid_or_ask_is_rejected(self):
        for field in ("bid", "ask"):
            chain = with_quote(self.chain, self.short_c, **{field: Dec("0")})
            d = self.only_candidate_decision(chain)
            self.assertEqual(d.outcome, E.NO_TRADE, field)
            self.assertIn("ONE_SIDED_QUOTE", self.rejections(d))

    def test_missing_quote_is_rejected(self):
        d = self.only_candidate_decision(edit_chain(self.chain, drop=(self.short_c,)))
        self.assertEqual(d.outcome, E.NO_TRADE)
        self.assertIn("MISSING_QUOTE", self.rejections(d))

    def test_stale_quote_is_rejected_at_the_limit_boundary(self):
        limit = sc.config().data_stale_after_seconds
        old = self.as_of - datetime.timedelta(seconds=limit)
        ok = with_quote(self.chain, self.long_c, timestamp=old)
        self.assertEqual(self.only_candidate_decision(ok).outcome, E.CANDIDATE)
        too_old = with_quote(self.chain, self.long_c, timestamp=old - datetime.timedelta(seconds=1))
        d = self.only_candidate_decision(too_old)
        self.assertEqual(d.outcome, E.NO_TRADE)
        self.assertIn("STALE_DATA", self.rejections(d))

    def test_wide_spread_boundary(self):
        q = self.chain.get(self.long_c)
        limit = DEFAULT.max_leg_spread_pct
        mid = q.mid
        half = mid * limit / 100 / 2                              # spread == limit% of mid exactly
        ok = with_quote(self.chain, self.long_c, bid=mid - half, ask=mid + half)
        qq = ok.get(self.long_c)
        self.assertEqual(qq.spread_pct_of_mid, limit)
        # snap to the tick so the chain stays valid: choose a spread of exactly limit% on a clean mid
        clean = with_quote(self.chain, self.long_c, bid=Dec("100.00"), ask=Dec("103.05"))
        self.assertGreater(clean.get(self.long_c).spread_pct_of_mid, limit)
        d = self.only_candidate_decision(clean)
        self.assertIn("WIDE_SPREAD", self.rejections(d))
        inside = with_quote(self.chain, self.long_c, bid=Dec("100.00"), ask=Dec("102.95"))
        self.assertLess(inside.get(self.long_c).spread_pct_of_mid, limit)
        self.assertNotIn("WIDE_SPREAD", self.rejections(self.only_candidate_decision(inside)))
        # a looser limit lets the same quote through
        loose = params(max_leg_spread_pct=Dec("4"), long_strike_offsets_steps=[self.base.selection["offset_steps"]],
                       width_points_options=[int(self.base.width_points)])
        self.assertNotIn("WIDE_SPREAD", self.rejections(self.only_candidate_decision(clean, loose)))

    def test_exact_spread_limit_is_accepted_and_one_tick_wider_is_not(self):
        # spread 3.00 on mid 100.00 -> exactly 3.0%
        edge = with_quote(self.chain, self.long_c, bid=Dec("98.50"), ask=Dec("101.50"))
        self.assertEqual(edge.get(self.long_c).spread_pct_of_mid, DEFAULT.max_leg_spread_pct)
        self.assertNotIn("WIDE_SPREAD", self.rejections(self.only_candidate_decision(edge)))
        over = with_quote(self.chain, self.long_c, bid=Dec("98.50"), ask=Dec("101.55"))
        self.assertIn("WIDE_SPREAD", self.rejections(self.only_candidate_decision(over)))

    def test_volume_boundary(self):
        for contract in (self.long_c, self.short_c):
            at_min = with_quote(self.chain, contract, volume=DEFAULT.min_leg_volume)
            self.assertNotIn("LOW_VOLUME", self.rejections(self.only_candidate_decision(at_min)))
            below = with_quote(self.chain, contract, volume=DEFAULT.min_leg_volume - 1)
            self.assertIn("LOW_VOLUME", self.rejections(self.only_candidate_decision(below)))
            none = with_quote(self.chain, contract, volume=None)
            self.assertIn("MISSING_VOLUME", self.rejections(self.only_candidate_decision(none)))

    def test_open_interest_boundary(self):
        for contract in (self.long_c, self.short_c):
            at_min = with_quote(self.chain, contract, open_interest=DEFAULT.min_leg_open_interest)
            self.assertNotIn("LOW_OPEN_INTEREST", self.rejections(self.only_candidate_decision(at_min)))
            below = with_quote(self.chain, contract, open_interest=DEFAULT.min_leg_open_interest - 1)
            self.assertIn("LOW_OPEN_INTEREST", self.rejections(self.only_candidate_decision(below)))
            none = with_quote(self.chain, contract, open_interest=None)
            self.assertIn("MISSING_OPEN_INTEREST", self.rejections(self.only_candidate_decision(none)))

    def test_depth_boundary_uses_the_traded_side_and_the_contract_layer_lot_size(self):
        lot = self.base.lot_size
        need = lot * DEFAULT.min_depth_lots
        ok = with_quote(self.chain, self.long_c, ask_qty=need)
        self.assertNotIn("THIN_DEPTH", self.rejections(self.only_candidate_decision(ok)))
        thin = with_quote(self.chain, self.long_c, ask_qty=need - 1)
        self.assertIn("THIN_DEPTH", self.rejections(self.only_candidate_decision(thin)))
        thin_short = with_quote(self.chain, self.short_c, bid_qty=need - 1)
        self.assertIn("THIN_DEPTH", self.rejections(self.only_candidate_decision(thin_short)))
        # the side we do NOT trade is irrelevant
        other = with_quote(self.chain, self.long_c, bid_qty=0)
        self.assertNotIn("THIN_DEPTH", self.rejections(self.only_candidate_decision(other)))
        missing = with_quote(self.chain, self.long_c, ask_qty=None)
        self.assertIn("MISSING_DEPTH", self.rejections(self.only_candidate_decision(missing)))

    def test_debit_to_width_boundary(self):
        width = self.base.width_points
        debit = self.base.net_debit_points
        exact = debit / width                                   # a limit equal to the real ratio passes
        p_ok = params(max_debit_to_width=exact, long_strike_offsets_steps=[self.base.selection["offset_steps"]],
                      width_points_options=[int(width)])
        self.assertEqual(self.only_candidate_decision(self.chain, p_ok).outcome, E.CANDIDATE)
        p_no = params(max_debit_to_width=exact - Dec("0.0001"),
                      long_strike_offsets_steps=[self.base.selection["offset_steps"]],
                      width_points_options=[int(width)])
        d = self.only_candidate_decision(self.chain, p_no)
        self.assertEqual(d.outcome, E.NO_TRADE)
        self.assertIn("DEBIT_TOO_HIGH_VS_WIDTH", self.rejections(d))

    def test_credit_or_zero_debit_is_never_a_candidate(self):
        chain = with_quote(self.chain, self.short_c, bid=self.chain.get(self.long_c).ask + Dec("5"),
                           ask=self.chain.get(self.long_c).ask + Dec("6"))
        d = self.only_candidate_decision(chain)
        self.assertEqual(d.outcome, E.NO_TRADE)
        self.assertTrue(any(code.startswith("SPREAD_INVALID_") for code in self.rejections(d)))

    def test_strict_thresholds_remove_all_candidates_rather_than_forcing_one(self):
        d = decide(self.regime, self.chain, self.events, self.as_of, p=params(min_leg_volume=10 ** 9))
        self.assertEqual((d.outcome, codes(d)), (E.NO_TRADE, ["NO_SUITABLE_CANDIDATE"]))
        self.assertIn("LOW_VOLUME", d.reasons[0].text)

    def test_unavailable_strikes_give_no_trade(self):
        d = decide(self.regime, self.chain, self.events, self.as_of, p=params(width_points_options=[5000]))
        self.assertEqual(d.outcome, E.NO_TRADE)
        self.assertIn("MISSING_QUOTE", all_rejection_codes(d))


class ChainGateTests(NoNetworkTestCase):
    def setUp(self):
        super(ChainGateTests, self).setUp()
        self.regime, self.chain, self.events, self.as_of = parts(BULL, "12:30")

    def test_missing_chain(self):
        d = decide(self.regime, None, self.events, self.as_of)
        self.assertEqual(codes(d), ["CHAIN_UNAVAILABLE"])

    def test_snapshot_staleness_boundary(self):
        limit = sc.config().data_stale_after_seconds
        for extra, expected in ((limit, E.CANDIDATE), (limit + 1, E.NO_TRADE)):
            later = self.chain.snapshot_time + datetime.timedelta(seconds=extra)
            regime = RE.classify_at(provider(BULL), sc.calendar(), later, RPARAMS)
            d = decide(regime, self.chain, self.events, later)
            self.assertEqual(regime.label, "BULLISH")
            self.assertEqual(d.outcome, expected, extra)
            if expected == E.NO_TRADE:
                self.assertIn("CHAIN_STALE", codes(d))

    def test_chain_from_the_future_is_refused(self):
        later_chain = provider(BULL).get_option_chain(as_of=at(BULL, "14:30"))
        d = decide(self.regime, later_chain, self.events, self.as_of)
        self.assertEqual(d.outcome, E.NO_TRADE)
        self.assertIn("CHAIN_FROM_FUTURE", codes(d))

    def test_a_single_future_quote_inside_an_older_snapshot_is_refused(self):
        future = self.as_of + datetime.timedelta(seconds=1)
        # the chain model forbids this at construction, so build the offender by hand and bypass the guard
        q = self.chain.quotes[0]._replace(timestamp=future)
        fake = type("FakeChain", (object,), {})()
        for attr in ("underlying", "snapshot_time", "spot", "data_label", "source"):
            setattr(fake, attr, getattr(self.chain, attr))
        fake.quotes = (q,) + tuple(self.chain.quotes[1:])
        fake.get = self.chain.get
        fake.expiries = self.chain.expiries
        self.assertIn("CHAIN_FROM_FUTURE", codes(decide(self.regime, fake, self.events, self.as_of)))

    def test_spot_must_agree_with_the_underlying_candles(self):
        last_close = self.regime.metrics["last_close"]
        limit = DEFAULT.max_spot_divergence_pct
        edge_ok = edit_chain(self.chain, spot=last_close * (1 + limit / 100))
        self.assertNotIn("SPOT_MISMATCH", codes(decide(self.regime, edge_ok, self.events, self.as_of)))
        bad = edit_chain(self.chain, spot=last_close * (1 + limit / 100 + Dec("0.0001")))
        self.assertIn("SPOT_MISMATCH", codes(decide(self.regime, bad, self.events, self.as_of)))
        bad_low = edit_chain(self.chain, spot=last_close * (1 - limit / 100 - Dec("0.0001")))
        self.assertIn("SPOT_MISMATCH", codes(decide(self.regime, bad_low, self.events, self.as_of)))

    def test_chain_off_the_contract_grid_is_invalid(self):
        bad = with_quote(self.chain, self.chain.quotes[0].contract, bid=Dec("100.03"), ask=Dec("100.07"))
        d = decide(self.regime, bad, self.events, self.as_of)
        self.assertIn("CHAIN_INVALID", codes(d))

    def test_synthetic_data_is_refused_outside_fixture_mode(self):
        d = E.decide(self.regime, self.chain, self.events, self.as_of, sc.config(), DEFAULT, RPARAMS, sc.specs(),
                     sc.calendar("real_readonly"), "real_readonly")
        self.assertEqual(d.outcome, E.NO_TRADE)
        self.assertIn("DATA_UNVERIFIED", codes(d))
        self.assertIn("CONTRACT_DATA_UNVERIFIED", codes(d))           # the layer has no verified value for this date

    def test_high_impact_event_blocks_the_day(self):
        ev = make_event(self.as_of.replace(hour=15, minute=0), "Synthetic test event", "HIGH", "FIXTURE_SYNTHETIC")
        d = decide(self.regime, self.chain, (ev,), self.as_of)
        self.assertEqual((d.outcome, codes(d)), (E.NO_TRADE, ["HIGH_IMPACT_EVENT"]))
        allday = make_event(self.as_of.date(), "Synthetic all-day", "HIGH", "FIXTURE_SYNTHETIC")
        self.assertIn("HIGH_IMPACT_EVENT", codes(decide(self.regime, self.chain, (allday,), self.as_of)))

    def test_lower_impact_or_other_day_events_do_not_block(self):
        low = make_event(self.as_of.replace(hour=15), "low", "MEDIUM", "FIXTURE_SYNTHETIC")
        other = make_event(self.as_of.date() + datetime.timedelta(days=1), "tomorrow", "HIGH", "FIXTURE_SYNTHETIC")
        self.assertEqual(decide(self.regime, self.chain, (low, other), self.as_of).outcome, E.CANDIDATE)

    def test_event_blocking_can_be_turned_off_by_assumption(self):
        ev = make_event(self.as_of.date(), "x", "HIGH", "FIXTURE_SYNTHETIC")
        d = decide(self.regime, self.chain, (ev,), self.as_of, p=params(block_on_high_impact_event=False))
        self.assertEqual(d.outcome, E.CANDIDATE)

    def test_regime_must_belong_to_the_decision_time(self):
        d = decide(self.regime, self.chain, self.events, self.as_of + datetime.timedelta(minutes=5))
        self.assertIn("REGIME_NOT_AT_DECISION_TIME", codes(d))
        d = decide(None, self.chain, self.events, self.as_of)
        self.assertIn("REGIME_NOT_AT_DECISION_TIME", codes(d))

    def test_expiry_choice_follows_min_days_to_expiry(self):
        self.assertEqual(decide(self.regime, self.chain, self.events, self.as_of).candidate.expiry,
                         datetime.date(2026, 9, 22))
        d = decide(self.regime, self.chain, self.events, self.as_of, p=params(min_days_to_expiry=7))
        self.assertEqual(d.candidate.expiry, datetime.date(2026, 9, 29))
        d = decide(self.regime, self.chain, self.events, self.as_of, p=params(min_days_to_expiry=20))
        self.assertEqual((d.outcome, codes(d)), (E.NO_TRADE, ["NO_ELIGIBLE_EXPIRY"]))

    def test_expiry_boundary_is_inclusive(self):
        days = (datetime.date(2026, 9, 22) - self.as_of.date()).days
        self.assertEqual(decide(self.regime, self.chain, self.events, self.as_of,
                                p=params(min_days_to_expiry=days)).candidate.expiry, datetime.date(2026, 9, 22))
        self.assertEqual(decide(self.regime, self.chain, self.events, self.as_of,
                                p=params(min_days_to_expiry=days + 1)).candidate.expiry, datetime.date(2026, 9, 29))


class SelectionTests(NoNetworkTestCase):
    def setUp(self):
        super(SelectionTests, self).setUp()
        self.d = decide_at(BULL, "12:30")

    def test_the_chosen_candidate_has_the_lowest_entry_friction(self):
        passed = [c for c in self.d.considered if not c.rejections]
        self.assertGreater(len(passed), 1)
        best = min(c.friction_pct for c in passed)
        chosen = self.d.candidate
        self.assertEqual(Dec(chosen.selection["entry_friction_pct"]), best.quantize(Dec("0.01")))
        for c in passed:
            if (c.offset_steps, c.width_points) == (chosen.selection["offset_steps"], chosen.width_points):
                self.assertEqual(c.friction_pct, best)

    def test_rank_key_orders_by_friction_then_width_then_offset_then_strike(self):
        rows = [(Dec("2"), Dec("150"), 0, Dec("100")), (Dec("2"), Dec("100"), 1, Dec("100")),
                (Dec("2"), Dec("100"), 0, Dec("200")), (Dec("2"), Dec("100"), 0, Dec("100")),
                (Dec("1.5"), Dec("150"), 1, Dec("300"))]
        self.assertEqual(sorted(rows, key=E.rank_key), [
            (Dec("1.5"), Dec("150"), 1, Dec("300")), (Dec("2"), Dec("100"), 0, Dec("100")),
            (Dec("2"), Dec("100"), 0, Dec("200")), (Dec("2"), Dec("100"), 1, Dec("100")),
            (Dec("2"), Dec("150"), 0, Dec("100"))])

    def test_candidate_lots_is_an_evaluation_unit_taken_from_the_assumptions(self):
        one = self.d.candidate
        self.assertEqual((one.lots, one.spread.lots), (1, 1))
        two = decide_at(BULL, "12:30", p=params(candidate_lots=2)).candidate
        self.assertEqual((two.lots, two.spread.lots), (2, 2))
        self.assertEqual(two.net_debit_inr, two.net_debit_points * two.lot_size * 2)

    def test_depth_requirement_scales_with_candidate_lots(self):
        regime, chain, events, as_of = parts(BULL, "12:30")
        base = decide(regime, chain, events, as_of).candidate
        lq = chain.get(base.long_contract)
        need_two = 2 * base.lot_size
        thin = with_quote(chain, base.long_contract, ask_qty=need_two - 1)
        p = params(candidate_lots=2, long_strike_offsets_steps=[base.selection["offset_steps"]],
                   width_points_options=[int(base.width_points)])
        self.assertIn("THIN_DEPTH", {r.code for c in decide(regime, thin, events, as_of, p=p).considered
                                     for r in c.rejections})
        ok = with_quote(chain, base.long_contract, ask_qty=need_two)
        self.assertEqual(decide(regime, ok, events, as_of, p=p).outcome, E.CANDIDATE)
        self.assertIsNotNone(lq)

    def test_all_tested_candidates_are_recorded(self):
        self.assertEqual(len(self.d.considered), len(DEFAULT.long_strike_offsets_steps) * len(DEFAULT.width_points_options))
        self.assertEqual(self.d.candidate.selection["candidates_tested"], len(self.d.considered))

    def test_selection_ignores_risk_settings(self):
        base = json.dumps(E.decision_to_journal(self.d), sort_keys=True)
        for risk in (Dec("1"), Dec("100"), Dec("2500"), Dec("10 000".replace(" ", ""))):
            cfg = sc.config()._replace(risk_per_trade_inr=risk, max_daily_loss_inr=risk * 2, account_capital_inr=risk * 100)
            d = decide_at(BULL, "12:30", cfg=cfg)
            self.assertEqual(json.dumps(E.decision_to_journal(d), sort_keys=True), base)

    def test_a_valid_candidate_is_not_enough_a_cheaper_spread_by_a_single_offset_is_ranked_by_data_quality(self):
        only_far = decide_at(BULL, "12:30", p=params(long_strike_offsets_steps=[1], width_points_options=[150]))
        self.assertEqual(only_far.candidate.selection["offset_steps"], 1)
        self.assertEqual(int(only_far.candidate.width_points), 150)

    def test_atm_strike_is_the_nearest_grid_strike_to_spot(self):
        c = decide_at(BULL, "12:30", p=params(long_strike_offsets_steps=[0], width_points_options=[100])).candidate
        spot = Dec(c.selection["chain_spot"])
        self.assertEqual(c.long_contract.strike, (spot / 50 + Dec("0.5")).to_integral_value(rounding="ROUND_FLOOR") * 50)

    def test_offsets_move_out_of_the_money_in_the_right_direction(self):
        bull = decide_at(BULL, "12:30", p=params(long_strike_offsets_steps=[0], width_points_options=[100])).candidate
        bull1 = decide_at(BULL, "12:30", p=params(long_strike_offsets_steps=[1], width_points_options=[100])).candidate
        self.assertEqual(bull1.long_contract.strike, bull.long_contract.strike + 50)
        bear = decide_at(BEAR, "14:30", p=params(long_strike_offsets_steps=[0], width_points_options=[100])).candidate
        bear1 = decide_at(BEAR, "14:30", p=params(long_strike_offsets_steps=[1], width_points_options=[100])).candidate
        self.assertEqual(bear1.long_contract.strike, bear.long_contract.strike - 50)
        self.assertEqual(bear.short_contract.strike, bear.long_contract.strike - 100)

    def test_the_spread_is_always_fully_covered(self):
        for name, hhmm in ((BULL, "12:30"), (BEAR, "14:30")):
            c = decide_at(name, hhmm).candidate
            self.assertEqual(c.spread.long_leg.units, c.spread.short_leg.units)
            self.assertEqual(c.spread.long_leg.option_type, c.spread.short_leg.option_type)
            self.assertEqual(c.spread.long_leg.expiry, c.spread.short_leg.expiry)


class LookAheadTests(NoNetworkTestCase):
    """The decision at time T must not depend on anything stamped after T."""

    def truncated(self, name, as_of):
        s = sc.scenario(name)
        return Scenario(s.name, s.day, s.underlying, s.interval_minutes, s.data_label,
                        [c for c in s.candles if c.end <= as_of], [c for c in s.chains if c.snapshot_time <= as_of],
                        s.events, s.meta)

    def corrupted(self, name, as_of):
        """Everything after as_of replaced by wildly different data."""
        s = sc.scenario(name)
        candles = [c for c in s.candles if c.end <= as_of]
        for c in s.candles:
            if c.end > as_of:
                candles.append(type(c)(c.start, c.interval_minutes, Dec("1000"), Dec("1000"), Dec("1000"), Dec("1000")))
        chains = [c for c in s.chains if c.snapshot_time <= as_of]
        for c in s.chains:
            if c.snapshot_time > as_of:
                chains.append(edit_chain(c, lambda q: q._replace(bid=Dec("1.00"), ask=Dec("1.05"), ltp=Dec("1.00")),
                                         spot=Dec("1000")))
        return Scenario(s.name, s.day, s.underlying, s.interval_minutes, s.data_label, candles, chains, s.events, s.meta)

    def journal(self, scenario, as_of):
        d = E.decide_at(FixtureProvider(scenario), sc.calendar(), sc.specs(), sc.config(), DEFAULT, RPARAMS, as_of)
        return json.dumps(E.decision_to_journal(d), sort_keys=True)

    def test_removing_or_corrupting_the_future_changes_nothing(self):
        checked = 0
        for name in sc.NAMES:
            full = sc.scenario(name)
            for minutes in range(10 * 60 + 15, 14 * 60 + 35, 5):
                as_of = parse_ist("%s %02d:%02d" % (full.day.isoformat(), minutes // 60, minutes % 60))
                base = self.journal(full, as_of)
                self.assertEqual(self.journal(self.truncated(name, as_of), as_of), base, (name, as_of))
                self.assertEqual(self.journal(self.corrupted(name, as_of), as_of), base, (name, as_of))
                checked += 1
        self.assertGreater(checked, 250)

    def test_candidates_were_found_in_the_sweep(self):
        # guards the test above against passing vacuously on all-NO_TRADE data
        n = 0
        for name, hhmm in ((BULL, "12:30"), (BEAR, "14:30")):
            as_of = at(name, hhmm)
            full, trunc = sc.scenario(name), self.truncated(name, as_of)
            self.assertEqual(self.journal(full, as_of), self.journal(trunc, as_of))
            self.assertIn('"outcome": "CANDIDATE"', self.journal(trunc, as_of))
            n += 1
        self.assertEqual(n, 2)

    def test_a_provider_that_ignores_as_of_cannot_leak_a_later_chain(self):
        class Leaky(FixtureProvider):
            def get_option_chain(self, expiry=None, as_of=None):
                return self.scenario.chains[-1]            # always the 14:30 snapshot

        prov = Leaky(sc.scenario(BULL))
        d = E.decide_at(prov, sc.calendar(), sc.specs(), sc.config(), DEFAULT, RPARAMS, at(BULL, "12:30"))
        self.assertEqual(d.outcome, E.NO_TRADE)
        self.assertIn("CHAIN_FROM_FUTURE", codes(d))
        self.assertIsNone(d.candidate)

    def test_a_provider_that_ignores_as_of_cannot_leak_later_candles(self):
        class LeakyBars(FixtureProvider):
            def get_underlying_bars(self, interval_minutes, start, end, as_of=None):
                return tuple(self.scenario.candles)

        prov = LeakyBars(sc.scenario(BULL))
        a = E.decide_at(prov, sc.calendar(), sc.specs(), sc.config(), DEFAULT, RPARAMS, at(BULL, "12:30"))
        b = decide_at(BULL, "12:30")
        self.assertEqual(json.dumps(E.decision_to_journal(a), sort_keys=True),
                         json.dumps(E.decision_to_journal(b), sort_keys=True).replace(
                             '"ignored_future_candles": 0', '"ignored_future_candles": %d' % a.regime.ignored_future_candles))

    def test_decision_is_the_same_whatever_the_time_zone_of_as_of(self):
        ist = at(BULL, "12:30")
        for zone in (UTC, datetime.timezone(datetime.timedelta(hours=-4)), datetime.timezone(datetime.timedelta(hours=13))):
            d = E.decide_at(provider(BULL), sc.calendar(), sc.specs(), sc.config(), DEFAULT, RPARAMS, ist.astimezone(zone))
            self.assertEqual(json.dumps(E.decision_to_journal(d), sort_keys=True),
                             json.dumps(E.decision_to_journal(decide_at(BULL, "12:30")), sort_keys=True))
            self.assertEqual(d.decision_time.tzinfo, IST)

    def test_the_decision_does_not_read_the_machine_clock(self):
        clocked = FixtureProvider(sc.scenario(BULL))
        self.assertEqual(json.dumps(E.decision_to_journal(E.decide_at(
            clocked, sc.calendar(), sc.specs(), sc.config(), DEFAULT, RPARAMS, at(BULL, "12:30"))), sort_keys=True),
            json.dumps(E.decision_to_journal(decide_at(BULL, "12:30")), sort_keys=True))


if __name__ == "__main__":
    unittest.main()
