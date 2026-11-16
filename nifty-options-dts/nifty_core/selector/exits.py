"""Early exits for a position the selector opened: the price-level rule each structure was entered with.

The position manager already handles the profit target, the stop, the forced flat-by time, bad data, abnormal volatility
and an opposite regime. What it cannot know is WHY this particular structure was a good idea, and so when that reason is
gone. That is decided here, from completed 5-minute candles only:

    bull call / bear put debit spread   a candle closes back through the day's average price (the trend has failed)
    bull put credit spread              a candle closes below the support / short-put level it was placed beyond
    bear call credit spread             a candle closes above the resistance / short-call level
    iron condor / iron butterfly        a candle closes outside the day's range the structure was sold inside (the range
                                        has broken), or within one strike interval of a short strike

It returns the reason text, or None. No data means no verdict (the manager's own data-safety rule deals with that).
"""
from decimal import Decimal

from ..errors import DataNotAvailable
from ..instruments.spread import KIND_BEAR_PUT, KIND_BULL_CALL
from ..market.ist import ist_datetime, to_ist
from . import analysis as A


def _p(x):
    return "{:,.2f}".format(Decimal(x))


def _hm(c):
    return c.start.strftime("%H:%M")


def early_exit(candles, as_of, candidate, params):
    as_of = to_ist(as_of)
    cs = [c for c in candles if c.end <= as_of]
    if not cs:
        return None
    last = cs[-1]
    plan = getattr(candidate, "exit_plan", None)
    if plan:
        rule = plan["invalidate"]
        kind = rule["rule"]
        if kind == "close_below" and last.close < Decimal(rule["level"]):
            return "the %s candle closed %s, below the key level %s the credit spread was placed beyond" % (
                _hm(last), _p(last.close), _p(rule["level"]))
        if kind == "close_above" and last.close > Decimal(rule["level"]):
            return "the %s candle closed %s, above the key level %s the credit spread was placed beyond" % (
                _hm(last), _p(last.close), _p(rule["level"]))
        if kind == "outside_range":
            low, high, buf = Decimal(rule["low"]), Decimal(rule["high"]), Decimal(rule["buffer_points"])
            if last.close < low or last.close > high:
                return "the %s candle closed %s, outside the day's range %s - %s the structure was sold inside: the range has broken" % (
                    _hm(last), _p(last.close), _p(low), _p(high))
            for name in ("short_put", "short_call"):
                strike = Decimal(rule[name])
                if abs(last.close - strike) <= buf:
                    return "the %s candle closed %s, within %s points of the %s strike %s" % (
                        _hm(last), _p(last.close), _p(buf), name.replace("_", " "), _p(strike))
        return None
    bull = candidate.strategy_type == KIND_BULL_CALL
    if not bull and candidate.strategy_type != KIND_BEAR_PUT:
        return None
    a = A.analyse(cs, as_of, params)
    if a is None:
        return None
    if bull and last.close < a.day_average:
        return "the %s candle closed %s, back below the day average %s: the up-trend has failed" % (
            _hm(last), _p(last.close), _p(a.day_average))
    if not bull and last.close > a.day_average:
        return "the %s candle closed %s, back above the day average %s: the down-trend has failed" % (
            _hm(last), _p(last.close), _p(a.day_average))
    return None


def early_exit_at(provider, calendar, regime_params, params, as_of, candidate):
    """early_exit with the candles fetched through the read-only provider, standing at as_of."""
    as_of = to_ist(as_of)
    session = calendar.session(as_of.date())
    if session is None:
        return None
    try:
        bars = provider.get_underlying_bars(regime_params.interval_minutes, ist_datetime(session.date, session.open),
                                            ist_datetime(session.date, session.close), as_of=as_of)
    except DataNotAvailable:
        return None
    return early_exit(bars, as_of, candidate, params)
