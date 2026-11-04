"""The market-data interface. READ-ONLY by construction.

Every public method is a getter. There is no method that accepts an order, and the safety tests
fail if one is ever added (names are checked against nifty_core.guards.forbidden). A future
real-time provider implements exactly this interface and nothing more.

`as_of` is the moment the caller is standing at. A provider must return only data that was
known at `as_of`: no bar that has not finished, no snapshot taken later.
"""
import abc


class MarketDataProvider(abc.ABC):
    @abc.abstractmethod
    def get_clock(self):
        """The provider's current moment as an aware IST datetime."""

    @abc.abstractmethod
    def get_underlying_bars(self, interval_minutes, start, end, as_of=None):
        """Completed candles of the underlying with start <= bar start and bar end <= min(end, as_of)."""

    @abc.abstractmethod
    def get_option_chain(self, expiry=None, as_of=None):
        """The latest OptionChain with snapshot_time <= as_of, optionally limited to one expiry."""

    @abc.abstractmethod
    def get_quote(self, contract, as_of=None):
        """The latest OptionQuote for a contract known at as_of. Raises DataNotAvailable if none."""

    @abc.abstractmethod
    def get_expiries(self, as_of=None):
        """Expiry dates quoted in the latest snapshot and not yet expired."""

    @abc.abstractmethod
    def get_event_calendar(self, day):
        """Scheduled events on a date."""
