"""Strategy (candidate selection) assumptions: one place, validated, fingerprinted.

These are OUR choices. Exchange facts, cost rules, risk limits and regime thresholds live elsewhere.
Nothing here refers to the risk limit: whether a candidate is affordable is the risk engine's call.
"""
import hashlib
import json
import os
from collections import namedtuple
from decimal import Decimal

from ..errors import ConfigError
from ..versioned import ROOT, load_json

DEFAULT_PATH = os.path.join(ROOT, "config", "strategy_thresholds.json")

FIELDS = ("min_days_to_expiry", "long_strike_offsets_steps", "width_points_options", "candidate_lots",
          "max_leg_spread_pct", "min_leg_volume", "min_leg_open_interest", "min_depth_lots",
          "max_debit_to_width", "max_spot_divergence_pct", "block_on_high_impact_event")
INT_FIELDS = ("min_days_to_expiry", "candidate_lots", "min_leg_volume", "min_leg_open_interest", "min_depth_lots")
NUMBER_FIELDS = ("max_leg_spread_pct", "max_debit_to_width", "max_spot_divergence_pct")

StrategyParams = namedtuple("StrategyParams", FIELDS + ("fingerprint",))


def _whole(v):
    return not isinstance(v, bool) and isinstance(v, int)


def build_params(values):
    unknown = sorted(set(values) - set(FIELDS))
    missing = sorted(set(FIELDS) - set(values))
    if unknown or missing:
        raise ConfigError("strategy thresholds: unknown %s, missing %s" % (unknown, missing))
    clean = {}
    for name in INT_FIELDS:
        v = values[name]
        minimum = 0 if name in ("min_leg_volume", "min_leg_open_interest") else 1
        if not _whole(v) or v < minimum:
            raise ConfigError("strategy threshold %s must be a whole number of at least %d, got %r" % (name, minimum, v))
        clean[name] = v
    for name in NUMBER_FIELDS:
        v = values[name]
        if isinstance(v, bool) or not isinstance(v, (int, Decimal)) or Decimal(v) <= 0:
            raise ConfigError("strategy threshold %s must be a number above zero, got %r" % (name, v))
        clean[name] = Decimal(v)
    v = values["block_on_high_impact_event"]
    if not isinstance(v, bool):
        raise ConfigError("block_on_high_impact_event must be true or false")
    clean["block_on_high_impact_event"] = v
    offsets = values["long_strike_offsets_steps"]
    if (not isinstance(offsets, (list, tuple)) or not offsets or not all(_whole(o) for o in offsets)
            or any(o < 0 for o in offsets) or len(set(offsets)) != len(offsets)):
        raise ConfigError("long_strike_offsets_steps must be a non-empty list of distinct whole numbers, 0 or more "
                          "(0 = nearest strike to spot, n = n strikes further out of the money)")
    widths = values["width_points_options"]
    if (not isinstance(widths, (list, tuple)) or not widths or not all(_whole(w) and w > 0 for w in widths)
            or len(set(widths)) != len(widths)):
        raise ConfigError("width_points_options must be a non-empty list of distinct whole numbers above zero")
    clean["long_strike_offsets_steps"] = tuple(sorted(offsets))
    clean["width_points_options"] = tuple(sorted(widths))
    if clean["max_debit_to_width"] >= 1:
        raise ConfigError("max_debit_to_width must be below 1 (a debit at or above the width cannot profit)")
    blob = json.dumps({k: (list(clean[k]) if isinstance(clean[k], tuple) else str(clean[k])) for k in FIELDS},
                      sort_keys=True, separators=(",", ":"))
    clean["fingerprint"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return StrategyParams(**clean)


def load_params(path=None):
    doc = load_json(path or DEFAULT_PATH, "strategy thresholds")
    if doc.get("schema_version") != 1 or doc.get("kind") != "STRATEGY_ASSUMPTIONS":
        raise ConfigError("strategy thresholds file must be schema_version 1, kind STRATEGY_ASSUMPTIONS")
    return build_params(doc.get("values", {}))


def params_as_json(p):
    """The thresholds as plain JSON values, for the journal."""
    out = {}
    for k in FIELDS:
        v = getattr(p, k)
        out[k] = list(v) if isinstance(v, tuple) else (str(v) if isinstance(v, Decimal) else v)
    out["fingerprint"] = p.fingerprint
    return out
