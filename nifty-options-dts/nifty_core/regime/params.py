"""Regime thresholds: one place, validated, with a fingerprint the journal can record.

These are STRATEGY ASSUMPTIONS (our choices). They are separate from verified exchange facts
(config/contracts) and from market data. Changing a value changes the fingerprint.
"""
import hashlib
import json
import os
from collections import namedtuple
from decimal import Decimal

from ..errors import ConfigError
from ..versioned import ROOT, load_json

DEFAULT_PATH = os.path.join(ROOT, "config", "regime_thresholds.json")

FIELDS = ("interval_minutes", "min_bars", "max_data_lag_minutes", "net_move_pct", "range_position_high",
          "range_position_low", "recent_window", "recent_dominance", "structure_window", "votes_required",
          "sideways_max_range_pct", "abnormal_bar_range_pct", "high_vol_stdev_pct")
INT_FIELDS = ("interval_minutes", "min_bars", "max_data_lag_minutes", "recent_window", "recent_dominance",
              "structure_window", "votes_required")
NUMBER_FIELDS = tuple(f for f in FIELDS if f not in INT_FIELDS)
N_EVIDENCE = 4          # net move, range position, recent candles, swing structure

RegimeParams = namedtuple("RegimeParams", FIELDS + ("fingerprint",))


def build_params(values):
    """Validate a dict of threshold values and return RegimeParams. Raises ConfigError."""
    unknown = sorted(set(values) - set(FIELDS))
    missing = sorted(set(FIELDS) - set(values))
    if unknown or missing:
        raise ConfigError("regime thresholds: unknown %s, missing %s" % (unknown, missing))
    clean = {}
    for name in INT_FIELDS:
        v = values[name]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ConfigError("regime threshold %s must be a whole number of at least 1, got %r" % (name, v))
        clean[name] = v
    for name in NUMBER_FIELDS:
        v = values[name]
        if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
            raise ConfigError("regime threshold %s must be a number (Decimal), got %r" % (name, v))
        v = Decimal(v)
        if v <= 0:
            raise ConfigError("regime threshold %s must be above zero, got %s" % (name, v))
        clean[name] = v
    if clean["interval_minutes"] != 5:
        raise ConfigError("Version 1 supports 5-minute candles only")
    if clean["structure_window"] % 3 != 0 or clean["structure_window"] < 6:
        raise ConfigError("structure_window must be a multiple of 3 and at least 6 (three equal blocks)")
    if clean["recent_dominance"] > clean["recent_window"] or clean["recent_dominance"] * 2 <= clean["recent_window"]:
        raise ConfigError("recent_dominance must be a strict majority of recent_window")
    if not (0 < clean["range_position_low"] < clean["range_position_high"] < 1):
        raise ConfigError("need 0 < range_position_low < range_position_high < 1")
    if not 1 <= clean["votes_required"] <= N_EVIDENCE:
        raise ConfigError("votes_required must be between 1 and %d" % N_EVIDENCE)
    if clean["votes_required"] * 2 <= N_EVIDENCE:
        raise ConfigError("votes_required must be a strict majority of the %d evidence items" % N_EVIDENCE)
    if clean["min_bars"] < max(clean["structure_window"], clean["recent_window"]):
        raise ConfigError("min_bars must cover structure_window and recent_window")
    if clean["sideways_max_range_pct"] <= 0 or clean["abnormal_bar_range_pct"] <= 0:
        raise ConfigError("range thresholds must be above zero")
    blob = json.dumps({k: str(clean[k]) for k in FIELDS}, sort_keys=True, separators=(",", ":"))
    clean["fingerprint"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return RegimeParams(**clean)


def load_params(path=None):
    doc = load_json(path or DEFAULT_PATH, "regime thresholds")
    if doc.get("schema_version") != 1 or doc.get("kind") != "STRATEGY_ASSUMPTIONS":
        raise ConfigError("regime thresholds file must be schema_version 1, kind STRATEGY_ASSUMPTIONS")
    return build_params(doc.get("values", {}))
