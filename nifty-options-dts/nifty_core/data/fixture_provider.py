"""Read-only, deterministic provider backed by a scenario file. No network, no randomness, no writes.

A scenario file is JSON (see docs/DATA_MODEL.md). Everything is validated when it is loaded;
a file that breaks a rule is rejected whole. A scenario carries NO lot size: it is refused if it
tries to, because lot size comes from the versioned contract layer.
"""
import datetime
import json
from decimal import Decimal

from ..errors import DataNotAvailable, DataValidationError
from ..market.ist import FixedClock, to_ist
from . import validation
from .models import (LABELS, Candle, OptionChain, OptionContract, OptionQuote, aware, make_event)
from .provider import MarketDataProvider

SCHEMA_VERSION = 1


def _ts(text, what):
    if not isinstance(text, str):
        raise DataValidationError("BAD_TIMESTAMP", "%s must be an ISO text, got %r" % (what, text))
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise DataValidationError("BAD_TIMESTAMP", "%s is not an ISO timestamp: %r" % (what, text))
    return aware(moment, what)


def _date(text, what):
    try:
        return datetime.date.fromisoformat(text)
    except (TypeError, ValueError):
        raise DataValidationError("BAD_TIMESTAMP", "%s is not an ISO date: %r" % (what, text))


def _no_lot_size(node, path="scenario"):
    """A scenario must not carry a lot size anywhere."""
    if isinstance(node, dict):
        for key, value in node.items():
            if "lot" in key.lower():
                raise DataValidationError("LOT_SIZE_IN_DATA", "%s.%s: lot size must come from the contract layer, "
                                          "not from market data" % (path, key))
            _no_lot_size(value, path + "." + key)
    elif isinstance(node, list):
        for i, value in enumerate(node[:50]):                      # the shape repeats; 50 rows is enough
            _no_lot_size(value, "%s[%d]" % (path, i))


class Scenario(object):
    """A parsed, validated scenario."""

    def __init__(self, name, day, underlying, interval_minutes, data_label, candles, chains, events, meta):
        self.name, self.day, self.underlying = name, day, underlying
        self.interval_minutes, self.data_label = interval_minutes, data_label
        self.candles, self.chains, self.events, self.meta = tuple(candles), tuple(chains), tuple(events), meta


def parse_scenario(doc, specs, calendar, data_mode="fixture"):
    """Build and fully validate a Scenario from a decoded JSON dict. Raises DataValidationError."""
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise DataValidationError("BAD_SCHEMA", "unsupported scenario schema_version %r" % (doc.get("schema_version"),))
    _no_lot_size(doc)
    label = doc.get("data_label")
    if label not in LABELS:
        raise DataValidationError("BAD_LABEL", "data_label must be one of %s" % (LABELS,))
    underlying = doc.get("underlying")
    day = _date(doc.get("date"), "date")
    interval = doc.get("interval_minutes")
    session = calendar.session(day)
    if session is None:
        raise DataValidationError("MARKET_CLOSED", "%s is not a trading day" % day)

    candles, last_end = [], None
    for i, c in enumerate(doc.get("candles", [])):
        candle = Candle(_ts(c.get("start"), "candle[%d].start" % i), interval, c.get("open"), c.get("high"),
                        c.get("low"), c.get("close"), c.get("volume"))
        if candle.start.date() != day:
            raise DataValidationError("CANDLE_OUTSIDE_SESSION", "candle %s is not on %s" % (candle.start, day))
        if candle.start.time() < session.open or candle.end.time() > session.close:
            raise DataValidationError("CANDLE_OUTSIDE_SESSION", "candle %s is outside %s to %s" % (
                candle.start, session.open, session.close))
        if last_end is not None and candle.start < last_end:
            raise DataValidationError("CANDLE_ORDER", "candles overlap or are out of order at %s" % candle.start)
        last_end = candle.end
        candles.append(candle)

    chains, last_snap = [], None
    for i, ch in enumerate(doc.get("chains", [])):
        snap = _ts(ch.get("snapshot_time"), "chains[%d].snapshot_time" % i)
        quotes = []
        for j, q in enumerate(ch.get("quotes", [])):
            contract = OptionContract(underlying, _date(q.get("expiry"), "quote.expiry"), q.get("strike"), q.get("type"))
            quotes.append(OptionQuote(contract, _ts(q.get("timestamp"), "quote[%d].timestamp" % j), q.get("bid"),
                                      q.get("ask"), q.get("ltp"), q.get("bid_qty"), q.get("ask_qty"),
                                      q.get("volume"), q.get("open_interest")))
        chain = OptionChain(underlying, snap, ch.get("spot"), quotes, label, doc.get("scenario", ""))
        if last_snap is not None and snap <= last_snap:
            raise DataValidationError("CHAIN_ORDER", "chain snapshots must be in increasing time order")
        last_snap = snap
        validation.validate_chain_against_contracts(chain, specs, calendar, data_mode)
        chains.append(chain)

    events = [make_event(_ts(e["when"], "event.when") if "T" in e["when"] else _date(e["when"], "event.when"),
                         e.get("name"), e.get("impact"), label) for e in doc.get("events", [])]
    return Scenario(doc.get("scenario", ""), day, underlying, interval, label, candles, chains, events,
                    doc.get("metadata", {}))


def load_scenario_file(path, specs, calendar, data_mode="fixture"):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh, parse_float=Decimal)
    except (OSError, ValueError) as e:
        raise DataValidationError("UNREADABLE", "cannot read scenario %s: %s" % (path, e))
    return parse_scenario(doc, specs, calendar, data_mode)


class FixtureProvider(MarketDataProvider):
    """Serves one validated Scenario. Deterministic: same inputs, same outputs, every time."""

    def __init__(self, scenario, clock=None):
        self._scenario = scenario
        if clock is None:
            if scenario.chains:
                moment = scenario.chains[-1].snapshot_time
            elif scenario.candles:
                moment = scenario.candles[0].start
            else:
                raise DataNotAvailable("the scenario has no data to place a clock on")
            clock = FixedClock(moment)
        self._clock = clock

    @classmethod
    def from_file(cls, path, specs, calendar, data_mode="fixture", clock=None):
        return cls(load_scenario_file(path, specs, calendar, data_mode), clock)

    @property
    def scenario(self):
        return self._scenario

    def _as_of(self, as_of):
        return to_ist(as_of) if as_of is not None else self._clock.now()

    # ---- interface
    def get_clock(self):
        return self._clock.now()

    def get_underlying_bars(self, interval_minutes, start, end, as_of=None):
        if interval_minutes != self._scenario.interval_minutes:
            raise DataNotAvailable("only %d-minute bars are available" % self._scenario.interval_minutes)
        limit = min(to_ist(end), self._as_of(as_of))
        start = to_ist(start)
        return tuple(c for c in self._scenario.candles if c.start >= start and c.end <= limit)

    def _latest_chain(self, as_of):
        known = [c for c in self._scenario.chains if c.snapshot_time <= as_of]
        if not known:
            raise DataNotAvailable("no option chain snapshot at or before %s" % as_of)
        return known[-1]

    def get_option_chain(self, expiry=None, as_of=None):
        chain = self._latest_chain(self._as_of(as_of))
        if expiry is None:
            return chain
        quotes = chain.for_expiry(expiry)
        if not quotes:
            raise DataNotAvailable("no quotes for expiry %s in the snapshot at %s" % (expiry, chain.snapshot_time))
        return OptionChain(chain.underlying, chain.snapshot_time, chain.spot, quotes, chain.data_label, chain.source)

    def get_quote(self, contract, as_of=None):
        quote = self._latest_chain(self._as_of(as_of)).get(contract)
        if quote is None:
            raise DataNotAvailable("%s is not quoted in the latest snapshot" % contract.symbol)
        return quote

    def get_expiries(self, as_of=None):
        moment = self._as_of(as_of)
        chain = self._latest_chain(moment)
        return [e for e in chain.expiries() if e >= moment.date()]

    def get_event_calendar(self, day):
        out = []
        for e in self._scenario.events:
            when_day = e.when.date() if isinstance(e.when, datetime.datetime) else e.when
            if when_day == day:
                out.append(e)
        return out

