"""Shared helpers: load the contract layer, calendar and the generated scenario fixtures."""
import os

from nifty_core import config as config_mod
from nifty_core.contracts import ContractSpecs
from nifty_core.data.fixture_provider import FixtureProvider, load_scenario_file
from nifty_core.market.calendar_nse import NseCalendar

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCEN_DIR = os.path.join(ROOT, "tests", "fixtures", "scenarios")
INVALID_DIR = os.path.join(ROOT, "tests", "fixtures", "invalid")
NAMES = ("bullish_trend", "bearish_trend", "sideways", "high_volatility", "illiquid_options",
         "missing_invalid_data")

_cache = {}


def specs():
    if "specs" not in _cache:
        _cache["specs"] = ContractSpecs.load()
    return _cache["specs"]


def calendar(mode="fixture"):
    key = "cal-" + mode
    if key not in _cache:
        _cache[key] = NseCalendar.load(specs=specs(), data_mode=mode)
    return _cache[key]


def config():
    return config_mod.load_config(env={})


def scenario(name):
    if name not in _cache:
        _cache[name] = load_scenario_file(os.path.join(SCEN_DIR, name + ".json"), specs(), calendar())
    return _cache[name]


def provider(name, clock=None):
    return FixtureProvider(scenario(name), clock)
