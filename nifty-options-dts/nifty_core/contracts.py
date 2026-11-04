"""NIFTY contract specifications, versioned by effective date and tracked to their sources.

Strategy, risk and execution code must obtain lot size, expiry rules, tick size, strike
interval and session times ONLY from here. Every lookup takes the date it is for and the
data mode. Outside "fixture" mode, UNVERIFIED values and values whose start date is unknown
(and the date asked for is earlier than the verification date) are refused.
"""
import datetime
import os

from .errors import ContractError
from .versioned import ROOT, VERIFIED_STATUSES, VersionedTable, load_json, load_sources

DEFAULT_PATH = os.path.join(ROOT, "config", "contracts", "nifty_contract_specs.json")
ITEMS = ("lot_size", "weekly_expiry_weekday", "expiry_holiday_rule", "tick_size",
         "strike_interval", "session")


def hhmm(text):
    h, m = text.split(":")
    return datetime.time(int(h), int(m))


class ContractSpecs(object):
    def __init__(self, doc, sources=None):
        if doc.get("schema_version") != 2:
            raise ContractError("unsupported contract spec schema_version: %r" % doc.get("schema_version"))
        if doc.get("underlying") != "NIFTY":
            raise ContractError("underlying must be NIFTY")
        self.table = VersionedTable(doc, sources if sources is not None else load_sources())
        for name in ITEMS:
            if name not in self.table.items:
                raise ContractError("missing contract item %s" % name)
        for v in self.table.items["lot_size"].versions:
            if not isinstance(v.value, int) or isinstance(v.value, bool) or v.value <= 0:
                raise ContractError("lot_size values must be positive integers")

    @classmethod
    def load(cls, path=None, sources_path=None):
        doc = load_json(path or DEFAULT_PATH, "contract specs")
        return cls(doc, load_sources(sources_path))

    # ---- lookups (each returns a versioned.Resolved)
    def lot_size(self, expiry, data_mode="fixture"):
        """Lot size for a contract that expires on `expiry` (datetime.date)."""
        return self.table.resolve("lot_size", expiry, data_mode)

    def expiry_weekday(self, nominal_expiry, data_mode="fixture"):
        """Weekday number (Monday=0) of the expiry rule in force for that nominal expiry date."""
        return self.table.resolve("weekly_expiry_weekday", nominal_expiry, data_mode)

    def expiry_holiday_rule(self, on, data_mode="fixture"):
        return self.table.resolve("expiry_holiday_rule", on, data_mode)

    def tick_size(self, on, data_mode="fixture"):
        return self.table.resolve("tick_size", on, data_mode)

    def strike_interval(self, on, data_mode="fixture"):
        return self.table.resolve("strike_interval", on, data_mode)

    def session_times(self, on, data_mode="fixture"):
        """(open, close, Resolved) as datetime.time values for the regular session on `on`."""
        r = self.table.resolve("session", on, data_mode)
        return hhmm(r.value["open"]), hhmm(r.value["close"]), r

    # ---- reporting
    def verification_report(self, on, data_mode="fixture"):
        """One row per item for date `on`: (name, status or None, problem or None, warnings)."""
        rows = []
        for name in ITEMS:
            r, problem = self.table.check(name, on, data_mode)
            rows.append((name, r.status if r else None, problem, r.warnings if r else ()))
        return rows

    def unverified_versions(self):
        out = []
        for name, item in self.table.items.items():
            for v in item.versions:
                if v.status not in VERIFIED_STATUSES:
                    out.append("%s(%s)" % (name, v.value))
        return out
