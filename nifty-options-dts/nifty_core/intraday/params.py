"""Intraday setup thresholds: one place, validated, fingerprinted. OUR choices, not exchange facts, not validated on data."""
import hashlib
import json
from collections import namedtuple
from decimal import Decimal

from ..contracts import hhmm
from ..errors import ConfigError
from ..versioned import load_json

FIELDS = ("opening_range_candles", "min_range_pct", "breakout_buffer_pct", "max_extension_pct", "require_retest",
          "retest_zone_pct", "max_breakout_age_candles", "require_resumption_candle", "require_day_average_side",
          "latest_setup_ist")
INT_FIELDS = ("opening_range_candles", "max_breakout_age_candles")
PCT_FIELDS = ("min_range_pct", "breakout_buffer_pct", "max_extension_pct", "retest_zone_pct")
BOOL_FIELDS = ("require_retest", "require_resumption_candle", "require_day_average_side")
SetupParams = namedtuple("SetupParams", FIELDS + ("fingerprint",))


def build_params(values):
    unknown = sorted(set(values) - set(FIELDS))
    missing = sorted(set(FIELDS) - set(values))
    if unknown or missing:
        raise ConfigError("intraday setup thresholds: unknown %s, missing %s" % (unknown, missing))
    clean = {}
    for name in INT_FIELDS:
        v = values[name]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ConfigError("intraday setup threshold %s must be a whole number of at least 1, got %r" % (name, v))
        clean[name] = v
    if clean["opening_range_candles"] < 2:
        raise ConfigError("opening_range_candles must be at least 2")
    for name in PCT_FIELDS:
        v = values[name]
        if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
            raise ConfigError("intraday setup threshold %s must be a number, got %r" % (name, v))
        v = Decimal(v)
        if v < 0 or v > 5:
            raise ConfigError("intraday setup threshold %s must be between 0 and 5 (percent), got %s" % (name, v))
        clean[name] = v
    for name in BOOL_FIELDS:
        if not isinstance(values[name], bool):
            raise ConfigError("intraday setup threshold %s must be true or false" % name)
        clean[name] = values[name]
    try:
        hhmm(values["latest_setup_ist"])
    except (ValueError, AttributeError):
        raise ConfigError("latest_setup_ist must look like 13:30")
    clean["latest_setup_ist"] = values["latest_setup_ist"]
    blob = json.dumps({k: str(clean[k]) for k in FIELDS}, sort_keys=True, separators=(",", ":"))
    clean["fingerprint"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return SetupParams(**clean)


def load_params(path):
    doc = load_json(path, "intraday setup thresholds")
    if doc.get("schema_version") != 1 or doc.get("kind") != "STRATEGY_ASSUMPTIONS":
        raise ConfigError("intraday setup thresholds must be schema_version 1, kind STRATEGY_ASSUMPTIONS")
    return build_params(doc.get("values", {}))
