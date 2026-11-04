"""Immutable market-data models with intrinsic validation.

Rules enforced here, at construction: timestamps are timezone-aware (stored in IST), prices and
quantities are not negative, bid does not exceed ask, candles are internally consistent, and a
contract is identified uniquely by (underlying, expiry, strike, option type).

Rules that need the versioned contract layer (strike grid, tick size, lot size, expiry) live in
nifty_core.data.validation. A quote carries NO lot size: lot size comes from the contract layer.
"""
import datetime
from collections import namedtuple
from decimal import Decimal

from ..errors import DataValidationError
from ..market.ist import IST

CE, PE = "CE", "PE"
LABEL_FIXTURE = "FIXTURE_SYNTHETIC"
LABEL_RECORDED = "RECORDED_REAL"
LABEL_LIVE_READONLY = "LIVE_READONLY"
LABELS = (LABEL_FIXTURE, LABEL_RECORDED, LABEL_LIVE_READONLY)
IMPACTS = ("LOW", "MEDIUM", "HIGH")


def aware(moment, what):
    """Return `moment` converted to IST; a naive datetime is a data error."""
    if not isinstance(moment, datetime.datetime):
        raise DataValidationError("BAD_TIMESTAMP", "%s is not a datetime: %r" % (what, moment))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise DataValidationError("NAIVE_TIMESTAMP", "%s has no time zone: %s" % (what, moment))
    return moment.astimezone(IST)


def money(value, what, allow_none=False, allow_zero=True):
    """Decimal price. Negative or non-numeric values are rejected."""
    if value is None:
        if allow_none:
            return None
        raise DataValidationError("MISSING_REQUIRED", "%s is required" % what)
    if isinstance(value, bool) or isinstance(value, float):
        raise DataValidationError("BAD_NUMBER", "%s must be a Decimal, int or numeric string (not %s)" % (
            what, type(value).__name__))
    try:
        d = Decimal(value)
    except Exception:
        raise DataValidationError("BAD_NUMBER", "%s is not a number: %r" % (what, value))
    if not d.is_finite():
        raise DataValidationError("BAD_NUMBER", "%s is not finite" % what)
    if d < 0:
        raise DataValidationError("NEGATIVE_PRICE", "%s is negative: %s" % (what, d))
    if d == 0 and not allow_zero:
        raise DataValidationError("NEGATIVE_PRICE", "%s must be above zero" % what)
    return d


def count(value, what, code):
    """Non-negative whole number, or None."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataValidationError("BAD_NUMBER", "%s must be a whole number: %r" % (what, value))
    if value < 0:
        raise DataValidationError(code, "%s is negative: %d" % (what, value))
    return value


class Candle(namedtuple("Candle", "start interval_minutes open high low close volume")):
    """One OHLC bar of the underlying. `start` is the bar's start; it ends interval_minutes later.

    volume is None where the source has none (the NIFTY index itself has no traded volume).
    """
    __slots__ = ()

    def __new__(cls, start, interval_minutes, open, high, low, close, volume=None):
        start = aware(start, "candle start")
        if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int) or interval_minutes <= 0:
            raise DataValidationError("BAD_NUMBER", "interval_minutes must be a positive whole number")
        o, h, l, c = (money(v, n, allow_zero=False) for v, n in
                      ((open, "open"), (high, "high"), (low, "low"), (close, "close")))
        if not (l <= o <= h and l <= c <= h):
            raise DataValidationError("CANDLE_INCONSISTENT", "need low <= open,close <= high at %s" % start)
        return super(Candle, cls).__new__(cls, start, interval_minutes, o, h, l, c,
                                          count(volume, "candle volume", "NEGATIVE_VOLUME"))

    @property
    def end(self):
        return self.start + datetime.timedelta(minutes=self.interval_minutes)


class OptionContract(namedtuple("OptionContract", "underlying expiry strike option_type")):
    """Identity of one listed option. Equal contracts compare equal and hash equal."""
    __slots__ = ()

    def __new__(cls, underlying, expiry, strike, option_type):
        if not isinstance(expiry, datetime.date) or isinstance(expiry, datetime.datetime):
            raise DataValidationError("BAD_TIMESTAMP", "expiry must be a date")
        if option_type not in (CE, PE):
            raise DataValidationError("BAD_OPTION_TYPE", "option_type must be CE or PE, got %r" % (option_type,))
        if not underlying:
            raise DataValidationError("MISSING_REQUIRED", "underlying is required")
        k = money(strike, "strike", allow_zero=False)
        return super(OptionContract, cls).__new__(cls, underlying, expiry, k, option_type)

    @property
    def symbol(self):
        return "%s%s%s%s" % (self.underlying, self.expiry.strftime("%y%m%d"), format(self.strike.normalize(), "f"),
                             self.option_type)


class OptionQuote(namedtuple("OptionQuote", "contract timestamp bid ask ltp bid_qty ask_qty volume open_interest")):
    """Market data for one option at one moment. Fields the source lacks are None.

    There is deliberately no lot_size here: it comes from the versioned contract layer.
    """
    __slots__ = ()

    def __new__(cls, contract, timestamp, bid=None, ask=None, ltp=None, bid_qty=None, ask_qty=None,
                volume=None, open_interest=None):
        if not isinstance(contract, OptionContract):
            raise DataValidationError("MISSING_REQUIRED", "contract must be an OptionContract")
        ts = aware(timestamp, "quote timestamp")
        b = money(bid, "bid", allow_none=True)
        a = money(ask, "ask", allow_none=True)
        last = money(ltp, "ltp", allow_none=True)
        if b is not None and a is not None and b > a:
            raise DataValidationError("BID_GT_ASK", "%s bid %s exceeds ask %s" % (contract.symbol, b, a))
        return super(OptionQuote, cls).__new__(
            cls, contract, ts, b, a, last,
            count(bid_qty, "bid_qty", "NEGATIVE_VOLUME"), count(ask_qty, "ask_qty", "NEGATIVE_VOLUME"),
            count(volume, "volume", "NEGATIVE_VOLUME"), count(open_interest, "open_interest", "NEGATIVE_OPEN_INTEREST"))

    @property
    def is_two_sided(self):
        """True when a buyer and a seller are both quoted (bid above zero and an ask)."""
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0

    @property
    def mid(self):
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread(self):
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct_of_mid(self):
        """Quoted spread as a percentage of the mid price, or None if not computable."""
        m = self.mid
        if m is None or m == 0:
            return None
        return self.spread / m * 100

    def age_seconds(self, now):
        return (aware(now, "now") - self.timestamp).total_seconds()


class OptionChain(object):
    """All quotes seen at one snapshot time, possibly for several expiries.

    Immutable after construction. Raises on a duplicate contract or a quote dated after the
    snapshot (which would be look-ahead).
    """
    __slots__ = ("underlying", "snapshot_time", "spot", "data_label", "source", "_quotes", "_index")

    def __init__(self, underlying, snapshot_time, spot, quotes, data_label, source=""):
        object.__setattr__(self, "underlying", underlying)
        object.__setattr__(self, "snapshot_time", aware(snapshot_time, "snapshot_time"))
        object.__setattr__(self, "spot", money(spot, "spot", allow_zero=False))
        if data_label not in LABELS:
            raise DataValidationError("BAD_LABEL", "data_label must be one of %s" % (LABELS,))
        object.__setattr__(self, "data_label", data_label)
        object.__setattr__(self, "source", source)
        index = {}
        for q in quotes:
            if not isinstance(q, OptionQuote):
                raise DataValidationError("MISSING_REQUIRED", "chain quotes must be OptionQuote objects")
            if q.contract.underlying != underlying:
                raise DataValidationError("WRONG_UNDERLYING", "%s is not an %s option" % (q.contract.symbol, underlying))
            if q.contract in index:
                raise DataValidationError("DUPLICATE_CONTRACT", "%s appears twice in one snapshot" % q.contract.symbol)
            if q.timestamp > self.snapshot_time:
                raise DataValidationError("QUOTE_FROM_FUTURE", "%s is stamped %s, after the snapshot %s" % (
                    q.contract.symbol, q.timestamp, self.snapshot_time))
            index[q.contract] = q
        object.__setattr__(self, "_quotes", tuple(sorted(
            index.values(), key=lambda q: (q.contract.expiry, q.contract.strike, q.contract.option_type))))
        object.__setattr__(self, "_index", index)

    def __setattr__(self, name, value):
        raise AttributeError("OptionChain is immutable")

    @property
    def quotes(self):
        return self._quotes

    def get(self, contract):
        """The quote for a contract, or None if the snapshot has none."""
        return self._index.get(contract)

    def expiries(self):
        return sorted({q.contract.expiry for q in self._quotes})

    def for_expiry(self, expiry):
        return tuple(q for q in self._quotes if q.contract.expiry == expiry)

    def strikes(self, expiry):
        return sorted({q.contract.strike for q in self._quotes if q.contract.expiry == expiry})


Event = namedtuple("Event", "when name impact label")
Event.__doc__ = """A scheduled event. `when` is an aware IST datetime, or a date for an all-day event."""


def make_event(when, name, impact, label):
    if isinstance(when, datetime.datetime):
        when = aware(when, "event time")
    elif not isinstance(when, datetime.date):
        raise DataValidationError("BAD_TIMESTAMP", "event time must be a date or an aware datetime")
    if impact not in IMPACTS:
        raise DataValidationError("BAD_IMPACT", "impact must be one of %s" % (IMPACTS,))
    if not name:
        raise DataValidationError("MISSING_REQUIRED", "event name is required")
    return Event(when, name, impact, label)
