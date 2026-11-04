"""Broker-specific brokerage. Adding a broker means adding a JSON file here, not changing logic.

Supported schemes:
    flat_per_executed_order   {"scheme": ..., "amount_inr": N}
    lower_of_pct_or_flat      {"scheme": ..., "pct": 0.0003, "cap_inr": 20}
    zero                      {"scheme": "zero"}
"""
import os
from decimal import Decimal

from ..errors import ContractError
from ..versioned import ROOT, VersionedTable, load_json, load_sources

BROKER_DIR = os.path.join(ROOT, "config", "costs", "brokers")
ITEMS = ("option_brokerage", "debit_balance_surcharge")


class BrokerProfile(object):
    def __init__(self, doc, sources=None):
        self.broker = doc.get("broker")
        if not self.broker:
            raise ContractError("broker profile needs a 'broker' id")
        self.display_name = doc.get("display_name", self.broker)
        self.model_assumptions = tuple(doc.get("model_assumptions", ()))
        self.table = VersionedTable(doc, sources if sources is not None else load_sources())
        if "option_brokerage" not in self.table.items:
            raise ContractError("broker %s: missing option_brokerage" % self.broker)

    @classmethod
    def load(cls, broker_or_path, sources_path=None):
        path = broker_or_path
        if not os.path.sep in path and not path.endswith(".json"):
            path = os.path.join(BROKER_DIR, broker_or_path + ".json")
        return cls(load_json(path, "broker profile"), load_sources(sources_path))

    def resolve_all(self, on, data_mode, account_in_debit=False):
        resolved, problems = {}, []
        names = ["option_brokerage"] + (["debit_balance_surcharge"] if account_in_debit else [])
        for name in names:
            r, problem = self.table.check(name, on, data_mode)
            if r is None:
                problems.append(("%s:%s" % (self.broker, name), problem))
            else:
                resolved[name] = r
        return resolved, problems

    @staticmethod
    def brokerage(execution, resolved, account_in_debit=False):
        spec = resolved["option_brokerage"].value
        scheme = spec.get("scheme")
        if scheme == "flat_per_executed_order":
            fee = Decimal(str(spec["amount_inr"]))
        elif scheme == "lower_of_pct_or_flat":
            fee = min(execution.turnover * Decimal(str(spec["pct"])), Decimal(str(spec["cap_inr"])))
        elif scheme == "zero":
            fee = Decimal(0)
        else:
            raise ContractError("unknown brokerage scheme %r" % (scheme,))
        if account_in_debit:
            fee += Decimal(str(resolved["debit_balance_surcharge"].value["amount_inr"]))
        return fee
