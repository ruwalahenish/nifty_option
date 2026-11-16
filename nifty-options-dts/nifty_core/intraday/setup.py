"""The intraday entry setup: OPENING-RANGE BREAKOUT, ENTERED ON A RETEST. Decides a direction from completed candles.

Bullish (the bearish setup is the mirror image, using the opening-range low):
    1. OPENING RANGE   the first `opening_range_candles` candles (30 minutes with 6 five-minute candles) are complete and
                       their range is at least `min_range_pct` wide (a tiny range makes breakouts noise).
    2. BREAKOUT        after the opening range, a candle CLOSED above the range high by `breakout_buffer_pct`.
    3. INTACT          every close since the breakout stays beyond the range edge (a close back inside cancels it, and a
                       later breakout counts as a new one), and the breakout is no older than `max_breakout_age_candles`.
    4. NOT CHASING     the last close is at most `max_extension_pct` above the range high. If it ran further, wait.
    5. RETEST          (require_retest) after the breakout candle, price came back to within `retest_zone_pct` of the
                       range high (a pullback to the broken level).
    6. RESUMPTION      (require_resumption_candle) the last candle closed up (bull) / down (bear).
    7. SIDE OF DAY     (require_day_average_side) the last close is above the day's average price (bull) / below (bear).
                       The index has no traded volume, so this is the plain average of each candle's (high+low+close)/3,
                       not a volume-weighted VWAP.
    8. TIME            no new setup after `latest_setup_ist`.

It only names a direction. Everything after that (regime data gates, volatility brake, option liquidity, the risk limit,
your "yes") is unchanged. It makes no forecast and no claim that the setup wins.
"""
from collections import namedtuple
from decimal import Decimal

from ..contracts import hhmm
from ..market.ist import slot_floor, to_ist

BULL, BEAR = "BULL", "BEAR"
Reason = namedtuple("Reason", "code text")
SetupResult = namedtuple("SetupResult", "direction reasons metrics")


def _p(x):
    return "{:,.2f}".format(Decimal(x))


def _pct(x):
    return "%.3f%%" % Decimal(x)


def _hm(c):
    return c.start.strftime("%H:%M")


def _check_side(cs, n, side, p, or_high, or_low, day_avg):
    """(ok, [failure reasons], success text, checks) for one side. `checks` lists every rule that was looked at with its
    value, its limit and whether it passed, so a missed breakout can be explained rule by rule (and near-misses seen)."""
    bull = side == BULL
    edge = or_high if bull else or_low
    word, beyond = ("above", "high") if bull else ("below", "low")
    buf = Decimal(p.breakout_buffer_pct) / 100
    level = edge * (1 + buf) if bull else edge * (1 - buf)
    last = cs[-1]
    fails, checks = [], []

    def fail(code, text):
        fails.append(Reason(code, text))

    def check(rule, ok, value, limit):
        checks.append({"rule": rule, "ok": bool(ok), "value": str(value), "limit": str(limit)})

    # the CURRENT breakout: the unbroken run of closes beyond the range edge that ends at the last candle
    start = len(cs)
    while start - 1 >= n and (cs[start - 1].close > edge if bull else cs[start - 1].close < edge):
        start -= 1
    if start == len(cs):
        earlier = next((c for c in cs[n:] if (c.close > level if bull else c.close < level)), None)
        check("breakout_close", False, _p(last.close), "%s %s" % (">" if bull else "<", _p(level)))
        if earlier is not None:
            fail("SETUP_FAILED_BREAKOUT", "price is back inside the opening range (the %s breakout failed): waiting for "
                 "a new close %s %s" % (_hm(earlier), word, _p(level)))
        else:
            fail("SETUP_NO_BREAKOUT", "waiting: no 5-minute close %s the opening-range %s %s yet (needs %s %s)" % (
                word, beyond, _p(edge), word, _p(level)))
        return False, fails, None, checks
    breakout = next((i for i in range(start, len(cs)) if (cs[i].close > level if bull else cs[i].close < level)), None)
    if breakout is None:
        check("breakout_close", False, _p(last.close), "%s %s" % (">" if bull else "<", _p(level)))
        fail("SETUP_NO_BREAKOUT", "waiting: price is %s the range edge %s but has not closed %s the buffer level %s" % (
            word, _p(edge), word, _p(level)))
        return False, fails, None, checks
    check("breakout_close", True, "%s closed %s" % (_hm(cs[breakout]), _p(cs[breakout].close)),
          "%s %s" % (">" if bull else "<", _p(level)))
    age = len(cs) - 1 - breakout
    check("breakout_age_candles", age <= p.max_breakout_age_candles, age, "<= %d" % p.max_breakout_age_candles)
    if age > p.max_breakout_age_candles:
        fail("SETUP_BREAKOUT_STALE", "the breakout (%s) is %d candles old (limit %d)" % (
            _hm(cs[breakout]), age, p.max_breakout_age_candles))
    ext = (last.close / edge - 1) * 100 if bull else (1 - last.close / edge) * 100
    check("not_extended", ext <= Decimal(p.max_extension_pct), _pct(ext), "<= %s" % _pct(p.max_extension_pct))
    if ext > Decimal(p.max_extension_pct):
        fail("SETUP_EXTENDED", "price is %s beyond the breakout level (limit %s): not chasing, waiting for a pullback" % (
            _pct(ext), _pct(p.max_extension_pct)))
    zone_pct = Decimal(p.retest_zone_pct) / 100
    zone = edge * (1 + zone_pct) if bull else edge * (1 - zone_pct)
    retested = None
    if p.require_retest:
        retested = next((c for c in cs[breakout + 1:] if (c.low <= zone if bull else c.high >= zone)), None)
        check("retest", retested is not None, "none yet" if retested is None else "%s %s %s" % (
            _hm(retested), "low" if bull else "high", _p(retested.low if bull else retested.high)),
              "%s %s %s" % ("low <=" if bull else "high >=", _p(zone), "after %s" % _hm(cs[breakout])))
        if retested is None:
            fail("SETUP_NO_RETEST", "waiting: no pullback to the broken level %s yet (needs a %s at or %s %s after %s)" % (
                _p(edge), "low" if bull else "high", "below" if bull else "above", _p(zone), _hm(cs[breakout])))
    if p.require_resumption_candle:
        check("resumption_candle", last.close > last.open if bull else last.close < last.open,
              "%s open %s close %s" % (_hm(last), _p(last.open), _p(last.close)), "closes %s" % ("up" if bull else "down"))
    if p.require_resumption_candle and not (last.close > last.open if bull else last.close < last.open):
        fail("SETUP_NO_RESUMPTION", "waiting: the last candle (%s) closed %s, not %s" % (
            _hm(last), "down" if bull else "up", "up" if bull else "down"))
    if p.require_day_average_side:
        check("day_average_side", last.close > day_avg if bull else last.close < day_avg, _p(last.close),
              "%s %s" % (">" if bull else "<", _p(day_avg)))
    if p.require_day_average_side and not (last.close > day_avg if bull else last.close < day_avg):
        fail("SETUP_WRONG_SIDE_OF_DAY_AVERAGE", "last close %s is on the wrong side of the day average %s" % (
            _p(last.close), _p(day_avg)))
    if fails:
        return False, fails, None, checks
    text = "%s setup: closed %s the opening range %s at %s, %s; last close %s is %s beyond the level, %s the day average %s" % (
        "BULL" if bull else "BEAR", word, _p(edge), _hm(cs[breakout]),
        "pulled back to it at %s and resumed" % _hm(retested) if retested is not None else "resumed",
        _p(last.close), _pct(ext), word, _p(day_avg))
    return True, [], text, checks


def evaluate(candles, as_of, p):
    """Direction from the candles completed at as_of. `candles` are Candle objects of one session, oldest first."""
    as_of = to_ist(as_of)
    cs = [c for c in candles if c.end <= as_of]
    n = p.opening_range_candles
    if len(cs) < n:
        return SetupResult(None, (Reason("SETUP_OPENING_RANGE_FORMING", "waiting: the opening range needs %d completed "
                                          "candles and %d exist" % (n, len(cs))),), {})
    if slot_floor(as_of).time() > hhmm(p.latest_setup_ist):         # a slot rule: 13:30:07 is still the 13:30 slot
        return SetupResult(None, (Reason("SETUP_TOO_LATE", "no new setup after %s" % p.latest_setup_ist),), {})
    opening = cs[:n]
    or_high, or_low = max(c.high for c in opening), min(c.low for c in opening)
    or_pct = (or_high - or_low) / opening[0].open * 100
    day_avg = sum(((c.high + c.low + c.close) / 3 for c in cs), Decimal(0)) / len(cs)
    metrics = {"opening_range_high": or_high, "opening_range_low": or_low, "opening_range_pct": or_pct,
               "day_average": day_avg, "last_close": cs[-1].close}
    if or_pct < Decimal(p.min_range_pct):
        return SetupResult(None, (Reason("SETUP_RANGE_TOO_NARROW", "the opening range %s-%s is %s wide (needs %s): "
                                          "breakouts of a range this tight are noise" % (
                                              _p(or_low), _p(or_high), _pct(or_pct), _pct(p.min_range_pct))),), metrics)
    mid = (or_high + or_low) / 2
    first = BULL if cs[-1].close >= mid else BEAR          # explain the side price is nearer; either side can qualify
    shown = shown_rules = None
    for side in (first, BEAR if first == BULL else BULL):
        ok, fails, text, checks = _check_side(cs, n, side, p, or_high, or_low, day_avg)
        if ok:
            return SetupResult(side, (Reason("INTRADAY_SETUP", text),),
                               dict(metrics, rules={"side": side, "checks": checks}))
        if shown is None:
            shown, shown_rules = fails, {"side": side, "checks": checks}
    return SetupResult(None, tuple(shown), dict(metrics, rules=shown_rules))


def failed_after_entry(candles, as_of, p, direction):
    """Reason text when the setup behind an OPEN position has failed, else None: the last completed candle closed back
    inside the opening range (at or below the range high for a bull position, at or above the range low for a bear one)."""
    as_of = to_ist(as_of)
    cs = [c for c in candles if c.end <= as_of]
    n = p.opening_range_candles
    if len(cs) <= n:
        return None
    opening = cs[:n]
    or_high, or_low = max(c.high for c in opening), min(c.low for c in opening)
    last = cs[-1]
    if direction == BULL and last.close <= or_high:
        return "the %s candle closed %s, back inside the opening range (high %s): the breakout has failed" % (
            _hm(last), _p(last.close), _p(or_high))
    if direction == BEAR and last.close >= or_low:
        return "the %s candle closed %s, back inside the opening range (low %s): the breakout has failed" % (
            _hm(last), _p(last.close), _p(or_low))
    return None


def levels(candles, as_of, p):
    """The price levels the setup is working with, for display: opening-range high and low, the breakout trigger levels
    (range edge plus/minus the buffer) and the day's average price. None until the opening range is complete."""
    as_of = to_ist(as_of)
    cs = [c for c in candles if c.end <= as_of]
    n = p.opening_range_candles
    if len(cs) < n:
        return None
    opening = cs[:n]
    or_high, or_low = max(c.high for c in opening), min(c.low for c in opening)
    buf = Decimal(p.breakout_buffer_pct) / 100
    day_avg = sum(((c.high + c.low + c.close) / 3 for c in cs), Decimal(0)) / len(cs)
    return {"opening_range_high": or_high, "opening_range_low": or_low, "breakout_up": or_high * (1 + buf),
            "breakout_down": or_low * (1 - buf), "day_average": day_avg}
