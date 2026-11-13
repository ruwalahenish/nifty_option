"""Adaptive intraday strategy selector.

WHAT IT DOES
    Reads the market (trend / range, volatility, momentum, support and resistance), tests EACH of four defined-risk
    strategies against its own list of REQUIRED conditions, scores the ones that pass, and picks the best fit. If no
    strategy passes, or the best one is not convincing enough, the answer is NO_TRADE. It never picks a strategy
    just to produce a trade.

THE FOUR STRATEGIES (all intraday, all defined-risk, one open at a time, flat by the configured time)
    DIRECTIONAL_DEBIT_SPREAD   bull call / bear put debit spread   a strong, clean trend with momentum and cheap-to-normal premium
    DIRECTIONAL_CREDIT_SPREAD  bull put / bear call credit spread  a trend that is steadier than it is explosive, rich-enough
                                                                   premium, the short strike placed BEYOND support / resistance
    IRON_CONDOR                short strangle + wings             a range-bound day, volatility low-to-normal and not expanding,
                                                                   price mid-range, short strikes outside the day's range
    IRON_BUTTERFLY             short straddle + wings             a tight, pinned, low-volatility day: price sitting on the
                                                                   day's average, tiny range, flat momentum

HOW A STRATEGY IS JUDGED
    required conditions   ALL must pass or the strategy is not eligible (the reason is shown)
    confirmations         weighted extras; confidence = 40 + 60 x (weight confirmed / total weight), at most 100.
                          This is a description of how many independent checks agree. It is NOT a probability of profit.
    a strategy also needs a concrete, quoted, liquid structure that fits the per-trade risk limit and its reward-to-risk floor

CHOICE
    Among eligible strategies with confidence >= min_confidence the highest confidence wins (ties: smaller worst case).
    Otherwise NO_TRADE. One open trade at a time, a KILL file, a closed market, a missing data feed, an abnormal-volatility
    day or a HIGH-impact event always means NO_TRADE. Before entering, `recheck` re-runs the decision: if the market has
    changed the original trade is dropped, never taken blindly.

It proposes only. Nothing here sizes beyond the evaluation lot, places or simulates an order. Not financial advice.
"""
import datetime
from collections import namedtuple
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from ..contracts import hhmm
from ..data import validation as data_validation
from ..data.models import OptionContract
from ..errors import SpreadError
from ..guards import paper_only
from ..instruments.leg import BUY, SELL
from ..instruments.spread import KIND_BEAR_PUT, KIND_BULL_CALL
from ..market.ist import to_ist
from ..regime import engine as regime_engine
from ..strategy import engine as strategy_engine
from . import analysis as A
from . import structures as S

TRADE, NO_TRADE = "TRADE", "NO_TRADE"
DEBIT, CREDIT, CONDOR, FLY = ("DIRECTIONAL_DEBIT_SPREAD", "DIRECTIONAL_CREDIT_SPREAD", "IRON_CONDOR", "IRON_BUTTERFLY")
STRATEGIES = (DEBIT, CREDIT, CONDOR, FLY)
TITLES = {DEBIT: "Directional Debit Spread", CREDIT: "Directional Credit Spread", CONDOR: "Iron Condor",
          FLY: "Iron Butterfly"}
KIND_TITLES = {KIND_BULL_CALL: "Bull Call Debit Spread", KIND_BEAR_PUT: "Bear Put Debit Spread",
               S.KIND_BULL_PUT: "Bull Put Credit Spread", S.KIND_BEAR_CALL: "Bear Call Credit Spread",
               S.KIND_IRON_CONDOR: "Iron Condor", S.KIND_IRON_FLY: "Iron Butterfly"}
BASE_CONFIDENCE, CONFIRMATION_SHARE = 40, 60
NOTE = ("A selection is a proposal only: it has not been entered, and no order or simulated fill exists. Figures describe "
        "the quoted prices at the decision time, not a forecast. Confidence counts how many independent checks agree; it is "
        "not a probability of profit. Paper trading only. Not financial advice.")

Reason = namedtuple("Reason", "code text")
Cond = namedtuple("Cond", "name passed detail required weight")
Proposal = namedtuple("Proposal", "kind direction structure legs quality friction_pct defined_risk_inr risk_parts "
                                  "max_profit_inr max_loss_inr reward_to_risk breakevens considered")
Evaluation = namedtuple("Evaluation", "strategy eligible confidence conditions proposal blockers")
Selection = namedtuple("Selection", (
    "outcome as_of analysis selected confidence confidence_label why trigger setup exit_rules evaluations reasons "
    "regime_label thresholds_fingerprint note"))


# ---------------------------------------------------------------- helpers
def _p(x):
    return "{:,.2f}".format(Decimal(x))


def _pct(x):
    return "%.2f%%" % Decimal(x)


def _floor_strike(x, interval):
    return (Decimal(x) / interval).to_integral_value(rounding=ROUND_FLOOR) * interval


def _ceil_strike(x, interval):
    return (Decimal(x) / interval).to_integral_value(rounding=ROUND_CEILING) * interval


def _atm(spot, interval):
    return ((spot / interval) + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR) * interval


def _cond(name, passed, detail, required=True, weight=0):
    return Cond(name, bool(passed), detail, required, weight)


def _confidence(conditions, proposal_ok):
    """(eligible, confidence or None)."""
    if not proposal_ok or any(c.required and not c.passed for c in conditions):
        return False, None
    extras = [c for c in conditions if not c.required]
    total = sum(c.weight for c in extras)
    got = sum(c.weight for c in extras if c.passed)
    return True, int(min(100, BASE_CONFIDENCE + CONFIRMATION_SHARE * got / total)) if total else BASE_CONFIDENCE


def confidence_label(score):
    return "HIGH" if score >= 80 else "MODERATE" if score >= 65 else "LOW"


def _timing(as_of, latest, label):
    return _cond("time window", as_of.time() <= hhmm(latest),
                 "%s: %s, no new %s entry after %s" % ("inside" if as_of.time() <= hhmm(latest) else "outside",
                                                       as_of.strftime("%H:%M"), label, latest))


class _Ctx(object):
    """Everything an evaluator needs, gathered once."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------------------------------------------------------- leg checks (shared liquidity rules)
def _check_legs(c, legs):
    """legs: [(contract, side)]. Returns (rejection list, quotes dict) using the data and liquidity rules of the strategy engine."""
    rej = []
    check = data_validation.check_entry_data(c.chain, legs, c.as_of, c.cfg, c.specs, c.calendar, c.data_mode)
    rej.extend(Reason(code, text) for code, text in check.problems)
    if rej or c.expiry not in check.lot_sizes:
        return rej
    units = c.strategy_params.candidate_lots * check.lot_sizes[c.expiry]
    for contract, side in legs:
        q = c.chain.get(contract)
        rej.extend(strategy_engine._liquidity_rejections("long" if side == BUY else "short", q, side, units,
                                                         c.strategy_params))
    return rej


def _friction(structure_legs, chain, credit_or_debit):
    spreads = [chain.get(l.contract).spread for l in structure_legs]
    return sum(spreads, Decimal(0)) / abs(credit_or_debit) * 100


def _credit_proposal(c, kind, contracts_sides, offset_steps, width, considered):
    """Try to build one credit structure; append (description, rejections) to `considered`; return a Proposal or None."""
    desc = "%s %s width %s" % (KIND_TITLES[kind], "/".join("%s%s" % (k.strike, k.option_type) for k, _ in contracts_sides), width)
    rej = []
    for contract, _side in contracts_sides:
        if c.chain.get(contract) is None:
            rej.append(Reason("MISSING_QUOTE", "%s is not in the chain" % contract.symbol))
    if not rej:
        rej = _check_legs(c, contracts_sides)
    structure = None
    if not rej:
        try:
            legs = [S.priced_leg(c.chain, k, side, c.strategy_params.candidate_lots, c.specs, c.data_mode)
                    for k, side in contracts_sides]
            structure = S.CreditStructure.create(kind, legs, c.specs, c.day, c.data_mode)
        except SpreadError as e:
            rej.extend(Reason("STRUCTURE_INVALID_" + code, text) for code, text in e.problems)
    ratio_floor = {S.KIND_BULL_PUT: c.p.min_credit_to_width_spread, S.KIND_BEAR_CALL: c.p.min_credit_to_width_spread,
                   S.KIND_IRON_CONDOR: c.p.min_credit_to_width_condor, S.KIND_IRON_FLY: c.p.min_credit_to_width_fly}[kind]
    proposal = None
    if structure is not None:
        ratio = structure.net_credit_points / structure.width_points
        if ratio < ratio_floor:
            rej.append(Reason("CREDIT_TOO_SMALL", "credit %s is %s of the width %s, floor %s" % (
                structure.net_credit_points, _pct(ratio * 100), structure.width_points, _pct(ratio_floor * 100))))
        total, parts = structure.defined_risk_inr(c.cost_engine, c.cfg.slippage_points_per_leg)
        if total > c.cfg.risk_per_trade_inr:
            rej.append(Reason("OVER_RISK_LIMIT", "worst case INR %s (loss + costs + slippage) is above the INR %s per-trade "
                              "limit" % (total, c.cfg.risk_per_trade_inr)))
        if not rej:
            proposal = Proposal(kind, None, structure, structure.legs, ratio, _friction(structure.legs, c.chain,
                                structure.net_credit_points), total, parts, structure.gross_max_profit_inr,
                                structure.gross_max_loss_inr, structure.reward_to_risk(),
                                (structure.lower_breakeven, structure.upper_breakeven), ())
    considered.append((desc, tuple(rej)))
    return proposal


def _best(proposals):
    """Highest credit-to-width, then lower friction, then smaller worst case."""
    return sorted(proposals, key=lambda x: (-x.quality, x.friction_pct, x.defined_risk_inr))[0] if proposals else None


def _summarise_rejections(considered):
    counts = {}
    for _d, rej in considered:
        for r in rej:
            counts[r.code] = counts.get(r.code, 0) + 1
    return ", ".join("%s x%d" % kv for kv in sorted(counts.items())) or "no candidate could be built"


# ---------------------------------------------------------------- strategy evaluators
def _eval_debit(c):
    a, p = c.analysis, c.p
    bull = a.structure == A.TRENDING_BULLISH
    trending = a.structure in (A.TRENDING_BULLISH, A.TRENDING_BEARISH)
    word = "up" if bull else "down"
    aligned = a.momentum == (A.MOM_UP if bull else A.MOM_DOWN)
    level = a.resistance if bull else a.support
    if level is not None and level == (a.day_high if bull else a.day_low) and aligned:
        level = None          # pressing the day's extreme with momentum behind it is a breakout in progress, not a wall
    room = None if level is None else abs(level / a.spot - 1) * 100
    conds = [
        _cond("trend", trending, "market is %s (%s)" % (a.structure.replace("_", " ").lower(), a.strength.lower())),
        _cond("momentum aligned", trending and aligned, "momentum is %s (%+.3f%% over %d candles)%s" % (
            a.momentum.lower(), a.momentum_pct, p.momentum_window,
            "; a debit spread needs it running %s" % word if trending else ", and there is no trend to follow")),
        _cond("premium not rich", a.volatility != A.VOL_HIGH, "volatility is %s; a debit spread pays for premium, so it "
              "wants LOW or NORMAL" % a.volatility.lower()),
        _cond("room to run", trending and (room is None or room >= p.room_min_pct),
              "no trend, so no direction to run in" if not trending else
              "no %s level ahead (open air)" % ("resistance" if bull else "support") if room is None else
              "nearest %s %s is %s away (needs %s)" % ("resistance" if bull else "support", _p(level), _pct(room), _pct(p.room_min_pct))),
        _timing(c.as_of, p.latest_entry_debit_ist, "debit-spread"),
        _cond("strong trend", a.strength == "STRONG", "net move %+.2f%%, efficiency %.2f" % (a.net_move_pct, a.efficiency), False, 25),
        _cond("momentum strong", aligned, "momentum %s" % a.momentum.lower(), False, 15),
        _cond("cheap premium", a.volatility == A.VOL_LOW, "volatility %s" % a.volatility.lower(), False, 10),
        _cond("open air ahead", trending and (room is None or room >= 2 * p.room_min_pct),
              "room %s" % ("unlimited" if room is None else _pct(room)), False, 15),
    ]
    proposal, considered = None, []
    if trending and all(x.passed for x in conds if x.required):
        regime = c.regime._replace(label=regime_engine.BULLISH if bull else regime_engine.BEARISH, reasons=())
        d = strategy_engine.decide(regime, c.chain, c.events, c.as_of, c.cfg, c.strategy_params, c.regime_params,
                                   c.specs, c.calendar, c.data_mode)
        if d.candidate is None:
            considered.append(("debit spread candidates", tuple(Reason(r.code, r.text) for r in d.reasons)))
        else:
            ok = []
            for cand in (d.candidate,) + tuple(d.alternates):
                sp = cand.spread
                rr = (sp.width_points - sp.net_debit_points) / sp.net_debit_points
                risk = sp.defined_risk(c.cost_engine, c.cfg.slippage_points_per_leg)
                rej = []
                if rr < p.min_rr_debit:
                    rej.append(Reason("RR_TOO_LOW", "reward-to-risk %.2f is below %s" % (rr, p.min_rr_debit)))
                if risk.total_defined_risk_inr > c.cfg.risk_per_trade_inr:
                    rej.append(Reason("OVER_RISK_LIMIT", "worst case INR %s is above the INR %s per-trade limit" % (
                        risk.total_defined_risk_inr, c.cfg.risk_per_trade_inr)))
                considered.append(("%s %s/%s" % (KIND_TITLES[sp.kind], sp.long_leg.strike, sp.short_leg.strike), tuple(rej)))
                if not rej:
                    ok.append(Proposal(sp.kind, "BULL" if bull else "BEAR", sp, (sp.long_leg, sp.short_leg), rr,
                                       Decimal(cand.selection["entry_friction_pct"]), risk.total_defined_risk_inr,
                                       {"max_loss_inr": sp.gross_max_loss_inr, "total_defined_risk_inr": risk.total_defined_risk_inr,
                                        "entry_costs_inr": risk.entry_costs.total,
                                        "estimated_exit_costs_inr": risk.estimated_exit_costs.total,
                                        "slippage_inr": risk.entry_slippage_inr},
                                       sp.gross_max_profit_inr, sp.gross_max_loss_inr, rr, (sp.expiry_breakeven,), ()))
            # best reward-to-risk first, then the engine's own ranking (lowest friction) already in list order
            proposal = sorted(ok, key=lambda x: -x.reward_to_risk)[0] if ok else None
    blockers = [x for x in conds if x.required and not x.passed]
    if proposal is None and not blockers:
        blockers = [_cond("tradable structure", False, "no debit spread passed liquidity, reward-to-risk (>= %s) and the "
                          "risk limit: %s" % (p.min_rr_debit, _summarise_rejections(considered)))]
    if proposal is not None:
        conds += [_cond("reward-to-risk comfortable", proposal.reward_to_risk >= p.min_rr_debit * Decimal("1.5"),
                        "reward-to-risk %.2f" % proposal.reward_to_risk, False, 15),
                  _cond("tight option spreads", proposal.friction_pct <= 10, "entry friction %.1f%% of the debit" % proposal.friction_pct, False, 10),
                  _cond("price on the right side of the day average", (a.spot > a.day_average) == bull,
                        "spot %s vs day average %s" % (_p(a.spot), _p(a.day_average)), False, 10)]
    eligible, conf = _confidence(conds, proposal is not None)
    return Evaluation(DEBIT, eligible, conf, tuple(conds), proposal, tuple(blockers))


def _eval_credit(c):
    a, p = c.analysis, c.p
    bull = a.structure == A.TRENDING_BULLISH
    trending = a.structure in (A.TRENDING_BULLISH, A.TRENDING_BEARISH)
    ok_moms = (A.MOM_UP, A.MOM_WEAK_UP, A.MOM_FLAT) if bull else (A.MOM_DOWN, A.MOM_WEAK_DOWN, A.MOM_FLAT)
    level = a.support if bull else a.resistance
    conds = [
        _cond("trend", trending, "market is %s (%s)" % (a.structure.replace("_", " ").lower(), a.strength.lower())),
        _cond("momentum not against the trend", trending and a.momentum in ok_moms,
              "momentum is %s (%+.3f%%)%s" % (a.momentum.lower(), a.momentum_pct, "" if trending else ", and there is no trend")),
        _cond("premium worth selling", a.volatility in (A.VOL_NORMAL, A.VOL_HIGH),
              "volatility is %s; a credit spread needs NORMAL or HIGH so the credit is worth the risk" % a.volatility.lower()),
        _cond("volatility not expanding", not a.vol_expanding, "recent stdev %.3f%% vs day %.3f%%" % (a.recent_stdev_pct, a.stdev_pct)),
        _timing(c.as_of, p.latest_entry_credit_ist, "credit"),
        _cond("strong trend", a.strength == "STRONG", "net move %+.2f%%, efficiency %.2f" % (a.net_move_pct, a.efficiency), False, 20),
        _cond("momentum with the trend", a.momentum == (A.MOM_UP if bull else A.MOM_DOWN), "momentum %s" % a.momentum.lower(), False, 15),
        _cond("defined %s level" % ("support" if bull else "resistance"), level is not None,
              "%s %s" % ("support" if bull else "resistance", _p(level) if level else "none found"), False, 10),
        _cond("volatility calm", a.recent_stdev_pct <= a.stdev_pct, "recent stdev %.3f%% <= day %.3f%%" % (a.recent_stdev_pct, a.stdev_pct), False, 10),
    ]
    proposal, considered = None, []
    if all(x.passed for x in conds if x.required):
        interval = c.interval
        d = p.credit_short_distance_pct / 100
        buf = p.level_buffer_pct / 100
        if bull:
            limit = a.spot * (1 - d)
            if a.support is not None:
                limit = min(limit, a.support * (1 - buf))
            base = _floor_strike(limit, interval)
        else:
            limit = a.spot * (1 + d)
            if a.resistance is not None:
                limit = max(limit, a.resistance * (1 + buf))
            base = _ceil_strike(limit, interval)
        found = []
        for step in (0, 1, 2):
            for width in p.credit_width_options:
                width = Decimal(width)
                if bull:
                    short_k, opt, kind = base - step * interval, "PE", S.KIND_BULL_PUT
                    long_k = short_k - width
                else:
                    short_k, opt, kind = base + step * interval, "CE", S.KIND_BEAR_CALL
                    long_k = short_k + width
                sc = OptionContract("NIFTY", c.expiry, short_k, opt)
                lc = OptionContract("NIFTY", c.expiry, long_k, opt)
                pr = _credit_proposal(c, kind, [(sc, SELL), (lc, BUY)], step, width, considered)
                if pr is not None:
                    found.append(pr._replace(direction="BULL" if bull else "BEAR"))
        proposal = _best(found)
    blockers = [x for x in conds if x.required and not x.passed]
    if proposal is None and not blockers:
        blockers = [_cond("tradable structure", False, "no credit spread passed (short strike beyond the level, liquidity, "
                          "credit >= %s of width, risk limit): %s" % (_pct(p.min_credit_to_width_spread * 100), _summarise_rejections(considered)))]
    if proposal is not None:
        sh = proposal.structure.legs[0]
        dist = abs(sh.strike / a.spot - 1) * 100
        conds += [_cond("generous distance to the short strike", dist >= p.credit_short_distance_pct * Decimal("1.5"),
                        "short strike %s is %s from spot" % (sh.strike, _pct(dist)), False, 15),
                  _cond("credit well above the floor", proposal.quality >= p.min_credit_to_width_spread * Decimal("1.5"),
                        "credit is %s of the width" % _pct(proposal.quality * 100), False, 15),
                  _cond("tight option spreads", proposal.friction_pct <= 15, "entry friction %.1f%% of the credit" % proposal.friction_pct, False, 5)]
    eligible, conf = _confidence(conds, proposal is not None)
    return Evaluation(CREDIT, eligible, conf, tuple(conds), proposal, tuple(blockers))


def _eval_condor(c):
    a, p = c.analysis, c.p
    rng = a.structure == A.RANGE_BOUND
    mid = p.range_mid_low <= a.range_position <= p.range_mid_high
    conds = [
        _cond("range-bound", rng, "market is %s (efficiency %.2f, net move %+.2f%%, day range %.2f%%)" % (
            a.structure.replace("_", " ").lower(), a.efficiency, a.net_move_pct, a.range_pct)),
        _cond("volatility low or normal", a.volatility != A.VOL_HIGH, "volatility is %s" % a.volatility.lower()),
        _cond("volatility not expanding", not a.vol_expanding, "recent stdev %.3f%% vs day %.3f%%" % (a.recent_stdev_pct, a.stdev_pct)),
        _cond("no directional momentum", a.momentum in (A.MOM_FLAT, A.MOM_WEAK_UP, A.MOM_WEAK_DOWN),
              "momentum is %s (%+.3f%%)" % (a.momentum.lower(), a.momentum_pct)),
        _cond("price mid-range", mid, "price sits at %.0f%% of the day's range (needs %.0f%%-%.0f%%)" % (
            a.range_position * 100, p.range_mid_low * 100, p.range_mid_high * 100)),
        _timing(c.as_of, p.latest_entry_credit_ist, "credit"),
        _cond("tight range", a.strength == "TIGHT", "day range %.2f%%" % a.range_pct, False, 15),
        _cond("flat momentum", a.momentum == A.MOM_FLAT, "momentum %s" % a.momentum.lower(), False, 15),
        _cond("low volatility", a.volatility == A.VOL_LOW, "volatility %s" % a.volatility.lower(), False, 15),
        _cond("very low efficiency", a.efficiency <= p.range_efficiency_max / 2, "efficiency %.2f" % a.efficiency, False, 10),
        _cond("centred", abs(a.range_position - Decimal("0.5")) <= Decimal("0.15"), "range position %.0f%%" % (a.range_position * 100), False, 10),
    ]
    proposal, considered = None, []
    if all(x.passed for x in conds if x.required):
        interval = c.interval
        d, buf = p.condor_short_distance_pct / 100, p.level_buffer_pct / 100
        call_base = _ceil_strike(max(a.spot * (1 + d), a.day_high * (1 + buf)), interval)
        put_base = _floor_strike(min(a.spot * (1 - d), a.day_low * (1 - buf)), interval)
        found = []
        for step in (0, 1):
            for width in p.condor_width_options:
                width = Decimal(width)
                sc_k, sp_k = call_base + step * interval, put_base - step * interval
                legs = [(OptionContract("NIFTY", c.expiry, sc_k, "CE"), SELL), (OptionContract("NIFTY", c.expiry, sc_k + width, "CE"), BUY),
                        (OptionContract("NIFTY", c.expiry, sp_k, "PE"), SELL), (OptionContract("NIFTY", c.expiry, sp_k - width, "PE"), BUY)]
                found.append(_credit_proposal(c, S.KIND_IRON_CONDOR, legs, step, width, considered))
        proposal = _best([x for x in found if x])
    blockers = [x for x in conds if x.required and not x.passed]
    if proposal is None and not blockers:
        blockers = [_cond("tradable structure", False, "no iron condor passed (short strikes outside the day's range, liquidity, "
                          "credit >= %s of width, risk limit): %s" % (_pct(p.min_credit_to_width_condor * 100), _summarise_rejections(considered)))]
    if proposal is not None:
        conds += [_cond("credit well above the floor", proposal.quality >= p.min_credit_to_width_condor * Decimal("1.5"),
                        "credit is %s of the width" % _pct(proposal.quality * 100), False, 10),
                  _cond("tight option spreads", proposal.friction_pct <= 15, "entry friction %.1f%% of the credit" % proposal.friction_pct, False, 10)]
    eligible, conf = _confidence(conds, proposal is not None)
    return Evaluation(CONDOR, eligible, conf, tuple(conds), proposal, tuple(blockers))


def _eval_fly(c):
    a, p = c.analysis, c.p
    pin = abs(a.spot / a.day_average - 1) * 100
    tight = a.structure == A.RANGE_BOUND and a.range_pct <= p.fly_day_range_max_pct
    conds = [
        _cond("range-bound and tight", tight, "market is %s, day range %.2f%% (needs <= %s)" % (
            a.structure.replace("_", " ").lower(), a.range_pct, _pct(p.fly_day_range_max_pct))),
        _cond("pinned market", a.efficiency <= p.fly_efficiency_max, "efficiency %.2f (needs <= %s)" % (a.efficiency, p.fly_efficiency_max)),
        _cond("low volatility", a.volatility == A.VOL_LOW, "volatility is %s" % a.volatility.lower()),
        _cond("volatility not expanding", not a.vol_expanding, "recent stdev %.3f%% vs day %.3f%%" % (a.recent_stdev_pct, a.stdev_pct)),
        _cond("flat momentum", a.momentum == A.MOM_FLAT, "momentum is %s (%+.3f%%)" % (a.momentum.lower(), a.momentum_pct)),
        _cond("price on the day average", pin <= p.fly_pin_band_pct, "spot %s is %s from the day average %s (needs <= %s)" % (
            _p(a.spot), _pct(pin), _p(a.day_average), _pct(p.fly_pin_band_pct))),
        _timing(c.as_of, p.latest_entry_credit_ist, "credit"),
        _cond("very tight range", a.range_pct <= p.fly_day_range_max_pct / 2, "day range %.2f%%" % a.range_pct, False, 20),
        _cond("pin is exact", pin <= p.fly_pin_band_pct / 2, "distance to day average %s" % _pct(pin), False, 20),
        _cond("centred in the range", abs(a.range_position - Decimal("0.5")) <= Decimal("0.15"), "range position %.0f%%" % (a.range_position * 100), False, 15),
    ]
    proposal, considered = None, []
    if all(x.passed for x in conds if x.required):
        interval = c.interval
        k = _atm(a.spot, interval)
        found = []
        for width in p.fly_width_options:
            width = Decimal(width)
            legs = [(OptionContract("NIFTY", c.expiry, k, "CE"), SELL), (OptionContract("NIFTY", c.expiry, k + width, "CE"), BUY),
                    (OptionContract("NIFTY", c.expiry, k, "PE"), SELL), (OptionContract("NIFTY", c.expiry, k - width, "PE"), BUY)]
            found.append(_credit_proposal(c, S.KIND_IRON_FLY, legs, 0, width, considered))
        proposal = _best([x for x in found if x])
    blockers = [x for x in conds if x.required and not x.passed]
    if proposal is None and not blockers:
        blockers = [_cond("tradable structure", False, "no iron butterfly passed (liquidity, credit >= %s of width, risk limit): %s" % (
            _pct(p.min_credit_to_width_fly * 100), _summarise_rejections(considered)))]
    if proposal is not None:
        conds += [_cond("credit well above the floor", proposal.quality >= p.min_credit_to_width_fly * Decimal("1.25"),
                        "credit is %s of the width" % _pct(proposal.quality * 100), False, 15),
                  _cond("tight option spreads", proposal.friction_pct <= 15, "entry friction %.1f%% of the credit" % proposal.friction_pct, False, 10)]
    eligible, conf = _confidence(conds, proposal is not None)
    return Evaluation(FLY, eligible, conf, tuple(conds), proposal, tuple(blockers))


# ---------------------------------------------------------------- narrative: setup, trigger, exits, why
def _setup(prop, a, c):
    s = prop.structure
    legs = []
    for l in prop.legs:
        q = c.chain.get(l.contract)
        legs.append({"action": "BUY" if l.is_long else "SELL", "contract": l.contract.symbol, "type": l.option_type,
                     "strike": str(l.strike), "price": str(l.entry_price), "bid": str(q.bid), "ask": str(q.ask)})
    credit = prop.kind in (S.KIND_BULL_PUT, S.KIND_BEAR_CALL, S.KIND_IRON_CONDOR, S.KIND_IRON_FLY)
    out = {"strategy": KIND_TITLES[prop.kind], "kind": prop.kind, "direction": prop.direction, "expiry": s.expiry.isoformat(),
           "lots": s.lots, "lot_size": s.lot_size, "units": s.units, "legs": legs,
           "net_credit_points" if credit else "net_debit_points": str(s.net_credit_points if credit else s.net_debit_points),
           "width_points": str(s.width_points), "max_profit_inr": str(prop.max_profit_inr.quantize(Decimal("0.01"))),
           "max_loss_inr": str(prop.max_loss_inr.quantize(Decimal("0.01"))),
           "worst_case_with_costs_inr": str(Decimal(prop.defined_risk_inr).quantize(Decimal("0.01"))),
           "risk_limit_inr": str(c.cfg.risk_per_trade_inr), "reward_to_risk": str(Decimal(prop.reward_to_risk).quantize(Decimal("0.01"))),
           "breakevens_at_expiry": [str(b) for b in prop.breakevens if b is not None],
           "entry_friction_pct": str(Decimal(prop.friction_pct).quantize(Decimal("0.1"))), "decision_spot": str(a.spot),
           "figures_note": "max profit / max loss are EXPIRY-payoff worst and best cases, gross of costs; the trade is closed "
                           "the same day, so actual P&L comes from the exit prices"}
    return out


def _exit_rules(prop, a, c):
    p, s = c.p, prop.structure
    credit = prop.kind in (S.KIND_BULL_PUT, S.KIND_BEAR_CALL, S.KIND_IRON_CONDOR, S.KIND_IRON_FLY)
    flat = c.cfg.flat_by_ist
    if credit:
        cr = s.net_credit_points
        tgt_cost = cr * (1 - p.credit_target_pct_of_credit / 100)
        loss_pts = min(cr * p.credit_stop_pct_of_credit / 100, (s.width_points - cr) * p.credit_stop_max_pct_of_max_loss / 100)
        stop_cost = cr + loss_pts
        target = ("Buy the structure back for %s points or less (captures %s%% of the %s-point credit = about INR %s)" % (
            tgt_cost.quantize(Decimal("0.05")), p.credit_target_pct_of_credit, cr, (cr * p.credit_target_pct_of_credit / 100 * s.units).quantize(Decimal("1"))))
        stop = ("Buy the structure back if it costs %s points or more (a loss of about INR %s: %s%% of the credit, but never more than "
                "%s%% of the INR %s worst case)" % (stop_cost.quantize(Decimal("0.05")), (loss_pts * s.units).quantize(Decimal("1")),
                                                    p.credit_stop_pct_of_credit, p.credit_stop_max_pct_of_max_loss,
                                                    prop.max_loss_inr.quantize(Decimal("1"))))
    else:
        db = s.net_debit_points
        target = "Sell the spread when it is worth %s points or more (+%s%% of the debit, about INR %s)" % (
            (db * (1 + p.debit_target_pct_of_risk / 100)).quantize(Decimal("0.05")), p.debit_target_pct_of_risk,
            (db * p.debit_target_pct_of_risk / 100 * s.units).quantize(Decimal("1")))
        stop = "Sell the spread when it is worth %s points or less (-%s%% of the debit, about INR %s)" % (
            (db * (1 - p.debit_stop_pct_of_risk / 100)).quantize(Decimal("0.05")), p.debit_stop_pct_of_risk,
            (db * p.debit_stop_pct_of_risk / 100 * s.units).quantize(Decimal("1")))
    early = ["a 5-minute close through the structure's key level (see below)", "volatility turns abnormal or the regime engine reports "
             "HIGH_VOLATILITY / NO_DATA", "three consecutive bad or stale data checks", "a scheduled HIGH-impact event is announced",
             "a KILL file appears (manage and close; no new entries)"]
    if prop.kind == S.KIND_BULL_PUT:
        early[0] = "a 5-minute candle closes below the short put strike %s or below support %s" % (prop.legs[0].strike, _p(a.support) if a.support else "n/a")
    elif prop.kind == S.KIND_BEAR_CALL:
        early[0] = "a 5-minute candle closes above the short call strike %s or above resistance %s" % (prop.legs[0].strike, _p(a.resistance) if a.resistance else "n/a")
    elif prop.kind in (S.KIND_IRON_CONDOR, S.KIND_IRON_FLY):
        sc_ = next(l for l in prop.legs if not l.is_long and l.option_type == "CE")
        sp_ = next(l for l in prop.legs if not l.is_long and l.option_type == "PE")
        early[0] = ("a 5-minute candle closes outside the day's range %s - %s (the range has broken), or price comes within %s points of "
                    "a short strike (%s / %s)" % (_p(a.day_low), _p(a.day_high), c.interval, sp_.strike, sc_.strike))
    elif prop.kind == KIND_BULL_CALL:
        early[0] = "a 5-minute candle closes back below the day average %s (the trend has failed)" % _p(a.day_average)
    elif prop.kind == KIND_BEAR_PUT:
        early[0] = "a 5-minute candle closes back above the day average %s (the trend has failed)" % _p(a.day_average)
    return {"target": target, "stop_loss": stop,
            "time_exit": "Flat by %s IST at the latest. No position is ever carried overnight." % flat,
            "early_exit": early}


def _trigger(prop, ev, a, c):
    p, s = c.p, prop.structure
    credit = hasattr(s, "net_credit_points")
    move = p.reeval_spot_move_pct
    t = ["Spot is still within %s of the decision spot %s (between %s and %s); a larger move means RE-EVALUATE, not enter." % (
        _pct(move), _p(a.spot), _p(a.spot * (1 - move / 100)), _p(a.spot * (1 + move / 100)))]
    if prop.kind == KIND_BULL_CALL:
        t.append("Entry candle: a 5-minute candle closes above the previous candle's high while spot stays above the day average %s." % _p(a.day_average))
    elif prop.kind == KIND_BEAR_PUT:
        t.append("Entry candle: a 5-minute candle closes below the previous candle's low while spot stays below the day average %s." % _p(a.day_average))
    elif prop.kind == S.KIND_BULL_PUT:
        t.append("No 5-minute candle closes below support %s or the short put strike %s before entry." % (
            _p(a.support) if a.support else "n/a", prop.legs[0].strike))
    elif prop.kind == S.KIND_BEAR_CALL:
        t.append("No 5-minute candle closes above resistance %s or the short call strike %s before entry." % (
            _p(a.resistance) if a.resistance else "n/a", prop.legs[0].strike))
    elif prop.kind == S.KIND_IRON_CONDOR:
        t.append("Spot keeps closing inside the day's range %s - %s; no close outside it before entry." % (_p(a.day_low), _p(a.day_high)))
    else:
        t.append("Spot stays within %s of the day average %s (the pin) and the day range stays under %s." % (
            _pct(p.fly_pin_band_pct), _p(a.day_average), _pct(p.fly_day_range_max_pct)))
    t.append("The market still classifies as %s with volatility %s and momentum %s (and not expanding)." % (
        a.structure.replace("_", " ").lower(), "not HIGH" if a.volatility != A.VOL_LOW else "LOW", a.momentum.lower()))
    if credit:
        floor = {S.KIND_IRON_FLY: p.min_credit_to_width_fly, S.KIND_IRON_CONDOR: p.min_credit_to_width_condor}.get(
            prop.kind, p.min_credit_to_width_spread)
        t.append("Re-quoted net credit is at least %s points (%s of the width; shorts are quoted at the bid, longs at the ask)." % (
            (s.width_points * floor).quantize(Decimal("0.05")), _pct(floor * 100)))
    else:
        t.append("Re-quoted net debit is at most %s points (reward-to-risk stays at %s or better)." % (
            (s.width_points / (1 + p.min_rr_debit)).quantize(Decimal("0.05")), p.min_rr_debit))
    t.append("The option chain is fresh (at most %d s old), every leg is two-sided, and the worst case stays within INR %s." % (
        c.cfg.data_stale_after_seconds, c.cfg.risk_per_trade_inr))
    latest = p.latest_entry_debit_ist if not credit else p.latest_entry_credit_ist
    t.append("Entry happens no later than %s IST, and only if no other trade is open." % latest)
    return t


def _why(sel_ev, others, a):
    ev = sel_ev
    passed_req = [x for x in ev.conditions if x.required]
    lines = ["Market: " + a.summary + "."]
    lines += ["%s: %s" % (x.name, x.detail) for x in passed_req]
    good = [x.name for x in ev.conditions if not x.required and x.passed]
    if good:
        lines.append("Extra confirmations: " + ", ".join(good) + ".")
    for o in others:
        lines.append("Not %s: %s" % (TITLES[o.strategy], _first_blocker(o)))
    return lines


def _first_blocker(ev):
    if ev.blockers:
        b = ev.blockers[0]
        return "%s (%s)" % (b.name, b.detail)
    if ev.eligible and ev.confidence is not None:
        return "eligible but ranked lower (confidence %d)" % ev.confidence
    return "no qualifying structure"


# ---------------------------------------------------------------- the decision
def _no_trade(as_of, analysis, reasons, regime, p, evaluations=()):
    return Selection(NO_TRADE, as_of, analysis, None, None, None, [], [], None, None, tuple(evaluations), tuple(reasons),
                     regime.label if regime is not None else None, p.fingerprint, NOTE)


def select(regime, candles, chain, events, as_of, ctx, p, position_open=False, kill_dir=None):
    """Pure decision from already-fetched inputs. `regime` is the regime engine's result at as_of (used for its data gates
    and abnormal-volatility brake; the market description itself comes from `candles`)."""
    as_of = to_ist(as_of)
    day = as_of.date()
    cfg, calendar = ctx.cfg, ctx.calendar
    analysis = A.analyse(candles, as_of, p) if candles else None
    gate = []
    if position_open:
        gate.append(Reason("ONE_TRADE_AT_A_TIME", "a trade is already open; only one strategy may be active at a time"))
    if kill_dir is not None and paper_only.kill_switch_active(kill_dir):
        gate.append(Reason("KILL_SWITCH", "a KILL file is present: no new entries"))
    session = calendar.session(day)
    if session is None:
        return _no_trade(as_of, analysis, [Reason("MARKET_CLOSED", "no trading session on %s" % day)], regime, p)
    strategy_engine.check_alignment(cfg, ctx.regime_params, session)
    start, end = hhmm(cfg.entry_window_start_ist), hhmm(cfg.entry_window_end_ist)
    if not start <= as_of.time() <= end:
        gate.append(Reason("OUTSIDE_ENTRY_WINDOW", "%s is outside the entry window %s-%s" % (
            as_of.strftime("%H:%M"), cfg.entry_window_start_ist, cfg.entry_window_end_ist)))
    if regime is None or regime.as_of != as_of:
        gate.append(Reason("REGIME_NOT_AT_DECISION_TIME", "the regime was not computed at the decision time"))
    elif regime.label == regime_engine.NO_DATA:
        gate.append(Reason("DATA_PROBLEM", "market data is missing, late or unverified: " + "; ".join(
            "%s: %s" % (r.code, r.text) for r in regime.reasons)))
    elif regime.label == regime_engine.HIGH_VOLATILITY:
        gate.append(Reason("ABNORMAL_VOLATILITY", "abnormal movement today, so no strategy is safe: " + "; ".join(
            r.text for r in regime.reasons)))
    if analysis is None and not any(g.code in ("REGIME_NOT_AT_DECISION_TIME", "DATA_PROBLEM") for g in gate):
        gate.append(Reason("TOO_EARLY", "fewer than %d completed candles: not enough to read the market" % p.min_candles))
    if ctx.strategy_params.block_on_high_impact_event:
        for e in events or ():
            e_day = e.when.date() if isinstance(e.when, datetime.datetime) else e.when
            if e.impact == "HIGH" and e_day == day:
                gate.append(Reason("HIGH_IMPACT_EVENT", "scheduled HIGH-impact event today: %s" % e.name))
    if gate:
        return _no_trade(as_of, analysis, gate, regime, p)
    chain_gate = strategy_engine._chain_gates(regime, chain, as_of, cfg, ctx.strategy_params, ctx.specs, calendar, ctx.data_mode)
    eligible_exp = [e for e in chain.expiries()] if chain is not None else []
    eligible_exp = [e for e in eligible_exp if (e - day).days >= ctx.strategy_params.min_days_to_expiry]
    if chain is not None and not chain_gate and not eligible_exp:
        chain_gate.append(Reason("NO_ELIGIBLE_EXPIRY", "no listed expiry is at least %d days away" % ctx.strategy_params.min_days_to_expiry))
    if chain_gate:
        return _no_trade(as_of, analysis, chain_gate, regime, p)

    c = _Ctx(analysis=analysis, p=p, regime=regime, chain=chain, events=events, as_of=as_of, day=day, cfg=cfg,
             specs=ctx.specs, calendar=calendar, data_mode=ctx.data_mode, strategy_params=ctx.strategy_params,
             regime_params=ctx.regime_params, cost_engine=ctx.cost_engine, expiry=eligible_exp[0],
             interval=Decimal(str(ctx.specs.strike_interval(day, ctx.data_mode).value)))
    evals = (_eval_debit(c), _eval_credit(c), _eval_condor(c), _eval_fly(c))
    ranked = sorted([e for e in evals if e.eligible and e.confidence >= p.min_confidence],
                    key=lambda e: (-e.confidence, e.proposal.defined_risk_inr))
    if not ranked:
        reasons = [Reason("NO_STRATEGY_QUALIFIES", "market: %s" % analysis.summary)]
        for e in evals:
            if e.eligible:
                reasons.append(Reason(e.strategy, "%s: all required conditions pass but confidence %d is below the minimum %d" % (
                    TITLES[e.strategy], e.confidence, p.min_confidence)))
            else:
                reasons.append(Reason(e.strategy, "%s: %s" % (TITLES[e.strategy], _first_blocker(e))))
        return _no_trade(as_of, analysis, reasons, regime, p, evals)
    best = ranked[0]
    others = [e for e in evals if e is not best]
    prop = best.proposal
    return Selection(TRADE, as_of, analysis, best.strategy, best.confidence, confidence_label(best.confidence),
                     _why(best, others, analysis), _trigger(prop, best, analysis, c), _setup(prop, analysis, c),
                     _exit_rules(prop, analysis, c), evals, (), regime.label, p.fingerprint, NOTE)


def select_at(provider, ctx, p, as_of, position_open=False, kill_dir=None, data_label=None):
    """Fetch everything through the read-only provider, standing at as_of, and decide."""
    from ..errors import DataNotAvailable
    as_of = to_ist(as_of)
    regime = regime_engine.classify_at(provider, ctx.calendar, as_of, ctx.regime_params, ctx.data_mode, data_label)
    candles = A.completed_candles(provider, ctx.calendar, ctx.regime_params, as_of)
    try:
        chain = provider.get_option_chain(as_of=as_of)
    except DataNotAvailable:
        chain = None
    events = provider.get_event_calendar(as_of.date())
    return select(regime, candles, chain, events, as_of, ctx, p, position_open, kill_dir)


def recheck(previous, current, p):
    """Entry-time re-evaluation. `previous` is the TRADE selection being acted on, `current` a fresh selection at the entry
    time. Returns (status, text): CONFIRMED only if the same strategy and structure are still the best fit and spot has not
    moved more than reeval_spot_move_pct; otherwise RE_EVALUATED (the original trade is dropped; use `current`) or CANCELLED."""
    if previous.outcome != TRADE:
        return "NOT_APPLICABLE", "the earlier decision was NO_TRADE, so there is nothing to confirm"
    if current.outcome != TRADE:
        return "CANCELLED", "market conditions changed: the decision is now NO_TRADE (%s)" % "; ".join(r.text for r in current.reasons[:2])
    moved = abs(current.analysis.spot / previous.analysis.spot - 1) * 100
    if current.selected != previous.selected or current.setup["kind"] != previous.setup["kind"]:
        return "RE_EVALUATED", "the best fit changed from %s to %s; the original trade is dropped" % (
            previous.setup["strategy"], current.setup["strategy"])
    if moved > p.reeval_spot_move_pct:
        return "RE_EVALUATED", "spot moved %s since the decision (limit %s): the original strikes are dropped, use the new setup" % (
            _pct(moved), _pct(p.reeval_spot_move_pct))
    return "CONFIRMED", "same strategy still the best fit and spot moved only %s" % _pct(moved)
