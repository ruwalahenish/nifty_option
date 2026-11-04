"""Version 1 debit spreads: the Bull Call Debit Spread and the Bear Put Debit Spread. Nothing else.

    Bull call debit spread   long CE at the LOWER strike, short CE at the HIGHER strike
    Bear put debit spread    long PE at the HIGHER strike, short PE at the LOWER strike

A structure is built only by DebitSpread.create, which checks everything below and raises
SpreadError listing every problem it finds. It never repairs, rounds or swaps anything.

EXPIRY PAYOFF versus INTRADAY P&L
    width, net debit, gross maximum profit, gross maximum loss and the breakeven are EXPIRY
    payoff concepts: what the position is worth if it is held to expiry. This system is an
    intraday paper-trading system and closes positions the same day, long before expiry. Its real
    profit or loss is computed from the ACTUAL exit prices of both legs (gross_pnl_from_exit_prices
    and, in a later phase, the paper fills). Expiry numbers describe the shape and the worst case of
    the structure; they are not a forecast of an intraday exit price or an intraday profit.

PAYOFF versus COSTS
    Payoff figures use option premiums and lot size only. Transaction costs are computed by the cost
    engine (cost_view) and kept in separate fields; they never change a gross figure.
"""
from collections import namedtuple
from decimal import Decimal

from ..costs.engine import QuotedLeg
from ..costs.models import BUY as COST_BUY
from ..costs.models import SELL as COST_SELL
from ..costs.models import LegExecution
from ..data import validation as data_validation
from ..errors import ContractError, SpreadError
from .coverage import uncovered_shorts

KIND_BULL_CALL = "BULL_CALL_DEBIT"
KIND_BEAR_PUT = "BEAR_PUT_DEBIT"

SpreadCostView = namedtuple("SpreadCostView", (
    "entry_costs exit_costs_at_max_loss exit_costs_at_max_profit "
    "entry_costs_inr exit_costs_at_max_loss_inr exit_costs_at_max_profit_inr "
    "round_trip_at_max_loss_inr round_trip_at_max_profit_inr net_max_loss_inr net_max_profit_inr "
    "net_max_profit_is_positive scenario_note"))

SCENARIO_NOTE = ("Exit costs are estimated for two illustrative ways of closing both legs: 'max loss' closes each "
                 "leg at one tick (the spread is worthless); 'max profit' closes the short leg at its entry price "
                 "and sells the long leg at that price plus the width (the spread is worth its full width). They "
                 "are cost scenarios, not predictions of any exit price.")


def _check_structure(legs, specs, trade_date, data_mode):
    """Return (problems, tick, warnings). problems is a list of (code, text)."""
    problems, warnings, tick = [], [], None

    def add(code, text):
        problems.append((code, text))

    if len(legs) != 2:
        add("WRONG_LEG_COUNT", "a Version 1 debit spread has exactly 2 legs, got %d" % len(legs))

    def naked():
        # listed last so the more specific structural reason is the primary code
        for short, units in uncovered_shorts(legs):
            add("NAKED_SHORT", "short %s would be uncovered for %d units" % (short.contract.symbol, units))

    if len(legs) != 2:
        naked()
        return problems, tick, warnings

    a, b = legs
    long_leg = a if a.is_long else (b if b.is_long else None)
    short_leg = a if not a.is_long else (b if not b.is_long else None)
    if a.side == b.side:
        add("TWO_LONG_LEGS" if a.is_long else "TWO_SHORT_LEGS",
            "need one long and one short leg, got two %s legs" % ("long" if a.is_long else "short"))
    if a.option_type != b.option_type:
        add("MIXED_OPTION_TYPES", "both legs must be the same option type, got %s and %s" % (a.option_type, b.option_type))
    if a.underlying != b.underlying:
        add("MIXED_UNDERLYING", "legs have different underlyings: %s and %s" % (a.underlying, b.underlying))
    for leg in legs:
        if leg.underlying != "NIFTY":
            add("UNSUPPORTED_UNDERLYING", "only NIFTY is supported, got %s" % leg.underlying)
            break
    if a.expiry != b.expiry:
        add("MIXED_EXPIRY", "legs have different expiries: %s and %s" % (a.expiry, b.expiry))
    if a.lot_size != b.lot_size:
        add("MIXED_LOT_SIZE", "legs have different lot sizes: %d and %d" % (a.lot_size, b.lot_size))
    if a.lots != b.lots:
        add("UNEQUAL_QUANTITY", "legs have different quantities: %d and %d lots" % (a.lots, b.lots))

    # lot size must equal the versioned contract layer's value for each expiry
    for leg in legs:
        try:
            layer = specs.lot_size(leg.expiry, data_mode)
        except ContractError as e:
            add("LOT_SIZE_UNAVAILABLE", "no usable lot size from the contract layer for %s: %s" % (leg.expiry, e))
            continue
        warnings.extend(layer.warnings)
        if leg.lot_size != layer.value:
            add("LOT_SIZE_MISMATCH", "leg %s has lot size %d but the contract layer says %d for expiry %s" % (
                leg.contract.symbol, leg.lot_size, layer.value, leg.expiry))
    # strike grid and tick come from the same layer
    try:
        interval = specs.strike_interval(trade_date, data_mode)
        tick_r = specs.tick_size(trade_date, data_mode)
        warnings.extend(interval.warnings + tick_r.warnings)
        tick = Decimal(str(tick_r.value))
        for leg in legs:
            if leg.strike % Decimal(str(interval.value)) != 0:
                add("OFF_GRID_STRIKE", "%s: strike is not a multiple of %s" % (leg.contract.symbol, interval.value))
            if leg.entry_price % tick != 0:
                add("OFF_TICK_PRICE", "%s: entry price %s is not a multiple of the %s tick" % (
                    leg.contract.symbol, leg.entry_price, tick))
    except ContractError as e:
        add("LAYER_UNAVAILABLE", "strike interval or tick size not usable in data_mode=%r: %s" % (data_mode, e))

    # strike order and debit, judged only when there is one long and one short of one option type
    if long_leg is not None and short_leg is not None and a.option_type == b.option_type:
        if long_leg.strike == short_leg.strike:
            add("SAME_STRIKE", "long and short strikes are equal (%s)" % long_leg.strike)
        elif long_leg.option_type == "CE" and not long_leg.strike < short_leg.strike:
            add("WRONG_STRIKE_ORDER", "bull call spread needs long strike %s below short strike %s" % (
                long_leg.strike, short_leg.strike))
        elif long_leg.option_type == "PE" and not long_leg.strike > short_leg.strike:
            add("WRONG_STRIKE_ORDER", "bear put spread needs long strike %s above short strike %s" % (
                long_leg.strike, short_leg.strike))
        debit = long_leg.entry_price - short_leg.entry_price
        width = abs(short_leg.strike - long_leg.strike)
        if debit < 0:
            add("NOT_A_DEBIT", "net premium is a CREDIT of %s points: credit spreads are not supported" % (-debit))
        elif debit == 0:
            add("ZERO_DEBIT", "net premium is zero: a genuine debit is required")
        elif width > 0 and debit >= width:
            add("DEBIT_NOT_BELOW_WIDTH", "net debit %s is not below the width %s, so there is no possible profit" % (
                debit, width))
    naked()
    return problems, tick, tuple(dict.fromkeys(warnings))


class DebitSpread(object):
    """A validated, immutable Version 1 debit spread. Build it with DebitSpread.create."""
    __slots__ = ("kind", "long_leg", "short_leg", "trade_date", "tick", "warnings", "width_points",
                 "net_debit_points", "units", "gross_max_loss_inr", "gross_max_profit_inr", "expiry_breakeven")

    def __init__(self, *args, **kwargs):
        raise TypeError("use DebitSpread.create(legs, specs, trade_date, data_mode)")

    def __setattr__(self, name, value):
        raise AttributeError("DebitSpread is immutable")

    @classmethod
    def create(cls, legs, specs, trade_date, data_mode="fixture"):
        legs = tuple(legs)
        problems, tick, warnings = _check_structure(legs, specs, trade_date, data_mode)
        if problems:
            raise SpreadError(problems)
        long_leg = legs[0] if legs[0].is_long else legs[1]
        short_leg = legs[1] if legs[0].is_long else legs[0]
        self = object.__new__(cls)
        width = abs(short_leg.strike - long_leg.strike)
        debit = long_leg.entry_price - short_leg.entry_price
        units = long_leg.units
        bull = long_leg.option_type == "CE"
        values = {
            "kind": KIND_BULL_CALL if bull else KIND_BEAR_PUT,
            "long_leg": long_leg, "short_leg": short_leg, "trade_date": trade_date, "tick": tick,
            "warnings": warnings,
            # expiry payoff concepts (see the module docstring)
            "width_points": width,
            "net_debit_points": debit,
            "units": units,
            "gross_max_loss_inr": debit * units,
            "gross_max_profit_inr": (width - debit) * units,
            "expiry_breakeven": long_leg.strike + debit if bull else long_leg.strike - debit,
        }
        for key, value in values.items():
            object.__setattr__(self, key, value)
        return self

    # ---- identity
    @property
    def underlying(self):
        return self.long_leg.underlying

    @property
    def expiry(self):
        return self.long_leg.expiry

    @property
    def lots(self):
        return self.long_leg.lots

    @property
    def lot_size(self):
        return self.long_leg.lot_size

    def __repr__(self):
        return "<%s %s %s/%s %s lots=%d debit=%s pts>" % (
            self.kind, self.expiry, self.long_leg.strike, self.short_leg.strike, self.long_leg.option_type,
            self.lots, self.net_debit_points)

    # ---- expiry payoff (NOT an intraday forecast)
    def expiry_payoff_inr(self, settlement_price):
        """Gross payoff in rupees if BOTH legs are held to expiry and the index settles at `settlement_price`.

        This is an expiry concept. It is not what an intraday exit would give.
        """
        s = Decimal(settlement_price)
        if s <= 0:
            raise SpreadError([("BAD_PRICE", "settlement price must be above zero")])
        if self.long_leg.option_type == "CE":
            long_value, short_value = max(s - self.long_leg.strike, 0), max(s - self.short_leg.strike, 0)
        else:
            long_value, short_value = max(self.long_leg.strike - s, 0), max(self.short_leg.strike - s, 0)
        return (long_value - short_value - self.net_debit_points) * self.units

    # ---- intraday P&L from actual exit prices
    def gross_pnl_from_exit_prices(self, long_exit_price, short_exit_price):
        """Gross P&L in rupees from the ACTUAL exit price of each leg, before costs.

        long leg gains (exit - entry); short leg gains (entry - exit); both x units. This is the basis
        for intraday P&L. It does not use width, maximum profit or breakeven.
        """
        long_exit, short_exit = Decimal(long_exit_price), Decimal(short_exit_price)
        if long_exit < 0 or short_exit < 0:
            raise SpreadError([("BAD_PRICE", "exit prices cannot be negative")])
        return ((long_exit - self.long_leg.entry_price) + (self.short_leg.entry_price - short_exit)) * self.units

    # ---- bridges to other layers
    def quoted_legs(self):
        """The legs as QuotedLeg objects for CostEngine.defined_risk (entry prices are quotes)."""
        return [QuotedLeg("long", COST_BUY, self.long_leg.entry_price, self.units),
                QuotedLeg("short", COST_SELL, self.short_leg.entry_price, self.units)]

    def defined_risk(self, engine, slippage_points, trade_date=None, exit_method="entry_price"):
        """Total Defined Risk (the risk gate's figure): net debit + entry costs + estimated exit costs + slippage."""
        return engine.defined_risk(self.quoted_legs(), slippage_points, trade_date or self.trade_date, self.tick,
                                   exit_method)

    def cost_view(self, engine, trade_date=None):
        """Costs kept apart from payoff. Entry and exit are costed leg by leg."""
        day = trade_date or self.trade_date
        u = self.units
        entry = engine.entry_costs([LegExecution("long", COST_BUY, self.long_leg.entry_price, u, day),
                                    LegExecution("short", COST_SELL, self.short_leg.entry_price, u, day)])
        exit_loss = engine.exit_costs([LegExecution("long", COST_SELL, self.tick, u, day),
                                       LegExecution("short", COST_BUY, self.tick, u, day)])
        short_px = self.short_leg.entry_price
        exit_profit = engine.exit_costs([LegExecution("long", COST_SELL, self.width_points + short_px, u, day),
                                         LegExecution("short", COST_BUY, short_px, u, day)])
        rt_loss, rt_profit = entry.total + exit_loss.total, entry.total + exit_profit.total
        net_loss = self.gross_max_loss_inr + rt_loss
        net_profit = self.gross_max_profit_inr - rt_profit
        return SpreadCostView(entry, exit_loss, exit_profit, entry.total, exit_loss.total, exit_profit.total,
                              rt_loss, rt_profit, net_loss, net_profit, net_profit > 0, SCENARIO_NOTE)

    def ensure_enterable(self, now, calendar):
        """Raise SpreadError(EXPIRED_CONTRACT) if either leg has expired and cannot be used for a NEW entry."""
        problems = []
        for leg in (self.long_leg, self.short_leg):
            if data_validation.contract_expired(leg.contract, now, calendar):
                problems.append(("EXPIRED_CONTRACT", "%s has expired and cannot be used for a new entry" %
                                 leg.contract.symbol))
        if problems:
            raise SpreadError(problems)
