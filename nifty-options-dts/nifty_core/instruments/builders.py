"""Build a debit spread from a chain snapshot, using conservative quotes and the contract layer's lot size."""
from ..data.models import OptionContract
from ..errors import SpreadError
from .leg import BUY, SELL, OptionLeg
from .spread import DebitSpread


def _leg(chain, contract, side, lots, specs, data_mode):
    q = chain.get(contract)
    if q is None:
        raise SpreadError([("MISSING_QUOTE", "%s is not in the snapshot" % contract.symbol)])
    price = q.ask if side == BUY else q.bid          # pay the ask when buying, receive the bid when selling
    if price is None or price <= 0:
        raise SpreadError([("NO_ASK" if side == BUY else "NO_BID", "%s has no usable %s for a %s" % (
            contract.symbol, "ask" if side == BUY else "bid", side))])
    try:
        lot_size = specs.lot_size(contract.expiry, data_mode).value        # always from the versioned layer
    except Exception as e:
        raise SpreadError([("LOT_SIZE_UNAVAILABLE", "%s: %s" % (contract.symbol, e))])
    return OptionLeg(contract, side, lots, lot_size, price)


def from_chain(chain, long_contract, short_contract, lots, specs, data_mode="fixture", trade_date=None,
               now=None, calendar=None):
    """Bull call or bear put spread from a snapshot: buy the long leg at its ask, sell the short leg at its bid.

    If `now` and `calendar` are given the spread is also checked as a NEW entry (expired contracts are refused).
    The structure is whatever the two contracts form; DebitSpread.create decides whether it is valid.
    """
    if not isinstance(long_contract, OptionContract) or not isinstance(short_contract, OptionContract):
        raise SpreadError([("BAD_LEG", "contracts must be OptionContract objects")])
    legs = [_leg(chain, long_contract, BUY, lots, specs, data_mode),
            _leg(chain, short_contract, SELL, lots, specs, data_mode)]
    day = trade_date or chain.snapshot_time.date()
    spread = DebitSpread.create(legs, specs, day, data_mode)
    if now is not None and calendar is not None:
        spread.ensure_enterable(now, calendar)
    return spread
