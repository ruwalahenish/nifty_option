"""Position-management assumptions: one place, validated, fingerprinted. OUR choices, not exchange facts or risk limits.

A stop-loss defined here is a trade-management exit. It never feeds the risk gate: the risk engine does not import
this module, and Total Defined Risk is never reduced by any stop.
"""
import hashlib
import json
import os
from collections import namedtuple
from decimal import Decimal

from ..errors import ConfigError
from ..versioned import ROOT, load_json

DEFAULT_PATH = os.path.join(ROOT, "config", "position_thresholds.json")
FIELDS = ("profit_target_pct_of_defined_risk", "stop_loss_pct_of_defined_risk", "invalidate_on_opposite_regime",
          "invalidate_on_high_volatility", "data_safety_failed_checks")
PositionParams = namedtuple("PositionParams", FIELDS + ("fingerprint",))


def build_params(values):
    unknown = sorted(set(values) - set(FIELDS))
    missing = sorted(set(FIELDS) - set(values))
    if unknown or missing:
        raise ConfigError("position thresholds: unknown %s, missing %s" % (unknown, missing))
    clean = {}
    for name in ("profit_target_pct_of_defined_risk", "stop_loss_pct_of_defined_risk"):
        v = values[name]
        if isinstance(v, bool) or not isinstance(v, (int, Decimal)) or Decimal(v) <= 0:
            raise ConfigError("%s must be a number above zero, got %r" % (name, v))
        clean[name] = Decimal(v)
    if clean["stop_loss_pct_of_defined_risk"] > 100:
        raise ConfigError("stop_loss_pct_of_defined_risk cannot exceed 100 (the defined risk is the worst case)")
    for name in ("invalidate_on_opposite_regime", "invalidate_on_high_volatility"):
        if not isinstance(values[name], bool):
            raise ConfigError("%s must be true or false" % name)
        clean[name] = values[name]
    n = values["data_safety_failed_checks"]
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ConfigError("data_safety_failed_checks must be a whole number of at least 1")
    clean["data_safety_failed_checks"] = n
    blob = json.dumps({k: str(clean[k]) for k in FIELDS}, sort_keys=True, separators=(",", ":"))
    clean["fingerprint"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return PositionParams(**clean)


def load_params(path=None):
    doc = load_json(path or DEFAULT_PATH, "position thresholds")
    if doc.get("schema_version") != 1 or doc.get("kind") != "STRATEGY_ASSUMPTIONS":
        raise ConfigError("position thresholds file must be schema_version 1, kind STRATEGY_ASSUMPTIONS")
    return build_params(doc.get("values", {}))
