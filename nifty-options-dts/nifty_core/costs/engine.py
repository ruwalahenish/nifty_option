"""Cost engine: per-leg costs, entry / exit / round-trip estimates, actual costs from fills,
and the defined-risk figure for a debit spread.

A spread is always costed as separate leg executions. There is no flat cost per spread.
Outside fixture mode, any cost rate that is missing, unverified, or not valid for the trade
date makes the engine raise CostRateUnverified (listing every problem) instead of guessing.
"""
import datetime
from collections import OrderedDict, namedtuple
from decimal import Decimal

from ..errors import CostRateUnverified
from . import brokers as brokers_mod
from . import statutory as statutory_mod
from .models import (BUY, COMPONENTS, SELL, DefinedRisk, LegCosts, LegExecution, PhaseCosts,
                     RoundTripCosts, paise)

QuotedLeg = namedtuple("QuotedLeg", "leg_id side quoted_price units")
ZERO = Decimal(0)


def slipped_price(price, side, slippage_points, tick):
    """Adverse fill: a buyer pays more, a seller receives less (never below one tick)."""
    price, slip, tick = Decimal(price), Decimal(slippage_points), Decimal(tick)
    if side == BUY:
        return price + slip
    return max(price - slip, tick)


class CostEngine(object):
    def __init__(self, statutory, broker, data_mode="fixture", account_in_debit=False):
        self.statutory = statutory
        self.broker = broker
        self.data_mode = data_mode
        self.account_in_debit = account_in_debit

    # ---- rate resolution and the verification gate
    def _rates(self, on):
        srates, sproblems = self.statutory.resolve_all(on, self.data_mode)
        brates, bproblems = self.broker.resolve_all(on, self.data_mode, self.account_in_debit)
        problems = sproblems + bproblems
        if problems:
            names = ", ".join(p[0] for p in problems)
            raise CostRateUnverified(
                "cost rates unusable on %s in data_mode=%r: %s. %s" % (
                    on, self.data_mode, names, "; ".join("%s: %s" % p for p in problems)),
                missing=[p[0] for p in problems])
        return srates, brates

    def verification_report(self, on):
        """Rows (item, status, problem, warnings) for every rate needed on date `on`."""
        rows = []
        for name in statutory_mod.ITEMS:
            r, problem = self.statutory.table.check(name, on, self.data_mode)
            rows.append((name, r.status if r else None, problem, r.warnings if r else ()))
        names = ["option_brokerage"] + (["debit_balance_surcharge"] if self.account_in_debit else [])
        for name in names:
            r, problem = self.broker.table.check(name, on, self.data_mode)
            rows.append(("%s:%s" % (self.broker.broker, name), r.status if r else None, problem,
                         r.warnings if r else ()))
        return rows

    def assert_ready(self, on):
        """Raise CostRateUnverified unless every needed rate can be used on `on` in this mode."""
        self._rates(on)

    # ---- costs
    def leg_costs(self, execution):
        srates, brates = self._rates(execution.trade_date)
        raw = self.statutory.compute(execution, srates)
        raw["brokerage"] = self.broker.brokerage(execution, brates, self.account_in_debit)
        parts = OrderedDict((name, paise(raw.get(name, ZERO))) for name in COMPONENTS if name != "gst")
        base = sum((parts[c] for c in srates["gst_base_components"].value), ZERO)
        parts["gst"] = paise(base * Decimal(str(srates["gst_rate"].value)))
        ordered = OrderedDict((name, parts[name]) for name in COMPONENTS)
        warnings = []
        versions = {}
        for r in list(srates.values()) + list(brates.values()):
            warnings.extend(r.warnings)
            versions[r.name] = (r.effective_from, r.verified_on, r.sources)
        return LegCosts(execution, ordered, sum(ordered.values(), ZERO), tuple(dict.fromkeys(warnings)), versions)

    def phase_costs(self, executions, label="estimate"):
        legs = [self.leg_costs(e) for e in executions]
        comps = OrderedDict((name, sum((l.components[name] for l in legs), ZERO)) for name in COMPONENTS)
        warnings = tuple(dict.fromkeys(w for l in legs for w in l.warnings))
        return PhaseCosts(label, tuple(legs), comps, sum(comps.values(), ZERO), warnings)

    def entry_costs(self, executions):
        return self.phase_costs(executions, "entry estimate")

    def exit_costs(self, executions):
        return self.phase_costs(executions, "exit estimate")

    def round_trip(self, entry_executions, exit_executions):
        entry, exit_ = self.entry_costs(entry_executions), self.exit_costs(exit_executions)
        return RoundTripCosts(entry, exit_, entry.total + exit_.total)

    def actual_costs(self, fills):
        """Costs of the paper fills actually made (each fill is a LegExecution at its fill price)."""
        return self.phase_costs(fills, "actual (paper fills)")

    # ---- defined risk of a debit spread
    def defined_risk(self, quoted_legs, slippage_points, trade_date, tick, exit_method="entry_price"):
        """Capital at risk for a debit spread opened on `trade_date`.

        quoted_legs      QuotedLeg for each leg at its QUOTED price (bid for a sell, ask for a buy)
        slippage_points  configured adverse slippage per leg execution
        exit_method      how the closing legs are priced for the exit-cost estimate:
                         "entry_price"  each leg closes at its entry quote (conservative: keeps
                                        STT on the long leg's sale at its entry premium)
                         "floor_tick"   each leg closes at one tick (a spread that expired worthless)
        """
        entry, closing = [], []
        quoted_debit = paid_debit = ZERO
        for q in quoted_legs:
            fill = slipped_price(q.quoted_price, q.side, slippage_points, tick)
            entry.append(LegExecution(q.leg_id, q.side, fill, q.units, trade_date))
            sign = 1 if q.side == BUY else -1
            quoted_debit += sign * Decimal(q.quoted_price) * q.units
            paid_debit += sign * fill * q.units
            close_price = Decimal(q.quoted_price) if exit_method == "entry_price" else Decimal(tick)
            if exit_method not in ("entry_price", "floor_tick"):
                raise ValueError("exit_method must be 'entry_price' or 'floor_tick'")
            closing.append(LegExecution(q.leg_id, SELL if q.side == BUY else BUY, close_price, q.units,
                                        trade_date))
        entry_costs = self.entry_costs(entry)
        exit_costs = self.exit_costs(closing)
        slippage_inr = paid_debit - quoted_debit
        entry_risk = paid_debit + entry_costs.total
        warnings = tuple(dict.fromkeys(entry_costs.warnings + exit_costs.warnings))
        return DefinedRisk(
            net_debit_quoted_inr=paise(quoted_debit), entry_slippage_inr=paise(slippage_inr),
            net_debit_paid_inr=paise(paid_debit), entry_costs=entry_costs,
            entry_risk_inr=paise(entry_risk), estimated_exit_costs=exit_costs,
            round_trip_costs_inr=paise(entry_costs.total + exit_costs.total),
            total_defined_risk_inr=paise(entry_risk + exit_costs.total),
            exit_estimate_method=exit_method, warnings=warnings)


def load_cost_engine(broker="zerodha", data_mode="fixture", account_in_debit=False,
                     statutory_path=None, broker_path=None, sources_path=None):
    """Build a CostEngine from the versioned data files."""
    statutory = statutory_mod.StatutoryCharges.load(statutory_path, sources_path)
    profile = brokers_mod.BrokerProfile.load(broker_path or broker, sources_path)
    return CostEngine(statutory, profile, data_mode, account_in_debit)
