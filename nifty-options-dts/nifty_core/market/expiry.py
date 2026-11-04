"""NIFTY weekly and monthly expiry dates, with the NSE holiday rule.

The weekday of the rule comes from the contract specs (looked up by the nominal expiry date,
so the Thursday-to-Tuesday change of 2025 is handled by data, not code). If the nominal day
is a trading holiday the expiry moves to the previous trading day (NSE contract specification).
"""
import datetime
from collections import namedtuple

from ..errors import ContractError, RateNotCovered

Expiry = namedtuple("Expiry", "date nominal shifted kind warnings")

_ONE = datetime.timedelta(days=1)


def _rule_weekday(specs, nominal, data_mode):
    r = specs.expiry_weekday(nominal, data_mode)
    return r.value, r


def _apply_holiday_rule(calendar, specs, nominal, data_mode):
    rule = specs.expiry_holiday_rule(nominal, data_mode)
    if rule.value != "PREVIOUS_TRADING_DAY":
        raise ContractError("unsupported expiry holiday rule %r" % (rule.value,))
    d = nominal
    for _ in range(10):
        if calendar.is_trading_day(d):
            return d, rule.warnings
        d -= _ONE
    raise ContractError("no trading day within 10 days before nominal expiry %s" % nominal)


def weekly_expiry(day, calendar, specs, data_mode="fixture"):
    """The weekly expiry of the calendar week (Monday to Sunday) that contains `day`.

    Raises RateNotCovered for a week with no weekly expiry (the 2025 rule-change gap).
    """
    monday = day - datetime.timedelta(days=day.weekday())
    last_error = None
    for weekday in (1, 3):                                   # the two rules NSE has used
        nominal = monday + datetime.timedelta(days=weekday)
        try:
            value, resolved = _rule_weekday(specs, nominal, data_mode)
        except RateNotCovered as e:
            last_error = e
            continue
        if value == weekday:
            actual, warns = _apply_holiday_rule(calendar, specs, nominal, data_mode)
            return Expiry(actual, nominal, actual != nominal, "WEEKLY",
                          tuple(resolved.warnings) + tuple(warns))
    raise RateNotCovered("no weekly expiry in the week of %s (%s)" % (monday, last_error))


def monthly_expiry(year, month, calendar, specs, data_mode="fixture"):
    """The monthly expiry: the last rule-weekday of the month, moved for holidays."""
    first_next = datetime.date(year + (month == 12), month % 12 + 1, 1)
    last_day = first_next - _ONE
    for weekday in (1, 3):
        nominal = last_day - datetime.timedelta(days=(last_day.weekday() - weekday) % 7)
        try:
            value, resolved = _rule_weekday(specs, nominal, data_mode)
        except RateNotCovered:
            continue
        if value == weekday:
            actual, warns = _apply_holiday_rule(calendar, specs, nominal, data_mode)
            return Expiry(actual, nominal, actual != nominal, "MONTHLY",
                          tuple(resolved.warnings) + tuple(warns))
    raise RateNotCovered("no monthly expiry rule covers %04d-%02d" % (year, month))


def next_weekly_expiry(on, calendar, specs, data_mode="fixture", max_weeks=8):
    """Earliest weekly expiry on or after `on` (the expiry day itself counts)."""
    monday = on - datetime.timedelta(days=on.weekday())
    for i in range(max_weeks):
        try:
            e = weekly_expiry(monday + datetime.timedelta(weeks=i), calendar, specs, data_mode)
        except RateNotCovered:
            continue
        if e.date >= on:
            return e
    raise RateNotCovered("no weekly expiry found within %d weeks of %s" % (max_weeks, on))


def is_expiry_day(day, calendar, specs, data_mode="fixture"):
    """True if `day` is a weekly or monthly NIFTY expiry day."""
    try:
        if weekly_expiry(day, calendar, specs, data_mode).date == day:
            return True
    except RateNotCovered:
        pass
    # a holiday shift can pull the previous week's expiry into this week's Monday..., and the
    # next week's expiry can land on a Friday/Monday of this one: check the neighbours too
    for delta in (-7, 7):
        try:
            if weekly_expiry(day + datetime.timedelta(days=delta), calendar, specs, data_mode).date == day:
                return True
        except RateNotCovered:
            continue
    try:
        return monthly_expiry(day.year, day.month, calendar, specs, data_mode).date == day
    except RateNotCovered:
        return False
