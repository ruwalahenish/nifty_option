"""Version 1 strategy engine: turn a market regime and an option-chain snapshot into ONE trade
candidate, or into NO_TRADE with explicit reasons.

WHAT IT DOES NOT DO
    It does not size a position, check the risk limit, approve anything, simulate a fill or place an
    order. A candidate is a proposal for the later risk engine. Nothing here refers to the 2,500 rupee
    limit, and no threshold was chosen to make a candidate fit it.

WHAT IT USES (only what was known at the decision time `as_of`)
    * the regime at as_of (classified from candles that had finished by as_of)
    * the latest option-chain snapshot with snapshot_time <= as_of, and only quotes stamped <= as_of
    * scheduled events for the day (a schedule known in advance is not look-ahead)

DECISION ORDER
    1  session open, and as_of inside the entry window (config: entry_window_start_ist .. entry_window_end_ist)
    2  regime is BULLISH or BEARISH; every other label is NO_TRADE (SIDEWAYS, HIGH_VOLATILITY, NO_DATA, NO_TRADE)
    3  no HIGH-impact scheduled event today (when block_on_high_impact_event)
    4  chain gates: exists, not from the future, not stale, valid against the contract layer, not synthetic
       in a real mode, spot consistent with the underlying candles
    5  expiry: the nearest listed expiry at least min_days_to_expiry days away
    6  candidates: BULLISH -> Bull Call Debit Spread (buy CE at L, sell CE at L+width);
                   BEARISH -> Bear Put Debit Spread  (buy PE at L, sell PE at L-width).
       L = the strike nearest to spot, moved long_strike_offsets_steps strikes out of the money.
       Every (offset, width) pair is tested; entry prices are the ask of the long leg and the bid of the short leg.
    7  per candidate: both legs two-sided, fresh, spread <= max_leg_spread_pct, volume and open interest at least
       the minimums, quoted size on the side we would trade >= min_depth_lots lots, a structurally valid spread,
       net debit <= max_debit_to_width x width
    8  ranking among the survivors: LOWEST entry friction (both legs' quoted bid-ask spread as a % of the net
       debit), then narrower width, then nearer the money, then lower long strike. The ranking uses data quality
       only: no forecast, no profit estimate, no risk figure.
    If no candidate survives the answer is NO_TRADE. A valid spread is never chosen merely because it exists.
    The best-ranked survivor is `candidate`; the others, in rank order, are `alternates`. The strategy engine never looks
    at the risk limit: the pipeline offers them to the risk gate in order and takes the first one that fits, so a spread
    that is too big for the per-trade limit does not hide a smaller one that fits.
"""
import datetime
from collections import namedtuple
from decimal import ROUND_FLOOR, Decimal

from ..contracts import hhmm
from ..data import validation as data_validation
from ..data.models import LABEL_FIXTURE, OptionContract
from ..errors import ConfigError, ContractError, DataNotAvailable, DataValidationError, SpreadError
from ..instruments import builders
from ..intraday import setup as intraday_setup
from ..instruments.spread import KIND_BEAR_PUT, KIND_BULL_CALL
from ..market.calendar_nse import check_config_fits_session
from ..market.ist import ist_datetime, to_ist
from ..regime import engine as regime_engine
from .params import params_as_json

CANDIDATE, NO_TRADE = "CANDIDATE", "NO_TRADE"
NOTE = ("A candidate is a proposal only. It has not been sized, risk-checked or approved, and no order or simulated "
        "fill exists. Its figures describe the quoted prices at the decision time, not a forecast.")

Reason = namedtuple("Reason", "code text")
Considered = namedtuple("Considered", "offset_steps width_points long_strike short_strike rejections friction_pct")
TradeCandidate = namedtuple("TradeCandidate", (
    "strategy_type regime_at_entry decision_time chain_snapshot_time data_label data_mode expiry long_contract "
    "short_contract spread entry_price_long entry_price_short width_points net_debit_points net_debit_inr lots "
    "lot_size liquidity selection assumptions note"))
StrategyDecision = namedtuple("StrategyDecision", (
    "outcome decision_time regime reasons candidate considered thresholds_fingerprint note alternates"),
    defaults=((),))


def _s(value):
    return None if value is None else str(value)


def _leg_evidence(quote, price, side):
    pct = quote.spread_pct_of_mid
    return {"symbol": quote.contract.symbol, "side": side, "price_used": str(price), "bid": _s(quote.bid),
            "ask": _s(quote.ask), "spread_points": _s(quote.spread),
            "spread_pct_of_mid": _s(None if pct is None else pct.quantize(Decimal("0.01"))),
            "bid_qty": quote.bid_qty, "ask_qty": quote.ask_qty, "volume": quote.volume,
            "open_interest": quote.open_interest, "quote_time": quote.timestamp.isoformat()}


def candidate_to_journal(c):
    return {
        "strategy_type": c.strategy_type, "regime_at_entry": c.regime_at_entry,
        "decision_time": c.decision_time.isoformat(), "chain_snapshot_time": c.chain_snapshot_time.isoformat(),
        "data_label": c.data_label, "data_mode": c.data_mode, "expiry": c.expiry.isoformat(),
        "long_contract": c.long_contract.symbol, "short_contract": c.short_contract.symbol,
        "entry_price_long": str(c.entry_price_long), "entry_price_short": str(c.entry_price_short),
        "width_points": str(c.width_points), "net_debit_points": str(c.net_debit_points),
        "net_debit_inr": str(c.net_debit_inr), "lots": c.lots, "lot_size": c.lot_size,
        "liquidity": c.liquidity, "selection": c.selection, "assumptions": c.assumptions, "note": c.note,
    }


def decision_to_journal(d):
    return {
        "outcome": d.outcome, "decision_time": d.decision_time.isoformat(),
        "regime": d.regime.to_journal_dict() if d.regime is not None else None,
        "reasons": [{"code": r.code, "text": r.text} for r in d.reasons],
        "candidate": candidate_to_journal(d.candidate) if d.candidate is not None else None,
        "considered": [{"offset_steps": c.offset_steps, "width_points": str(c.width_points),
                        "long_strike": str(c.long_strike), "short_strike": str(c.short_strike),
                        "entry_friction_pct": _s(c.friction_pct),
                        "rejections": [{"code": r.code, "text": r.text} for r in c.rejections]}
                       for c in d.considered],
        "thresholds_fingerprint": d.thresholds_fingerprint, "note": d.note,
    }


def check_alignment(cfg, regime_params, session):
    """The entry window must fit the session and must not open before the regime engine can classify.

    Raises ConfigError.
    """
    check_config_fits_session(cfg, session)
    first = (datetime.datetime.combine(session.date, session.open)
             + datetime.timedelta(minutes=regime_params.min_bars * regime_params.interval_minutes)).time()
    if hhmm(cfg.entry_window_start_ist) < first:
        raise ConfigError("entry_window_start_ist %s is before the first possible regime classification (%s: %d "
                          "completed %d-minute candles after the open)" % (
                              cfg.entry_window_start_ist, first.strftime("%H:%M"), regime_params.min_bars,
                              regime_params.interval_minutes))


def _no_trade(as_of, regime, reasons, params, considered=()):
    return StrategyDecision(NO_TRADE, as_of, regime, tuple(reasons), None, tuple(considered), params.fingerprint, NOTE)


def _nearest_strike(spot, interval):
    return ((spot / interval) + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR) * interval


def rank_key(survivor):
    """Ranking among candidates that passed every filter: lowest friction, then narrower, then nearer the money,
    then lower long strike. survivor = (friction, width, offset, long strike, ...)."""
    return survivor[0], survivor[1], survivor[2], survivor[3]


def _legs_for(regime_label, atm, interval, offset, width):
    """(option type, long strike, short strike) for a regime and an (offset, width) pair."""
    if regime_label == regime_engine.BULLISH:
        long_k = atm + offset * interval
        return "CE", long_k, long_k + width
    long_k = atm - offset * interval
    return "PE", long_k, long_k - width


def _liquidity_rejections(label, quote, side, units, params):
    """Reasons a leg's quote is not good enough. `label` is 'long' or 'short'."""
    out = []
    sym = quote.contract.symbol

    def add(code, text):
        out.append(Reason(code, "%s leg %s: %s" % (label, sym, text)))
    if not quote.is_two_sided:
        add("ONE_SIDED_QUOTE", "needs both a bid and an ask above zero (bid=%s ask=%s)" % (quote.bid, quote.ask))
        return out
    pct = quote.spread_pct_of_mid
    if pct is None or pct > params.max_leg_spread_pct:
        add("WIDE_SPREAD", "bid-ask spread is %s%% of mid, limit %s%%" % (
            "unknown" if pct is None else pct.quantize(Decimal("0.01")), params.max_leg_spread_pct))
    for value, floor, name in ((quote.volume, params.min_leg_volume, "volume"),
                               (quote.open_interest, params.min_leg_open_interest, "open interest")):
        tag = name.upper().replace(" ", "_")
        if value is None:
            add("MISSING_" + tag, "%s is not reported" % name)
        elif value < floor:
            add("LOW_" + tag, "%s %d is below the minimum %d" % (name, value, floor))
    qty = quote.ask_qty if side == "BUY" else quote.bid_qty
    need = units * params.min_depth_lots
    if qty is None:
        add("MISSING_DEPTH", "quoted %s size is not reported" % ("ask" if side == "BUY" else "bid"))
    elif qty < need:
        add("THIN_DEPTH", "quoted %s size %d is below %d units (%d lot(s))" % (
            "ask" if side == "BUY" else "bid", qty, need, params.min_depth_lots))
    return out


def _chain_gates(regime, chain, as_of, cfg, params, specs, calendar, data_mode):
    gate = []
    if chain is None:
        return [Reason("CHAIN_UNAVAILABLE", "no option-chain snapshot at or before %s" % as_of.isoformat())]
    if chain.snapshot_time > as_of or any(q.timestamp > as_of for q in chain.quotes):
        gate.append(Reason("CHAIN_FROM_FUTURE", "the chain holds data stamped after the decision time"))
    else:
        age = (as_of - chain.snapshot_time).total_seconds()
        if age > cfg.data_stale_after_seconds:
            gate.append(Reason("CHAIN_STALE", "the latest snapshot is %d s old (limit %d s)" % (
                age, cfg.data_stale_after_seconds)))
    if data_mode != "fixture" and chain.data_label == LABEL_FIXTURE:
        gate.append(Reason("DATA_UNVERIFIED", "synthetic fixture data cannot be used when data_mode=%r" % data_mode))
    try:
        data_validation.validate_chain_against_contracts(chain, specs, calendar, data_mode)
    except DataValidationError as e:
        gate.append(Reason("CHAIN_INVALID", "%s: %s" % (e.code, e)))
    except ContractError as e:
        gate.append(Reason("CONTRACT_DATA_UNVERIFIED", "the versioned contract layer cannot vouch for this date in "
                           "data_mode=%r: %s" % (data_mode, e)))
    last_close = regime.metrics.get("last_close")
    if last_close:
        gap = abs(chain.spot / last_close - 1) * 100
        if gap > params.max_spot_divergence_pct:
            gate.append(Reason("SPOT_MISMATCH", "chain spot %s differs from the last candle close %s by %s%% "
                               "(limit %s%%)" % (chain.spot, last_close, gap.quantize(Decimal("0.001")),
                                                 params.max_spot_divergence_pct)))
    return gate


def decide(regime, chain, events, as_of, cfg, params, regime_params, specs, calendar, data_mode="fixture"):
    """Pure decision from already-fetched inputs. `regime` is a RegimeResult computed at `as_of`."""
    as_of = to_ist(as_of)
    day = as_of.date()
    session = calendar.session(day)
    if session is None:
        return _no_trade(as_of, regime, [Reason("MARKET_CLOSED", "no trading session on %s" % day)], params)
    check_alignment(cfg, regime_params, session)
    gate = []

    start, end = hhmm(cfg.entry_window_start_ist), hhmm(cfg.entry_window_end_ist)
    if as_of.time() < start:
        gate.append(Reason("OUTSIDE_ENTRY_WINDOW", "%s is before the entry window opens at %s" % (
            as_of.strftime("%H:%M"), cfg.entry_window_start_ist)))
    elif as_of.time() > end:
        gate.append(Reason("OUTSIDE_ENTRY_WINDOW", "%s is after the entry window closes at %s" % (
            as_of.strftime("%H:%M"), cfg.entry_window_end_ist)))
    if regime is None or regime.as_of != as_of:
        gate.append(Reason("REGIME_NOT_AT_DECISION_TIME", "the regime was not computed at the decision time"))
    elif regime.label not in (regime_engine.BULLISH, regime_engine.BEARISH):
        why = "; ".join("%s: %s" % (r.code, r.text) for r in regime.reasons) or "no detail"
        gate.append(Reason("REGIME_" + regime.label, "regime is %s, so Version 1 takes no trade (%s)" % (
            regime.label, why)))
    if gate:
        return _no_trade(as_of, regime, gate, params)

    if params.block_on_high_impact_event:
        for e in events or ():
            e_day = e.when.date() if isinstance(e.when, datetime.datetime) else e.when
            if e.impact == "HIGH" and e_day == day:
                gate.append(Reason("HIGH_IMPACT_EVENT", "scheduled HIGH-impact event today: %s" % e.name))
    gate.extend(_chain_gates(regime, chain, as_of, cfg, params, specs, calendar, data_mode))
    if gate:
        return _no_trade(as_of, regime, gate, params)

    eligible = [e for e in chain.expiries() if (e - day).days >= params.min_days_to_expiry]
    if not eligible:
        listed = ", ".join(str(e) for e in chain.expiries()) or "none"
        return _no_trade(as_of, regime, [Reason(
            "NO_ELIGIBLE_EXPIRY", "no listed expiry is at least %d days away (listed: %s)" % (
                params.min_days_to_expiry, listed))], params)
    expiry = eligible[0]
    interval = Decimal(str(specs.strike_interval(day, data_mode).value))
    atm = _nearest_strike(chain.spot, interval)

    considered, survivors = [], []
    for offset in params.long_strike_offsets_steps:
        for width in params.width_points_options:
            width = Decimal(width)
            opt, long_k, short_k = _legs_for(regime.label, atm, interval, offset, width)
            long_c = OptionContract("NIFTY", expiry, long_k, opt)
            short_c = OptionContract("NIFTY", expiry, short_k, opt)
            check = data_validation.check_entry_data(
                chain, [(long_c, "BUY"), (short_c, "SELL")], as_of, cfg, specs, calendar, data_mode)
            rej = [Reason(code, text) for code, text in check.problems]
            lq, sq = chain.get(long_c), chain.get(short_c)
            if lq is not None and sq is not None and expiry in check.lot_sizes:
                units = params.candidate_lots * check.lot_sizes[expiry]
                rej.extend(_liquidity_rejections("long", lq, "BUY", units, params))
                rej.extend(_liquidity_rejections("short", sq, "SELL", units, params))
            spread, friction = None, None
            if not rej:
                try:
                    spread = builders.from_chain(chain, long_c, short_c, params.candidate_lots, specs, data_mode,
                                                 trade_date=day, now=as_of, calendar=calendar)
                except SpreadError as e:
                    rej.extend(Reason("SPREAD_INVALID_" + code, text) for code, text in e.problems)
            if spread is not None and spread.net_debit_points > params.max_debit_to_width * spread.width_points:
                rej.append(Reason("DEBIT_TOO_HIGH_VS_WIDTH", "net debit %s is above %s of the width %s" % (
                    spread.net_debit_points, params.max_debit_to_width, spread.width_points)))
            if spread is not None and not rej:
                friction = (lq.spread + sq.spread) / spread.net_debit_points * 100
                survivors.append((friction, width, offset, long_k, spread, lq, sq))
            considered.append(Considered(offset, width, long_k, short_k, tuple(rej), friction))

    if not survivors:
        counts = {}
        for c in considered:
            for r in c.rejections:
                counts[r.code] = counts.get(r.code, 0) + 1
        summary = ", ".join("%s x%d" % kv for kv in sorted(counts.items())) or "none"
        return _no_trade(as_of, regime, [Reason("NO_SUITABLE_CANDIDATE", "%d candidate(s) tested, none passed (%s)" % (
            len(considered), summary))], params, considered)

    survivors.sort(key=rank_key)
    regime_info = {"label": regime.label, "as_of": regime.as_of.isoformat(), "evidence_score": _s(regime.evidence_score),
                   "bull_votes": regime.bull_votes, "bear_votes": regime.bear_votes,
                   "thresholds_fingerprint": regime.thresholds_fingerprint}

    def build(survivor):
        friction, width, offset, long_k, spread, lq, sq = survivor
        return TradeCandidate(
            strategy_type=spread.kind, regime_at_entry=regime_info, decision_time=as_of,
            chain_snapshot_time=chain.snapshot_time, data_label=chain.data_label, data_mode=data_mode, expiry=expiry,
            long_contract=spread.long_leg.contract, short_contract=spread.short_leg.contract, spread=spread,
            entry_price_long=spread.long_leg.entry_price, entry_price_short=spread.short_leg.entry_price,
            width_points=spread.width_points, net_debit_points=spread.net_debit_points,
            net_debit_inr=spread.net_debit_points * spread.units, lots=spread.lots, lot_size=spread.lot_size,
            liquidity={"long": _leg_evidence(lq, spread.long_leg.entry_price, "BUY"),
                       "short": _leg_evidence(sq, spread.short_leg.entry_price, "SELL"),
                       "chain_age_seconds": int((as_of - chain.snapshot_time).total_seconds())},
            selection={"rule": "lowest entry friction among candidates that passed every filter, then narrower width, "
                               "then nearer the money, then lower long strike",
                       "entry_friction_pct": str(friction.quantize(Decimal("0.01"))),
                       "debit_to_width": str((spread.net_debit_points / spread.width_points).quantize(Decimal("0.0001"))),
                       "offset_steps": offset, "candidates_tested": len(considered), "candidates_passed": len(survivors),
                       "atm_strike": str(atm), "chain_spot": str(chain.spot),
                       "evaluation_lots_note": "lots is the unit used to evaluate the candidate, not a position size"},
            assumptions={"strategy_thresholds": params_as_json(params),
                         "entry_window_ist": [cfg.entry_window_start_ist, cfg.entry_window_end_ist],
                         "flat_by_ist": cfg.flat_by_ist, "quote_stale_after_seconds": cfg.data_stale_after_seconds,
                         "regime_thresholds_fingerprint": regime_params.fingerprint,
                         "entry_prices": "ask of the long leg, bid of the short leg"},
            note=NOTE)

    built = [build(x) for x in survivors]
    return StrategyDecision(CANDIDATE, as_of, regime, (), built[0], tuple(considered), params.fingerprint, NOTE,
                            tuple(built[1:]))


def apply_intraday_setup(regime, provider, calendar, regime_params, setup_params, as_of):
    """Let the opening-range setup name the direction. The regime engine's verdicts that mean "do not trade" still come
    first and are never overridden: NO_DATA (data gates), HIGH_VOLATILITY (the brake) and SIDEWAYS (a narrow, going-
    nowhere day, where range breakouts are mostly noise). Any other label becomes BULLISH, BEARISH or NO_TRADE according
    to the setup, with the setup's reasons (which say what it is waiting for)."""
    if regime.label in (regime_engine.NO_DATA, regime_engine.HIGH_VOLATILITY, regime_engine.SIDEWAYS):
        return regime
    session = calendar.session(as_of.date())
    try:
        bars = provider.get_underlying_bars(regime_params.interval_minutes, ist_datetime(session.date, session.open),
                                            ist_datetime(session.date, session.close), as_of=as_of)
    except DataNotAvailable:
        bars = ()
    result = intraday_setup.evaluate(bars, as_of, setup_params)
    label = {intraday_setup.BULL: regime_engine.BULLISH, intraday_setup.BEAR: regime_engine.BEARISH}.get(
        result.direction, regime_engine.NO_TRADE)
    return regime._replace(label=label, reasons=tuple(regime_engine.Reason(r.code, r.text) for r in result.reasons))


def setup_has_failed(provider, calendar, regime_params, setup_params, as_of, entry_regime_label):
    """Reason text if the intraday setup behind a held position has failed at `as_of`, else None. No data means no
    verdict here (the position manager's own data-safety rule deals with missing data)."""
    as_of = to_ist(as_of)
    session = calendar.session(as_of.date())
    direction = {regime_engine.BULLISH: intraday_setup.BULL, regime_engine.BEARISH: intraday_setup.BEAR}.get(
        entry_regime_label)
    if session is None or direction is None:
        return None
    try:
        bars = provider.get_underlying_bars(regime_params.interval_minutes, ist_datetime(session.date, session.open),
                                            ist_datetime(session.date, session.close), as_of=as_of)
    except DataNotAvailable:
        return None
    return intraday_setup.failed_after_entry(bars, as_of, setup_params, direction)


def decide_at(provider, calendar, specs, cfg, params, regime_params, as_of, data_mode="fixture", data_label=None,
              setup_params=None):
    """Fetch everything through the read-only provider, standing at `as_of`, and decide. With `setup_params` the
    direction comes from the intraday opening-range setup instead of the regime engine's own votes."""
    as_of = to_ist(as_of)
    regime = regime_engine.classify_at(provider, calendar, as_of, regime_params, data_mode, data_label)
    if setup_params is not None:
        regime = apply_intraday_setup(regime, provider, calendar, regime_params, setup_params, as_of)
    try:
        chain = provider.get_option_chain(as_of=as_of)
    except DataNotAvailable:
        chain = None
    events = provider.get_event_calendar(as_of.date())
    return decide(regime, chain, events, as_of, cfg, params, regime_params, specs, calendar, data_mode)
