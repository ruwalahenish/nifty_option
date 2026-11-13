"""Helpers for the selector tests: real fixture option chains and calendars, with hand-made candle paths so every market
condition (pinned, ranging, trending, volatile) can be produced on demand."""
import datetime
from decimal import Decimal as Dec

from nifty_core import pipeline
from nifty_core.data.models import Candle
from nifty_core.market.ist import parse_ist
from nifty_core.regime import engine as RE
from nifty_core.selector import engine as E
from nifty_core.selector import params as SP
from tests import _scenarios as sc

PARAMS = SP.load_params()
_ctx = {}


def ctx():
    if "ctx" not in _ctx:
        _ctx["ctx"] = pipeline.load_context(sc.config(), "fixture", "intraday")
    return _ctx["ctx"]


def params(**override):
    values = {f: getattr(PARAMS, f) for f in SP.FIELDS}
    values.update(override)
    return SP.build_params(values)


def at(name, hhmm):
    return parse_ist("%s %s" % (sc.scenario(name).day.isoformat(), hhmm))


def candles_from(day, closes, wick=Dec(2)):
    out, prev = [], closes[0]
    start = parse_ist("%s 09:15" % day)
    for i, close in enumerate(closes):
        out.append(Candle(start + datetime.timedelta(minutes=5 * i), 5, prev, max(prev, close) + wick,
                          min(prev, close) - wick, close))
        prev = close
    return out


def decide(name, hhmm, offsets=None, p=PARAMS, chain_fn=None, candles=None, context=None, **kw):
    """select() on the fixture chain at `hhmm`, with candles ending at the chain's spot and shaped by
    offsets(n) -> list of n price offsets from that final spot (or an explicit `candles` list)."""
    prov, as_of = sc.provider(name), at(name, hhmm)
    chain = prov.get_option_chain(as_of=as_of)
    if chain_fn is not None:
        chain = chain_fn(chain)
    day = sc.scenario(name).day
    if candles is None and offsets is None:
        candles = _scenario_candles(name, as_of)
    elif candles is None:
        n = int((as_of - parse_ist("%s 09:15" % day)).total_seconds() // 300)
        candles = candles_from(day, [chain.spot + o for o in offsets(n)])
    regime = RE.classify_at(prov, sc.calendar(), as_of, ctx().regime_params)
    regime = regime._replace(metrics=dict(regime.metrics, last_close=chain.spot))
    return E.select(regime, candles, chain, prov.get_event_calendar(day), as_of, context or ctx(), p, **kw)


def pinned(amp=5):
    return lambda n: [Dec(amp) * (1 if i % 2 else -1) for i in range(n)]


def trending(slope, noise=10):
    """A steady drift of `slope` index points per candle ending at the spot, with +/- noise chop."""
    return lambda n: [Dec(slope) * (i - (n - 1)) + (Dec(noise) if i % 2 else -Dec(noise)) for i in range(n)]


def _scenario_candles(name, as_of):
    from nifty_core.selector import analysis
    return analysis.completed_candles(sc.provider(name), sc.calendar(), ctx().regime_params, as_of)
