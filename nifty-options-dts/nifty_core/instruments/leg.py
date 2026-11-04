"""One option leg of a structure: a contract, a side, a quantity and an entry price.

lot_size is carried on the leg only so a mismatch can be detected and refused. The spread
builder compares it with the versioned contract layer; a leg whose lot size disagrees with the
layer is rejected, never corrected.
"""
from collections import namedtuple
from decimal import Decimal

from ..data.models import OptionContract
from ..errors import SpreadError

BUY, SELL = "BUY", "SELL"


class OptionLeg(namedtuple("OptionLeg", "contract side lots lot_size entry_price")):
    """Immutable. Prices are Decimal index points per unit; quantities are whole lots."""
    __slots__ = ()

    def __new__(cls, contract, side, lots, lot_size, entry_price):
        if not isinstance(contract, OptionContract):
            raise SpreadError([("BAD_LEG", "contract must be an OptionContract")])
        if side not in (BUY, SELL):
            raise SpreadError([("BAD_LEG", "side must be BUY or SELL, got %r" % (side,))])
        for name, value in (("lots", lots), ("lot_size", lot_size)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SpreadError([("BAD_LEG", "%s must be a positive whole number, got %r" % (name, value))])
        if isinstance(entry_price, (bool, float)):
            raise SpreadError([("BAD_LEG", "entry_price must be a Decimal, int or numeric string, not a float")])
        try:
            price = Decimal(entry_price)
        except Exception:
            raise SpreadError([("BAD_LEG", "entry_price is not a number: %r" % (entry_price,))])
        if not price.is_finite() or price <= 0:
            raise SpreadError([("BAD_LEG", "entry_price must be above zero, got %s" % price)])
        return super(OptionLeg, cls).__new__(cls, contract, side, lots, lot_size, price)

    @property
    def units(self):
        """Index units traded: lots x lot size."""
        return self.lots * self.lot_size

    @property
    def strike(self):
        return self.contract.strike

    @property
    def option_type(self):
        return self.contract.option_type

    @property
    def expiry(self):
        return self.contract.expiry

    @property
    def underlying(self):
        return self.contract.underlying

    @property
    def is_long(self):
        return self.side == BUY
