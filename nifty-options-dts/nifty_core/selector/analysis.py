"""Market condition analysis from COMPLETED candles only (end <= as_of). Describes; never predicts.

Measures
    trend         net move since the day's open, and EFFICIENCY (net move / total path: 1 = a straight line, near 0 = chop)
    volatility    population stdev of 5-minute close-to-close % returns; expanding when the recent stdev is much bigger
    momentum      % change of the close over the last `momentum_window` candles
    levels        support / resistance from the opening range, the day's high and low, and swing pivots
    structure     TRENDING_BULLISH / TRENDING_BEARISH / RANGE_BOUND / UNCLEAR (a day that is neither: no sell-or-buy-the-trend edge)
Everything here is a description of what has happened, never a probability of what will.
"""
from collections import namedtuple
from decimal import Decimal

from ..errors import DataNotAvailable
from ..market.ist import ist_datetime, to_ist

TRENDING_BULLISH, TRENDING_BEARISH, RANGE_BOUND, UNCLEAR = "TRENDING_BULLISH", "TRENDING_BEARISH", "RANGE_BOUND", "UNCLEAR"
VOL_LOW, VOL_NORMAL, VOL_HIGH = "LOW", "NORMAL", "HIGH"
MOM_UP, MOM_DOWN, MOM_FLAT, MOM_WEAK_UP, MOM_WEAK_DOWN = "UP", "DOWN", "FLAT", "WEAK_UP", "WEAK_DOWN"
ZERO = Decimal(0)
PIVOT_SPAN = 2

MarketAnalysis = namedtuple("MarketAnalysis", (
    "as_of structure strength volatility vol_expanding momentum spot day_open day_high day_low day_average "
    "net_move_pct efficiency range_pct range_position stdev_pct recent_stdev_pct momentum_pct opening_high "
    "opening_low support resistance supports resistances candles_used summary"))


def _p(x):
    return "{:,.2f}".format(Decimal(x))


def _stdev(values):
    if len(values) < 2:
        return ZERO
    mean = sum(values, ZERO) / len(values)
    return (sum(((v - mean) ** 2 for v in values), ZERO) / len(values)).sqrt()


def _pivots(cs):
    highs, lows = [], []
    for i in range(PIVOT_SPAN, len(cs) - PIVOT_SPAN):
        around = cs[i - PIVOT_SPAN:i] + cs[i + 1:i + 1 + PIVOT_SPAN]
        if all(cs[i].high > c.high for c in around):
            highs.append(cs[i].high)
        if all(cs[i].low < c.low for c in around):
            lows.append(cs[i].low)
    return highs, lows


def analyse(candles, as_of, p, opening_range_candles=6):
    """MarketAnalysis at as_of, or None when fewer than `min_candles` have completed."""
    as_of = to_ist(as_of)
    cs = [c for c in candles if c.end <= as_of]
    if len(cs) < p.min_candles:
        return None
    day_open, spot = cs[0].open, cs[-1].close
    day_high, day_low = max(c.high for c in cs), min(c.low for c in cs)
    day_avg = sum(((c.high + c.low + c.close) / 3 for c in cs), ZERO) / len(cs)
    path_points = [day_open] + [c.close for c in cs]
    moves = [abs(b - a) for a, b in zip(path_points, path_points[1:])]
    path = sum(moves, ZERO)
    net_pct = (spot / day_open - 1) * 100
    efficiency = abs(spot - day_open) / path if path else ZERO
    rets = [(b / a - 1) * 100 for a, b in zip(path_points, path_points[1:])]
    stdev, recent = _stdev(rets), _stdev(rets[-6:])
    rng_pct = (day_high - day_low) / day_open * 100
    rpos = (spot - day_low) / (day_high - day_low) if day_high > day_low else Decimal("0.5")
    w = min(p.momentum_window, len(path_points) - 1)
    mom = (spot / path_points[-1 - w] - 1) * 100

    if mom >= p.momentum_min_pct:
        mom_label = MOM_UP
    elif mom <= -p.momentum_min_pct:
        mom_label = MOM_DOWN
    elif abs(mom) <= p.momentum_flat_max_pct:
        mom_label = MOM_FLAT
    else:
        mom_label = MOM_WEAK_UP if mom > 0 else MOM_WEAK_DOWN
    vol = VOL_HIGH if stdev >= p.vol_high_stdev_pct else VOL_LOW if stdev <= p.vol_low_stdev_pct else VOL_NORMAL
    expanding = stdev > 0 and recent >= stdev * p.vol_expansion_ratio

    strength = ZERO
    if net_pct >= p.trend_net_move_pct and efficiency >= p.trend_efficiency_min and spot > day_avg:
        structure = TRENDING_BULLISH
    elif net_pct <= -p.trend_net_move_pct and efficiency >= p.trend_efficiency_min and spot < day_avg:
        structure = TRENDING_BEARISH
    elif (efficiency <= p.range_efficiency_max and abs(net_pct) <= p.range_net_move_max_pct
          and rng_pct <= p.range_day_range_max_pct):
        structure = RANGE_BOUND
    else:
        structure = UNCLEAR
    if structure in (TRENDING_BULLISH, TRENDING_BEARISH):
        strength = "STRONG" if (abs(net_pct) >= p.strong_net_move_pct and efficiency >= p.strong_efficiency_min) \
            else "MODERATE"
    elif structure == RANGE_BOUND:
        strength = "TIGHT" if rng_pct <= p.fly_day_range_max_pct else "NORMAL"
    else:
        strength = "NONE"

    n = min(opening_range_candles, len(cs))
    o_high, o_low = max(c.high for c in cs[:n]), min(c.low for c in cs[:n])
    p_highs, p_lows = _pivots(cs)
    resistances = sorted({day_high, o_high, *p_highs})
    supports = sorted({day_low, o_low, *p_lows})
    above = [x for x in resistances if x > spot]
    below = [x for x in supports if x < spot]
    resistance, support = (min(above) if above else None), (max(below) if below else None)

    summary = ("%s (%s), volatility %s%s, momentum %s; net move %+.2f%%, efficiency %.2f, day range %.2f%%, "
               "stdev of 5-min moves %.3f%%" % (
                   structure.replace("_", " ").lower(), strength.lower(), vol.lower(),
                   " and expanding" if expanding else "", mom_label.lower(), net_pct, efficiency, rng_pct, stdev))
    return MarketAnalysis(as_of, structure, strength, vol, expanding, mom_label, spot, day_open, day_high, day_low,
                          day_avg, net_pct, efficiency, rng_pct, rpos, stdev, recent, mom, o_high, o_low, support,
                          resistance, tuple(supports), tuple(resistances), len(cs), summary)


def analysis_to_json(a):
    return {k: (None if v is None else (str(v) if isinstance(v, Decimal) else
                                         [str(x) for x in v] if isinstance(v, tuple) else
                                         v.isoformat() if hasattr(v, "isoformat") else v))
            for k, v in a._asdict().items()}


def completed_candles(provider, calendar, regime_params, as_of):
    """The session's candles completed by as_of, through the read-only provider. Empty when none are available."""
    as_of = to_ist(as_of)
    session = calendar.session(as_of.date())
    if session is None:
        return []
    try:
        return list(provider.get_underlying_bars(regime_params.interval_minutes, ist_datetime(session.date, session.open),
                                                 ist_datetime(session.date, session.close), as_of=as_of))
    except DataNotAvailable:
        return []
