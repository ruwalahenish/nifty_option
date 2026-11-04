"""Plain data types for the cost model. Money is Decimal rupees; prices are Decimal index points."""
import datetime
from collections import namedtuple
from decimal import ROUND_HALF_UP, Decimal

from ..errors import NiftyError

PAISE = Decimal("0.01")
CRORE = Decimal(10000000)
BUY, SELL = "BUY", "SELL"

# Components in the order they are reported.
COMPONENTS = ("brokerage", "stt", "exchange_txn", "ipft", "sebi_fee", "stamp_duty", "gst")


def paise(x):
    return Decimal(x).quantize(PAISE, rounding=ROUND_HALF_UP)


class LegExecution(namedtuple("LegExecution", "leg_id side price units trade_date")):
    """One option leg executed (or to be executed) at a price, for a number of index units.

    `units` is lots x lot_size, so costs scale with the real quantity traded.
    """
    __slots__ = ()

    def __new__(cls, leg_id, side, price, units, trade_date):
        if side not in (BUY, SELL):
            raise NiftyError("side must be BUY or SELL, got %r" % (side,))
        price = Decimal(price)
        if price <= 0:
            raise NiftyError("price must be positive, got %s" % price)
        if isinstance(units, bool) or not isinstance(units, int) or units <= 0:
            raise NiftyError("units must be a positive whole number, got %r" % (units,))
        if not isinstance(trade_date, datetime.date):
            raise NiftyError("trade_date must be a date")
        return super(LegExecution, cls).__new__(cls, leg_id, side, price, units, trade_date)

    @property
    def turnover(self):
        return self.price * self.units


class LegCosts(namedtuple("LegCosts", "execution components total warnings rule_versions")):
    """Costs of one leg execution. components: dict name -> rupees (paise precision)."""
    __slots__ = ()


class PhaseCosts(namedtuple("PhaseCosts", "label legs components total warnings")):
    """Costs of a set of executions (an entry, an exit, or actual fills), summed per component."""
    __slots__ = ()


class RoundTripCosts(namedtuple("RoundTripCosts", "entry exit total")):
    """Entry costs + exit costs. This is what a trade's net P&L is reduced by."""
    __slots__ = ()


class DefinedRisk(namedtuple("DefinedRisk", (
        "net_debit_quoted_inr entry_slippage_inr net_debit_paid_inr entry_costs "
        "entry_risk_inr estimated_exit_costs round_trip_costs_inr total_defined_risk_inr "
        "exit_estimate_method warnings"))):
    """Capital at risk for a debit spread.

    entry_risk_inr          = net debit paid (after entry slippage) + entry-side costs.
                              What leaves the account when the spread is opened.
    round_trip_costs_inr    = entry costs + estimated exit costs. What reduces net P&L.
    total_defined_risk_inr  = entry_risk_inr + estimated exit costs. The worst-case loss:
                              the whole debit is lost and both sides' costs are paid. The
                              risk gate compares THIS figure with the per-trade limit.
    A stop-loss never lowers any of these figures.
    """
    __slots__ = ()
