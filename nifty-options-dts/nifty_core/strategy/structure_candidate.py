"""A trade candidate for the CREDIT structures the adaptive selector can propose: bull put / bear call credit spread,
iron condor and iron butterfly. It is the multi-leg counterpart of `TradeCandidate` (which carries a `DebitSpread`):
the risk gate, the paper executor and the position manager read either one through `nifty_core.legs`.

A candidate is a PROPOSAL. It has not been sized, risk-checked or approved, and no order or simulated fill exists.
`exit_plan` holds the exit levels fixed at decision time (profit target, stop, and the price level that invalidates the
structure), so every trade has its entry, stop-loss, target and maximum risk written down before it is entered.
"""
from collections import namedtuple

StructureCandidate = namedtuple("StructureCandidate", (
    "strategy_type regime_at_entry decision_time chain_snapshot_time data_label data_mode expiry structure lots lot_size "
    "net_credit_points net_credit_inr width_points liquidity selection assumptions exit_plan note"))


def to_journal(c):
    """Plain JSON-friendly record. Its SHA-256 is the candidate's identity, so every field that matters is in it."""
    return {
        "strategy_type": c.strategy_type, "regime_at_entry": c.regime_at_entry,
        "decision_time": c.decision_time.isoformat(), "chain_snapshot_time": c.chain_snapshot_time.isoformat(),
        "data_label": c.data_label, "data_mode": c.data_mode, "expiry": c.expiry.isoformat(),
        "legs": [{"contract": l.contract.symbol, "side": l.side, "strike": str(l.strike), "type": l.option_type,
                  "entry_price": str(l.entry_price)} for l in getattr(c.structure, "legs", ())],
        "lots": c.lots, "lot_size": c.lot_size, "net_credit_points": str(c.net_credit_points),
        "net_credit_inr": str(c.net_credit_inr), "width_points": str(c.width_points), "liquidity": c.liquidity,
        "selection": c.selection, "assumptions": c.assumptions, "exit_plan": c.exit_plan, "note": c.note,
    }
