"""Validated, immutable configuration. Paper-only: nothing here can enable live trading.

Money and percentages are Decimal. Unknown keys are rejected. Any key or environment
variable that tries to switch off paper mode, or that looks like a broker credential or
order setting, raises LiveTradingNotSupported.
"""
import datetime
import json
import os
from collections import namedtuple
from decimal import Decimal

from .errors import ConfigError, LiveTradingNotSupported

KNOWN_KEYS = (
    "account_capital_inr", "risk_per_trade_pct", "max_daily_loss_pct", "max_lots",
    "max_trades_per_day", "risk_basis", "slippage_points_per_leg", "entry_window_start_ist",
    "entry_window_end_ist", "flat_by_ist", "allow_averaging", "allow_overnight", "data_mode",
    "data_stale_after_seconds", "paper",
)

# Which kind of setting each key is. This file holds OUR choices only. Exchange facts (lot size,
# expiry rules, tick, strike interval, session hours) live in config/contracts/, and cost rules in
# config/costs/. A test fails if an exchange or cost fact appears here.
KEY_GROUPS = {
    "account_and_risk": ("account_capital_inr", "risk_per_trade_pct", "max_daily_loss_pct", "max_lots",
                         "max_trades_per_day", "risk_basis"),
    "strategy_assumptions": ("entry_window_start_ist", "entry_window_end_ist", "flat_by_ist",
                             "allow_averaging", "allow_overnight"),
    "cost_assumptions": ("slippage_points_per_leg",),
    "data_handling": ("data_mode", "data_stale_after_seconds"),
    "safety": ("paper",),
}

# A key containing any of these fragments looks like an attempt to go live or to hold
# broker access. Always refused.
LIVE_LOOKING_FRAGMENTS = ("live", "broker", "api_key", "apikey", "secret", "token", "password",
                          "client_id", "order", "real_money", "endpoint")

RISK_BASIS = "net_debit_plus_costs_plus_slippage"
DATA_MODES = ("fixture", "real_readonly")
# Latest time config may name. The real close for a date comes from the contract specs; use
# nifty_core.market.calendar_nse.check_config_fits_session to check a specific date.
HHMM_SESSION_CLOSE = (15, 40)

Config = namedtuple("Config", KNOWN_KEYS + ("risk_per_trade_inr", "max_daily_loss_inr"))

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "config.example.json")


def _hhmm(text, key):
    try:
        h, m = text.split(":")
        t = datetime.time(int(h), int(m))
    except (AttributeError, ValueError):
        raise ConfigError("%s must be 'HH:MM', got %r" % (key, text))
    return t


def _check_env(env):
    paper = env.get("NIFTY_PAPER")
    if paper is not None and paper.strip().lower() not in ("1", "true", "yes"):
        raise LiveTradingNotSupported("NIFTY_PAPER=%r: paper mode cannot be turned off in Version 1" % paper)
    for name, value in env.items():
        if name.upper().startswith("NIFTY_") and "LIVE" in name.upper() and str(value).strip().lower() not in ("", "0", "false", "no"):
            raise LiveTradingNotSupported("environment variable %s: live trading is not supported" % name)


def _positive_decimal(value, key, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ConfigError("%s must be a number" % key)
    d = Decimal(value)
    if d < 0 or (d == 0 and not allow_zero):
        raise ConfigError("%s must be %s" % (key, "zero or more" if allow_zero else "greater than zero"))
    return d


def validate(raw, env=None):
    """Return a Config from a dict parsed with Decimal floats, or raise."""
    _check_env(os.environ if env is None else env)
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    for key in raw:
        if any(frag in key.lower() for frag in LIVE_LOOKING_FRAGMENTS):
            raise LiveTradingNotSupported("config key %r is not allowed: Version 1 has no live or broker settings" % key)
    unknown = sorted(set(raw) - set(KNOWN_KEYS))
    if unknown:
        raise ConfigError("unknown config keys: %s" % ", ".join(unknown))
    missing = sorted(set(KNOWN_KEYS) - set(raw))
    if missing:
        raise ConfigError("missing config keys: %s" % ", ".join(missing))

    if raw["paper"] is not True:
        raise LiveTradingNotSupported("'paper' must be true: Version 1 is paper-only")
    if raw["allow_averaging"] is not False:
        raise ConfigError("allow_averaging must be false in Version 1")
    if raw["allow_overnight"] is not False:
        raise ConfigError("allow_overnight must be false in Version 1")
    if raw["risk_basis"] != RISK_BASIS:
        raise ConfigError("risk_basis must be %r (full net debit plus costs plus slippage)" % RISK_BASIS)
    if raw["data_mode"] not in DATA_MODES:
        raise ConfigError("data_mode must be one of %s" % (DATA_MODES,))

    capital = _positive_decimal(raw["account_capital_inr"], "account_capital_inr")
    risk_pct = _positive_decimal(raw["risk_per_trade_pct"], "risk_per_trade_pct")
    daily_pct = _positive_decimal(raw["max_daily_loss_pct"], "max_daily_loss_pct")
    if risk_pct > 100 or daily_pct > 100:
        raise ConfigError("percentages must be at most 100")
    if daily_pct < risk_pct:
        raise ConfigError("max_daily_loss_pct must be at least risk_per_trade_pct")
    slip = _positive_decimal(raw["slippage_points_per_leg"], "slippage_points_per_leg", allow_zero=True)
    for key in ("max_lots", "max_trades_per_day", "data_stale_after_seconds"):
        v = raw[key]
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ConfigError("%s must be a whole number of at least 1" % key)

    start = _hhmm(raw["entry_window_start_ist"], "entry_window_start_ist")
    end = _hhmm(raw["entry_window_end_ist"], "entry_window_end_ist")
    flat = _hhmm(raw["flat_by_ist"], "flat_by_ist")
    if not (datetime.time(9, 15) <= start < end <= flat <= datetime.time(*HHMM_SESSION_CLOSE)):
        raise ConfigError("need 09:15 <= entry start < entry end <= flat_by <= 15:40 (IST)")

    values = dict(raw)
    values["account_capital_inr"] = capital
    values["risk_per_trade_pct"] = risk_pct
    values["max_daily_loss_pct"] = daily_pct
    values["slippage_points_per_leg"] = slip
    values["risk_per_trade_inr"] = capital * risk_pct / Decimal(100)
    values["max_daily_loss_inr"] = capital * daily_pct / Decimal(100)
    return Config(**values)


def load_config(path=None, env=None):
    path = path or DEFAULT_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh, parse_float=Decimal)
    except (OSError, ValueError) as e:
        raise ConfigError("cannot read config %s: %s" % (path, e))
    return validate(raw, env)
