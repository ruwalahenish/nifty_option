"""Defined-risk CREDIT structures: bull put / bear call credit spread, iron condor, iron butterfly.

Every short leg must be covered by a long leg (the same naked-short test the debit spread uses), all legs share one
expiry and one quantity, and the wings are equal. A structure is only built by `CreditStructure.create`, which lists
every problem it finds and never repairs anything. Entry prices are conservative quotes: sell at the bid, buy at the ask.

Expiry payoff figures (max profit, max loss, breakevens) describe the SHAPE and worst case; this system closes the day it
opens, so real P&L comes from actual exit prices of every leg (gross_pnl_from_exit_prices).
"""
from decimal import Decimal

from ..costs.engine import slipped_price
from ..costs.models import BUY as COST_BUY
from ..costs.models import SELL as COST_SELL
from ..costs.models import LegExecution, paise
from ..errors import ContractError, SpreadError
from ..instruments.leg import BUY, OptionLeg

KIND_BULL_PUT = "BULL_PUT_CREDIT"
KIND_BEAR_CALL = "BEAR_CALL_CREDIT"
KIND_IRON_CONDOR = "IRON_CONDOR"
KIND_IRON_FLY = "IRON_BUTTERFLY"
KINDS = (KIND_BULL_PUT, KIND_BEAR_CALL, KIND_IRON_CONDOR, KIND_IRON_FLY)
_LEGS = {KIND_BULL_PUT: 2, KIND_BEAR_CALL: 2, KIND_IRON_CONDOR: 4, KIND_IRON_FLY: 4}


def uncovered_by_wings(legs):
    """List of (short leg, uncovered units) for every short NOT capped by a long wing of the same type further out of the money.

    The debit-spread rule (instruments.coverage) wants a long at the same or a more protective strike, which a credit
    structure's short never has by design. A credit structure is defined-risk instead because each short call has a
    long call at a HIGHER strike and each short put a long put at a LOWER strike, same expiry, at least the same units,
    so the loss is capped at the wing width. A long leg caps shorts only up to its own units."""
    capacity = {i: l.units for i, l in enumerate(legs) if l.is_long}
    result = []
    for short in (l for l in legs if not l.is_long):
        need = short.units
        for i, wing in enumerate(legs):
            if need == 0:
                break
            if not wing.is_long or capacity.get(i, 0) == 0:
                continue
            if (wing.underlying, wing.expiry, wing.option_type) != (short.underlying, short.expiry, short.option_type):
                continue
            beyond = wing.strike > short.strike if short.option_type == "CE" else wing.strike < short.strike
            if not beyond:
                continue
            used = min(need, capacity[i])
            capacity[i] -= used
            need -= used
        if need > 0:
            result.append((short, need))
    return result


def _wings(legs):
    """(call_width, put_width): the distance between the short and long leg of each side; None for an absent side."""
    out = {}
    for opt in ("CE", "PE"):
        shorts = [l for l in legs if l.option_type == opt and not l.is_long]
        longs = [l for l in legs if l.option_type == opt and l.is_long]
        out[opt] = abs(longs[0].strike - shorts[0].strike) if shorts and longs else None
    return out["CE"], out["PE"]


def _net_credit(legs):
    return sum((-l.entry_price if l.is_long else l.entry_price for l in legs), Decimal(0))


def _check(kind, legs, specs, trade_date, data_mode):
    problems = []

    def add(code, text):
        problems.append((code, text))
    if kind not in KINDS:
        add("UNKNOWN_KIND", "kind must be one of %s" % (KINDS,))
        return problems
    if len(legs) != _LEGS[kind]:
        add("WRONG_LEG_COUNT", "%s has exactly %d legs, got %d" % (kind, _LEGS[kind], len(legs)))
        return problems
    for short, units in uncovered_by_wings(legs):
        add("NAKED_SHORT", "short %s has no long wing further out of the money for %d units" % (short.contract.symbol, units))
    if len({l.expiry for l in legs}) != 1:
        add("MIXED_EXPIRY", "all legs must share one expiry")
    if len({l.lots for l in legs}) != 1 or len({l.lot_size for l in legs}) != 1:
        add("UNEQUAL_QUANTITY", "all legs must trade the same lots and lot size")
    if any(l.underlying != "NIFTY" for l in legs):
        add("UNSUPPORTED_UNDERLYING", "only NIFTY is supported")
    for leg_ in legs:
        try:
            layer = specs.lot_size(leg_.expiry, data_mode)
            if leg_.lot_size != layer.value:
                add("LOT_SIZE_MISMATCH", "%s lot size %d but the contract layer says %d" % (
                    leg_.contract.symbol, leg_.lot_size, layer.value))
        except ContractError as e:
            add("LOT_SIZE_UNAVAILABLE", "%s: %s" % (leg_.contract.symbol, e))
    try:
        interval = Decimal(str(specs.strike_interval(trade_date, data_mode).value))
        tick = Decimal(str(specs.tick_size(trade_date, data_mode).value))
        for leg_ in legs:
            if leg_.strike % interval != 0:
                add("OFF_GRID_STRIKE", "%s is not on the %s strike grid" % (leg_.contract.symbol, interval))
            if leg_.entry_price % tick != 0:
                add("OFF_TICK_PRICE", "%s price %s is not on the %s tick" % (leg_.contract.symbol, leg_.entry_price, tick))
    except ContractError as e:
        add("LAYER_UNAVAILABLE", "strike grid or tick not usable in data_mode=%r: %s" % (data_mode, e))
    if problems:
        return problems
    calls = [l for l in legs if l.option_type == "CE"]
    puts = [l for l in legs if l.option_type == "PE"]
    sc_ = next((l for l in calls if not l.is_long), None)
    sp = next((l for l in puts if not l.is_long), None)
    lc = next((l for l in calls if l.is_long), None)
    lp = next((l for l in puts if l.is_long), None)
    if kind == KIND_BULL_PUT:
        if calls or sp is None or lp is None or not lp.strike < sp.strike:
            add("WRONG_STRUCTURE", "bull put spread: sell a put, buy a LOWER put, no calls")
    elif kind == KIND_BEAR_CALL:
        if puts or sc_ is None or lc is None or not lc.strike > sc_.strike:
            add("WRONG_STRUCTURE", "bear call spread: sell a call, buy a HIGHER call, no puts")
    else:
        if not (sc_ and sp and lc and lp) or len(calls) != 2 or len(puts) != 2:
            add("WRONG_STRUCTURE", "%s needs a short and a long call and a short and a long put" % kind)
        elif not (lp.strike < sp.strike <= sc_.strike < lc.strike):
            add("WRONG_STRUCTURE", "wings must lie outside the short strikes (long put < short put <= short call < long call)")
        elif kind == KIND_IRON_FLY and sc_.strike != sp.strike:
            add("WRONG_STRUCTURE", "an iron butterfly sells the call and the put at the SAME strike")
        elif kind == KIND_IRON_CONDOR and not sp.strike < sc_.strike:
            add("WRONG_STRUCTURE", "an iron condor's short put must be below its short call")
        else:
            cw, pw = _wings(legs)
            if cw != pw:
                add("UNEQUAL_WINGS", "call wing %s and put wing %s must be equal" % (cw, pw))
    if problems:
        return problems
    credit = _net_credit(legs)
    width = max(w for w in _wings(legs) if w is not None)
    if credit <= 0:
        add("NOT_A_CREDIT", "net premium is a debit of %s points" % (-credit))
    elif credit >= width:
        add("CREDIT_NOT_BELOW_WIDTH", "credit %s is not below the width %s: no loss would be possible, so the quotes "
            "are suspect" % (credit, width))
    return problems


class CreditStructure(object):
    """A validated, immutable credit structure. Build it with CreditStructure.create."""
    __slots__ = ("kind", "legs", "trade_date", "tick", "width_points", "net_credit_points", "units",
                 "gross_max_profit_inr", "gross_max_loss_inr", "lower_breakeven", "upper_breakeven")

    def __init__(self, *args, **kwargs):
        raise TypeError("use CreditStructure.create(kind, legs, specs, trade_date, data_mode)")

    def __setattr__(self, name, value):
        raise AttributeError("CreditStructure is immutable")

    @classmethod
    def create(cls, kind, legs, specs, trade_date, data_mode="fixture"):
        legs = tuple(legs)
        problems = _check(kind, legs, specs, trade_date, data_mode)
        if problems:
            raise SpreadError(problems)
        credit = _net_credit(legs)
        width = max(w for w in _wings(legs) if w is not None)
        units = legs[0].units
        short_call = next((l for l in legs if not l.is_long and l.option_type == "CE"), None)
        short_put = next((l for l in legs if not l.is_long and l.option_type == "PE"), None)
        values = {
            "kind": kind, "legs": legs, "trade_date": trade_date,
            "tick": Decimal(str(specs.tick_size(trade_date, data_mode).value)), "width_points": width,
            "net_credit_points": credit, "units": units,
            "gross_max_profit_inr": credit * units, "gross_max_loss_inr": (width - credit) * units,
            "lower_breakeven": short_put.strike - credit if short_put is not None else None,
            "upper_breakeven": short_call.strike + credit if short_call is not None else None,
        }
        self = object.__new__(cls)
        for key, value in values.items():
            object.__setattr__(self, key, value)
        return self

    @property
    def expiry(self):
        return self.legs[0].expiry

    @property
    def lots(self):
        return self.legs[0].lots

    @property
    def lot_size(self):
        return self.legs[0].lot_size

    def reward_to_risk(self):
        """Gross max profit / gross max loss at expiry (a shape figure, not a forecast)."""
        return self.gross_max_profit_inr / self.gross_max_loss_inr

    def __repr__(self):
        return "<%s %s %s credit=%s pts>" % (self.kind, self.expiry, "/".join(
            "%s%s%s" % ("+" if l.is_long else "-", l.strike, l.option_type) for l in self.legs), self.net_credit_points)

    def gross_pnl_from_exit_prices(self, exit_prices):
        """Gross P&L in rupees from the ACTUAL price each leg is closed at (a list in the same order as `legs`)."""
        if len(exit_prices) != len(self.legs):
            raise SpreadError([("BAD_PRICE", "need one exit price per leg")])
        total = Decimal(0)
        for leg_, px in zip(self.legs, exit_prices):
            px = Decimal(px)
            if px < 0:
                raise SpreadError([("BAD_PRICE", "exit prices cannot be negative")])
            total += (px - leg_.entry_price) if leg_.is_long else (leg_.entry_price - px)
        return total * self.units

    def defined_risk_inr(self, engine, slippage_points, trade_date=None):
        """Capital at risk, comparable to the debit spread's Total Defined Risk: worst-case loss + slippage on every leg
        + entry costs + estimated exit costs (each leg closing at its entry quote, a conservative stand-in).
        Returns (total, parts dict)."""
        day = trade_date or self.trade_date
        entry, closing, slip = [], [], Decimal(0)
        for i, l in enumerate(self.legs):
            side = COST_BUY if l.is_long else COST_SELL
            fill = slipped_price(l.entry_price, side, slippage_points, self.tick)
            slip += abs(fill - l.entry_price) * self.units
            entry.append(LegExecution("leg%d" % i, side, fill, self.units, day))
            closing.append(LegExecution("leg%d" % i, COST_SELL if l.is_long else COST_BUY, l.entry_price, self.units, day))
        entry_costs, exit_costs = engine.entry_costs(entry), engine.exit_costs(closing)
        total = self.gross_max_loss_inr + slip + entry_costs.total + exit_costs.total
        return paise(total), {"max_loss_inr": paise(self.gross_max_loss_inr), "slippage_inr": paise(slip),
                              "entry_costs_inr": paise(entry_costs.total),
                              "estimated_exit_costs_inr": paise(exit_costs.total)}


def priced_leg(chain, contract, side, lots, specs, data_mode):
    """An OptionLeg priced conservatively from the chain (sell at the bid, buy at the ask)."""
    q = chain.get(contract)
    if q is None:
        raise SpreadError([("MISSING_QUOTE", "%s is not in the snapshot" % contract.symbol)])
    price = q.ask if side == BUY else q.bid
    if price is None or price <= 0:
        raise SpreadError([("NO_ASK" if side == BUY else "NO_BID", "%s has no usable %s" % (
            contract.symbol, "ask" if side == BUY else "bid"))])
    try:
        lot_size = specs.lot_size(contract.expiry, data_mode).value
    except ContractError as e:
        raise SpreadError([("LOT_SIZE_UNAVAILABLE", "%s: %s" % (contract.symbol, e))])
    return OptionLeg(contract, side, lots, lot_size, price)
