"""One view of a trade candidate's legs, whatever its shape.

The risk gate, the paper executor and the position manager were written for a two-leg debit spread (a `TradeCandidate`
that carries a `DebitSpread` in `.spread`). The adaptive selector also proposes credit structures (bull put / bear call
credit spread, iron condor, iron butterfly), carried by a `StructureCandidate` with a `CreditStructure` in `.structure`.
These helpers let the three layers treat both the same way without special cases scattered through them:

    leg_plan(c)        the entry legs: [(leg_id, contract, side, lots, units)]
    symbols(c)         every contract symbol the candidate would hold
    symbol_fields(c)   (long_symbol, short_symbol) for the approval and ledger ('+'-joined when there are several)
    gross_pnl(...)     gross P&L from entry and exit fills, leg by leg
    structure_of(c)    the validated DebitSpread or CreditStructure

For a debit spread the leg ids stay "long" and "short", so every existing figure and journal record is unchanged.
No network, no orders. PAPER only.
"""
from decimal import Decimal

from .costs.models import BUY, SELL


def is_candidate(c):
    return hasattr(c, "spread") or hasattr(c, "structure")


def is_structure_candidate(c):
    return hasattr(c, "structure")


def structure_of(c):
    return c.structure if is_structure_candidate(c) else c.spread


def legs_of(c):
    s = structure_of(c)
    return tuple(s.legs) if is_structure_candidate(c) else (s.long_leg, s.short_leg)


def leg_plan(c):
    """Entry legs as [(leg_id, contract, side, lots, units)]."""
    if is_structure_candidate(c):
        return [("leg%d" % i, l.contract, l.side, l.lots, l.units) for i, l in enumerate(c.structure.legs)]
    s = c.spread
    return [("long", s.long_leg.contract, BUY, s.lots, s.units), ("short", s.short_leg.contract, SELL, s.lots, s.units)]


def symbols(c):
    return {l.contract.symbol for l in legs_of(c)}


def symbol_fields(c):
    """(long_symbol, short_symbol): the bought and the sold contracts, '+'-joined and sorted when there are several."""
    longs = sorted(l.contract.symbol for l in legs_of(c) if l.is_long)
    shorts = sorted(l.contract.symbol for l in legs_of(c) if not l.is_long)
    return "+".join(longs), "+".join(shorts)


def split_symbols(*fields):
    """The individual contract symbols inside ledger / approval symbol fields."""
    out = set()
    for f in fields:
        out.update(x for x in (f or "").split("+") if x)
    return out


def gross_pnl(c, entry_fills, exit_fills):
    """Gross P&L in rupees from the entry and exit fills of every leg (before costs). Both are lists of LegFill."""
    entry = {f.leg_id: f for f in entry_fills}
    total = Decimal(0)
    for f in exit_fills:
        e = entry[f.leg_id]
        total += (f.fill_price - e.fill_price) * f.units if e.side == BUY else (e.fill_price - f.fill_price) * f.units
    return total
