"""Selector thresholds: one place, validated, fingerprinted. OUR choices, not exchange facts, not validated on data."""
import hashlib
import json
import os
from collections import namedtuple
from decimal import Decimal

from ..contracts import hhmm
from ..errors import ConfigError
from ..versioned import ROOT, load_json

DEFAULT_PATH = os.path.join(ROOT, "config", "selector_thresholds.json")

INT_FIELDS = ("min_candles", "momentum_window", "min_confidence")
NUMBER_FIELDS = (
    "trend_net_move_pct", "strong_net_move_pct", "trend_efficiency_min", "strong_efficiency_min",
    "range_efficiency_max", "range_net_move_max_pct", "range_day_range_max_pct", "fly_efficiency_max",
    "fly_day_range_max_pct", "fly_pin_band_pct", "momentum_min_pct", "momentum_flat_max_pct", "vol_low_stdev_pct",
    "vol_high_stdev_pct", "vol_expansion_ratio", "level_buffer_pct", "room_min_pct", "credit_short_distance_pct",
    "condor_short_distance_pct", "min_credit_to_width_spread", "min_credit_to_width_condor",
    "min_credit_to_width_fly", "min_rr_debit", "debit_target_pct_of_risk", "debit_stop_pct_of_risk",
    "credit_target_pct_of_credit", "credit_stop_pct_of_credit", "credit_stop_max_pct_of_max_loss", "reeval_spot_move_pct", "range_mid_low", "range_mid_high")
LIST_FIELDS = ("credit_width_options", "condor_width_options", "fly_width_options")
TIME_FIELDS = ("latest_entry_debit_ist", "latest_entry_credit_ist")
FIELDS = INT_FIELDS + NUMBER_FIELDS + LIST_FIELDS + TIME_FIELDS
SelectorParams = namedtuple("SelectorParams", FIELDS + ("fingerprint",))


def _whole(v):
    return not isinstance(v, bool) and isinstance(v, int)


def build_params(values):
    unknown, missing = sorted(set(values) - set(FIELDS)), sorted(set(FIELDS) - set(values))
    if unknown or missing:
        raise ConfigError("selector thresholds: unknown %s, missing %s" % (unknown, missing))
    clean = {}
    for name in INT_FIELDS:
        v = values[name]
        if not _whole(v) or v < 1 or (name == "min_confidence" and v > 100):
            raise ConfigError("selector threshold %s must be a whole number of at least 1 (confidence at most 100), "
                              "got %r" % (name, v))
        clean[name] = v
    for name in NUMBER_FIELDS:
        v = values[name]
        if isinstance(v, bool) or not isinstance(v, (int, Decimal)) or Decimal(v) <= 0:
            raise ConfigError("selector threshold %s must be a number above zero, got %r" % (name, v))
        clean[name] = Decimal(v)
    for name in LIST_FIELDS:
        v = values[name]
        if (not isinstance(v, (list, tuple)) or not v or not all(_whole(w) and w > 0 for w in v)
                or len(set(v)) != len(v)):
            raise ConfigError("selector threshold %s must be a non-empty list of distinct whole numbers above zero" % name)
        clean[name] = tuple(sorted(v))
    for name in TIME_FIELDS:
        try:
            hhmm(values[name])
        except (ValueError, AttributeError, TypeError):
            raise ConfigError("selector threshold %s must look like 13:30" % name)
        clean[name] = values[name]
    if clean["strong_net_move_pct"] < clean["trend_net_move_pct"] or \
            clean["strong_efficiency_min"] < clean["trend_efficiency_min"]:
        raise ConfigError("the 'strong' trend thresholds must be at least the plain trend thresholds")
    if clean["range_efficiency_max"] >= clean["trend_efficiency_min"]:
        raise ConfigError("range_efficiency_max must be below trend_efficiency_min (a day cannot be both)")
    if clean["momentum_flat_max_pct"] >= clean["momentum_min_pct"]:
        raise ConfigError("momentum_flat_max_pct must be below momentum_min_pct")
    if clean["vol_low_stdev_pct"] >= clean["vol_high_stdev_pct"]:
        raise ConfigError("vol_low_stdev_pct must be below vol_high_stdev_pct")
    if clean["credit_stop_max_pct_of_max_loss"] > 100:
        raise ConfigError("credit_stop_max_pct_of_max_loss is a percentage of the worst case: at most 100")
    if not clean["range_mid_low"] < clean["range_mid_high"] < 1:
        raise ConfigError("need 0 < range_mid_low < range_mid_high < 1")
    for name in ("min_credit_to_width_spread", "min_credit_to_width_condor", "min_credit_to_width_fly"):
        if clean[name] >= 1:
            raise ConfigError("%s must be below 1 (a credit cannot reach the width)" % name)
    blob = json.dumps({k: (list(clean[k]) if isinstance(clean[k], tuple) else str(clean[k])) for k in FIELDS},
                      sort_keys=True, separators=(",", ":"))
    clean["fingerprint"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return SelectorParams(**clean)


def load_params(path=None):
    doc = load_json(path or DEFAULT_PATH, "selector thresholds")
    if doc.get("schema_version") != 1 or doc.get("kind") != "STRATEGY_ASSUMPTIONS":
        raise ConfigError("selector thresholds must be schema_version 1, kind STRATEGY_ASSUMPTIONS")
    return build_params(doc.get("values", {}))
