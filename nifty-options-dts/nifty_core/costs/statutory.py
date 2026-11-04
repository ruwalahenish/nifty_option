"""Statutory and exchange charges, independent of the broker."""
import os
from decimal import Decimal

from ..errors import ContractError
from ..versioned import ROOT, VersionedTable, load_json, load_sources
from .models import BUY, CRORE, SELL

DEFAULT_PATH = os.path.join(ROOT, "config", "costs", "statutory_charges.json")
ITEMS = ("stt_option_sell_premium_rate", "exchange_txn_charge_inr_per_crore_option_premium",
         "ipft_inr_per_crore_option_premium", "sebi_turnover_fee_inr_per_crore",
         "stamp_duty_option_buyer_rate", "gst_rate", "gst_base_components")


class StatutoryCharges(object):
    def __init__(self, doc, sources=None):
        self.table = VersionedTable(doc, sources if sources is not None else load_sources())
        for name in ITEMS:
            if name not in self.table.items:
                raise ContractError("statutory charges: missing item %s" % name)

    @classmethod
    def load(cls, path=None, sources_path=None):
        return cls(load_json(path or DEFAULT_PATH, "statutory charges"), load_sources(sources_path))

    def resolve_all(self, on, data_mode):
        """(resolved dict, problems list of (item, text)) for date `on`."""
        resolved, problems = {}, []
        for name in ITEMS:
            r, problem = self.table.check(name, on, data_mode)
            if r is None:
                problems.append((name, problem))
            else:
                resolved[name] = r
        return resolved, problems

    @staticmethod
    def compute(execution, rates):
        """Unrounded component amounts for one execution, given resolved rates.

        STT on SELL only; stamp duty on BUY only; exchange, IPFT and SEBI on both sides.
        GST is added by the engine, since its base includes the broker's brokerage.
        """
        turnover = execution.turnover
        out = {}
        out["stt"] = (turnover * Decimal(str(rates["stt_option_sell_premium_rate"].value))
                      if execution.side == SELL else Decimal(0))
        per_crore = lambda item: turnover * Decimal(str(rates[item].value)) / CRORE
        out["exchange_txn"] = per_crore("exchange_txn_charge_inr_per_crore_option_premium")
        out["ipft"] = per_crore("ipft_inr_per_crore_option_premium")
        out["sebi_fee"] = per_crore("sebi_turnover_fee_inr_per_crore")
        out["stamp_duty"] = (turnover * Decimal(str(rates["stamp_duty_option_buyer_rate"].value))
                             if execution.side == BUY else Decimal(0))
        return out
