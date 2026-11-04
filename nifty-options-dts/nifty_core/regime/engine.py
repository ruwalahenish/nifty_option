"""Version 1 market regime engine.

WHAT IT IS
    A deterministic function that DESCRIBES the market as it stands at one moment, from the
    completed 5-minute candles of the underlying. It does not predict prices, and none of its
    outputs is a probability of profit.

WHAT IT USES
    Only candles that had finished at `as_of` (end <= as_of). Candles after `as_of`, or still
    forming, are ignored and counted, never used. No option-chain data, no events, no future.

OUTCOMES
    BULLISH / BEARISH      enough agreeing evidence and none against it
    SIDEWAYS               a narrow day range and a small net move
    HIGH_VOLATILITY        abnormal movement (takes precedence over direction: it is not a signal to trade)
    NO_TRADE               data is fine but the evidence is conflicting or too weak
    NO_DATA                data is missing, late, gapped, stale, insufficient or of unverified origin

DECISION ORDER (first match wins)
    1. data gate        any data problem                                -> NO_DATA
    2. volatility gate  a bar range or the realised volatility too big   -> HIGH_VOLATILITY
    3. direction        bull votes >= votes_required, no bear votes, AND the net_move item itself votes bull
                        -> BULLISH   (mirror: BEARISH). net_move is the anchor: it is the only item with an absolute
                        size, so three scale-free items cannot make a direction out of noise in a narrow range.
    4. sideways         day range <= sideways_max_range_pct and |net move| < net_move_pct -> SIDEWAYS
    5. otherwise        some votes on both sides -> NO_TRADE (CONFLICTING_EVIDENCE), else NO_TRADE (INSUFFICIENT_EVIDENCE)

THE FOUR EVIDENCE ITEMS (each votes BULL, BEAR or NEUTRAL)
    net_move         (last close / day open - 1) in %        >= +net_move_pct bull, <= -net_move_pct bear
    range_position   (last close - day low) / (day high - day low)   >= range_position_high bull, <= range_position_low bear
    recent_candles   in the last recent_window candles, up (close > open) or down (close < open) count
                     >= recent_dominance                      bull for up, bear for down
    swing_structure  the last structure_window candles in three equal blocks: block highs AND block lows
                     all rising -> bull (higher highs, higher lows); all falling -> bear

THE TWO VOLATILITY TESTS
    abnormal bar     the largest single-candle range, (high - low) / open in %    >= abnormal_bar_range_pct
    realised vol     population standard deviation of 5-minute close-to-close % returns  >= high_vol_stdev_pct

evidence_score = (bull votes - bear votes) / 4. It is a model summary of how the four evidence items
lean, NOT a probability and NOT a forecast. Thresholds live in config/regime_thresholds.json (our
assumptions, not exchange facts, not market data).
"""
from collections import OrderedDict, namedtuple
from decimal import Decimal, localcontext

from ..data.models import LABEL_FIXTURE, Candle
from ..errors import DataNotAvailable
from ..market.ist import ist_datetime, to_ist
from .params import N_EVIDENCE

BULLISH, BEARISH, SIDEWAYS = "BULLISH", "BEARISH", "SIDEWAYS"
HIGH_VOLATILITY, NO_DATA, NO_TRADE = "HIGH_VOLATILITY", "NO_DATA", "NO_TRADE"
LABELS = (BULLISH, BEARISH, SIDEWAYS, HIGH_VOLATILITY, NO_DATA, NO_TRADE)
BULL, BEAR, NEUTRAL = "BULL", "BEAR", "NEUTRAL"

NOTE = ("Describes conditions at the decision time from completed candles. Not a prediction. evidence_score is a model "
        "summary of how the evidence leans, not a probability of profit.")

Evidence = namedtuple("Evidence", "name observed rule vote")
Reason = namedtuple("Reason", "code text")


class RegimeResult(namedtuple("RegimeResult", (
        "label as_of reasons evidence metrics data_issues bull_votes bear_votes evidence_score "
        "candles_used ignored_future_candles thresholds_fingerprint note"))):
    __slots__ = ()

    def to_journal_dict(self):
        """Plain JSON-friendly structure for the trading journal."""
        return {
            "label": self.label,
            "as_of": self.as_of.isoformat(),
            "reasons": [{"code": r.code, "text": r.text} for r in self.reasons],
            "evidence": [{"name": e.name, "observed": e.observed, "rule": e.rule, "vote": e.vote} for e in self.evidence],
            "metrics": {k: (str(v) if v is not None else None) for k, v in self.metrics.items()},
            "data_issues": [{"code": r.code, "text": r.text} for r in self.data_issues],
            "bull_votes": self.bull_votes, "bear_votes": self.bear_votes,
            "evidence_score": str(self.evidence_score) if self.evidence_score is not None else None,
            "candles_used": self.candles_used, "ignored_future_candles": self.ignored_future_candles,
            "thresholds_fingerprint": self.thresholds_fingerprint, "note": self.note,
        }


def _pct(x, digits=4):
    return str(x.quantize(Decimal(1).scaleb(-digits)))


def _no_data(as_of, issues, params, used, ignored, metrics=None):
    reasons = tuple(issues)
    return RegimeResult(NO_DATA, as_of, reasons, (), metrics or OrderedDict(), tuple(issues), 0, 0, None,
                        used, ignored, params.fingerprint, NOTE)


def _data_issues(usable, as_of, params, session, data_mode, data_label):
    issues = []
    if data_mode != "fixture" and (data_label is None or data_label == LABEL_FIXTURE):
        issues.append(Reason("UNVERIFIED_SOURCE", "data_mode=%r needs data labelled as real; got %r" % (
            data_mode, data_label)))
    if not usable:
        issues.append(Reason("NO_CANDLES", "no completed candle at or before %s" % as_of))
        return issues
    day = session.date
    for c in usable:
        if c.interval_minutes != params.interval_minutes:
            issues.append(Reason("WRONG_INTERVAL", "candle %s is %d-minute; need %d" % (
                c.start, c.interval_minutes, params.interval_minutes)))
            break
    if any(c.start.date() != day for c in usable):
        issues.append(Reason("DAY_MISMATCH", "candles are not all from the session date %s" % day))
    if usable[0].start.time() != session.open:
        issues.append(Reason("LATE_START", "the first candle starts %s, not at the session open %s" % (
            usable[0].start.time(), session.open)))
    if any(c.end.time() > session.close for c in usable if c.end.date() == day):
        issues.append(Reason("OUTSIDE_SESSION", "a candle ends after the session close %s" % session.close))
    for prev, cur in zip(usable, usable[1:]):
        if cur.start < prev.end:
            issues.append(Reason("OUT_OF_ORDER", "candle %s overlaps or precedes %s" % (cur.start, prev.start)))
            break
        if cur.start > prev.end:
            issues.append(Reason("GAP_IN_CANDLES", "no candle between %s and %s; the day's history has a hole, so "
                                 "nothing is classified until the next session" % (prev.end, cur.start)))
            break
    if len(usable) < params.min_bars:
        issues.append(Reason("INSUFFICIENT_HISTORY", "%d completed candles; need at least %d" % (
            len(usable), params.min_bars)))
    reference = min(as_of, ist_datetime(day, session.close))
    lag = (reference - usable[-1].end).total_seconds() / 60.0
    if lag > params.max_data_lag_minutes:
        issues.append(Reason("STALE_DATA", "the last completed candle ended %.1f minutes before %s (limit %d)" % (
            lag, reference.time(), params.max_data_lag_minutes)))
    return issues


def classify(candles, as_of, params, session, data_mode="fixture", data_label=None):
    """Classify the market at `as_of`. See the module docstring for the exact logic."""
    as_of = to_ist(as_of)                                  # a naive moment raises ValueError
    candles = list(candles)
    if not all(isinstance(c, Candle) for c in candles):
        return _no_data(as_of, [Reason("INVALID_CANDLE", "every item must be a validated Candle object")], params, 0,
                        len(candles))
    if session is None:
        return _no_data(as_of, [Reason("MARKET_CLOSED", "no trading session on %s" % as_of.date())], params, 0,
                        len(candles))
    if as_of.date() != session.date:
        return _no_data(as_of, [Reason("DAY_MISMATCH", "as_of date %s is not the session date %s" % (
            as_of.date(), session.date))], params, 0, len(candles))
    usable = [c for c in candles if c.end <= as_of]        # the look-ahead guard: finished candles only
    ignored = len(candles) - len(usable)
    issues = _data_issues(usable, as_of, params, session, data_mode, data_label)
    if issues:
        return _no_data(as_of, issues, params, len(usable), ignored)

    with localcontext() as ctx:
        ctx.prec = 28
        return _classify_clean(usable, ignored, as_of, params)


def _classify_clean(cs, ignored, as_of, p):
    hundred = Decimal(100)
    day_open, last_close = cs[0].open, cs[-1].close
    day_high, day_low = max(c.high for c in cs), min(c.low for c in cs)
    net_move = (last_close / day_open - 1) * hundred
    range_pct = (day_high - day_low) / day_open * hundred
    span = day_high - day_low
    range_position = (last_close - day_low) / span if span > 0 else None

    recent = cs[-p.recent_window:]
    up = sum(1 for c in recent if c.close > c.open)
    down = sum(1 for c in recent if c.close < c.open)

    block = p.structure_window // 3
    window = cs[-p.structure_window:]
    blocks = [window[i * block:(i + 1) * block] for i in range(3)]
    highs = [max(c.high for c in b) for b in blocks]
    lows = [min(c.low for c in b) for b in blocks]
    rising = highs[0] < highs[1] < highs[2] and lows[0] < lows[1] < lows[2]
    falling = highs[0] > highs[1] > highs[2] and lows[0] > lows[1] > lows[2]

    bar_ranges = [(c.high - c.low) / c.open * hundred for c in cs]
    max_bar_range = max(bar_ranges)
    returns = [(b.close / a.close - 1) * hundred for a, b in zip(cs, cs[1:])]
    mean = sum(returns) / len(returns)
    stdev = (sum((r - mean) ** 2 for r in returns) / len(returns)).sqrt()

    metrics = OrderedDict([
        ("day_open", day_open), ("last_close", last_close), ("day_high", day_high), ("day_low", day_low),
        ("net_move_pct", net_move), ("range_pct", range_pct), ("range_position", range_position),
        ("recent_up_candles", up), ("recent_down_candles", down),
        ("block_highs", "/".join(str(h) for h in highs)), ("block_lows", "/".join(str(l) for l in lows)),
        ("max_bar_range_pct", max_bar_range), ("realised_stdev_pct", stdev)])

    ev = []
    vote = BULL if net_move >= p.net_move_pct else (BEAR if net_move <= -p.net_move_pct else NEUTRAL)
    ev.append(Evidence("net_move", "%s%%" % _pct(net_move),
                       "bull if >= +%s%%, bear if <= -%s%%" % (p.net_move_pct, p.net_move_pct), vote))
    if range_position is None:
        ev.append(Evidence("range_position", "n/a (zero range)", "needs a non-zero day range", NEUTRAL))
    else:
        vote = BULL if range_position >= p.range_position_high else (
            BEAR if range_position <= p.range_position_low else NEUTRAL)
        ev.append(Evidence("range_position", _pct(range_position),
                           "bull if >= %s, bear if <= %s" % (p.range_position_high, p.range_position_low), vote))
    vote = BULL if up >= p.recent_dominance else (BEAR if down >= p.recent_dominance else NEUTRAL)
    ev.append(Evidence("recent_candles", "%d up, %d down of last %d" % (up, down, len(recent)),
                       "bull if >= %d up, bear if >= %d down" % (p.recent_dominance, p.recent_dominance), vote))
    vote = BULL if rising else (BEAR if falling else NEUTRAL)
    ev.append(Evidence("swing_structure", "block highs %s | lows %s" % (
        "/".join(str(h) for h in highs), "/".join(str(l) for l in lows)),
        "bull if both highs and lows rise across %d blocks of %d candles; bear if both fall" % (3, block), vote))
    bull = sum(1 for e in ev if e.vote == BULL)
    bear = sum(1 for e in ev if e.vote == BEAR)
    score = Decimal(bull - bear) / Decimal(N_EVIDENCE)

    def result(label, reasons):
        return RegimeResult(label, as_of, tuple(reasons), tuple(ev), metrics, (), bull, bear, score, len(cs), ignored,
                            p.fingerprint, NOTE)

    vol_reasons = []
    if max_bar_range >= p.abnormal_bar_range_pct:
        vol_reasons.append(Reason("ABNORMAL_BAR_RANGE", "largest candle range %s%% >= %s%%" % (
            _pct(max_bar_range), p.abnormal_bar_range_pct)))
    if stdev >= p.high_vol_stdev_pct:
        vol_reasons.append(Reason("HIGH_REALISED_VOLATILITY", "standard deviation of 5-minute returns %s%% >= %s%%" % (
            _pct(stdev), p.high_vol_stdev_pct)))
    if vol_reasons:
        return result(HIGH_VOLATILITY, vol_reasons)

    anchor = ev[0].vote                                    # the net_move item
    if bull >= p.votes_required and bear == 0 and anchor == BULL:
        return result(BULLISH, [Reason("BULL_VOTES", "%d of %d evidence items vote bull (including net_move), none vote bear" % (
            bull, N_EVIDENCE))])
    if bear >= p.votes_required and bull == 0 and anchor == BEAR:
        return result(BEARISH, [Reason("BEAR_VOTES", "%d of %d evidence items vote bear (including net_move), none vote bull" % (
            bear, N_EVIDENCE))])
    if range_pct <= p.sideways_max_range_pct and abs(net_move) < p.net_move_pct:
        return result(SIDEWAYS, [Reason("NARROW_RANGE", "day range %s%% <= %s%% and net move %s%% is inside +/-%s%%" % (
            _pct(range_pct), p.sideways_max_range_pct, _pct(net_move), p.net_move_pct))])
    if bull > 0 and bear > 0:
        return result(NO_TRADE, [Reason("CONFLICTING_EVIDENCE", "%d bull and %d bear votes" % (bull, bear))])
    return result(NO_TRADE, [Reason("INSUFFICIENT_EVIDENCE", "%d bull and %d bear votes; %d are needed with none against" % (
        bull, bear, p.votes_required))])


def classify_at(provider, calendar, as_of, params, data_mode="fixture", data_label=None):
    """Fetch the day's candles from a provider (which itself hides anything after as_of) and classify."""
    as_of = to_ist(as_of)
    session = calendar.session(as_of.date())
    if session is None:
        return classify([], as_of, params, None, data_mode, data_label)
    try:
        bars = provider.get_underlying_bars(params.interval_minutes, ist_datetime(session.date, session.open),
                                            ist_datetime(session.date, session.close), as_of=as_of)
    except DataNotAvailable as e:
        return _no_data(as_of, [Reason("PROVIDER_UNAVAILABLE", str(e))], params, 0, 0)
    return classify(bars, as_of, params, session, data_mode, data_label)
