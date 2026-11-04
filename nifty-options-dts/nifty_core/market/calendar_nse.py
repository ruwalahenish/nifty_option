"""NSE F&O trading calendar and sessions.

A calendar year must be listed in config/market/nse_calendar.json. For a year that is not
listed: in fixture mode the calendar falls back to "weekdays are trading days" and says so in
the result's warnings; in any other mode it raises CalendarNotCovered. Nothing is guessed
silently.
"""
import datetime
import os
from collections import namedtuple

from .. import contracts as contracts_mod
from ..errors import CalendarNotCovered, ConfigError
from ..versioned import ROOT, VERIFIED_STATUSES, load_json, load_sources, parse_date

DEFAULT_PATH = os.path.join(ROOT, "config", "market", "nse_calendar.json")

Session = namedtuple("Session", "date kind pre_open_start open close warnings assumed")


class NseCalendar(object):
    def __init__(self, doc, specs=None, sources=None, data_mode="fixture"):
        if doc.get("schema_version") != 1:
            raise CalendarNotCovered("unsupported calendar schema_version")
        self.data_mode = data_mode
        self.specs = specs if specs is not None else contracts_mod.ContractSpecs.load()
        sources = sources if sources is not None else load_sources()
        self._years = {}
        for year, cal in doc.get("calendars", {}).items():
            status = cal.get("status")
            if status in VERIFIED_STATUSES:
                if not cal.get("sources") or not cal.get("verified_on"):
                    raise CalendarNotCovered("calendar %s: verified status needs sources and verified_on" % year)
                for sid in cal["sources"]:
                    if sid not in sources:
                        raise CalendarNotCovered("calendar %s: unknown source %s" % (year, sid))
            holidays = {}
            for h in cal.get("trading_holidays", []) + cal.get("weekend_holidays", []):
                d = parse_date(h["date"], "holiday date")
                if d.year != int(year):
                    raise CalendarNotCovered("holiday %s is outside calendar year %s" % (d, year))
                holidays[d] = h["description"]
            specials = {}
            for s in cal.get("special_sessions", []):
                d = parse_date(s["date"], "special session date")
                if d.year != int(year):
                    raise CalendarNotCovered("special session %s is outside calendar year %s" % (d, year))
                specials[d] = s
            self._years[int(year)] = {"status": status, "holidays": holidays, "specials": specials,
                                      "verified_on": cal.get("verified_on")}

    @classmethod
    def load(cls, path=None, specs=None, sources_path=None, data_mode="fixture"):
        return cls(load_json(path or DEFAULT_PATH, "NSE calendar"), specs, load_sources(sources_path), data_mode)

    # ---- coverage
    def covered(self, day):
        y = self._years.get(day.year)
        return bool(y) and y["status"] in VERIFIED_STATUSES

    def _year(self, day):
        y = self._years.get(day.year)
        if y is not None and y["status"] in VERIFIED_STATUSES:
            return y, False
        if self.data_mode != "fixture":
            raise CalendarNotCovered("no verified NSE calendar for %d (data_mode=%r); add it from the NSE "
                                     "trading-holiday circular" % (day.year, self.data_mode))
        return None, True

    # ---- queries
    def holiday_reason(self, day):
        """Reason the exchange is closed on `day`, or None if it is open (special sessions are open)."""
        y, assumed = self._year(day)
        if y is not None and day in y["specials"]:
            return None
        if day.weekday() >= 5:
            return "weekend"
        if y is not None and day in y["holidays"]:
            return y["holidays"][day]
        return None

    def is_trading_day(self, day):
        return self.holiday_reason(day) is None

    def previous_trading_day(self, day):
        d = day - datetime.timedelta(days=1)
        for _ in range(15):
            if self.is_trading_day(d):
                return d
            d -= datetime.timedelta(days=1)
        raise CalendarNotCovered("no trading day found within 15 days before %s" % day)

    def next_trading_day(self, day):
        d = day + datetime.timedelta(days=1)
        for _ in range(15):
            if self.is_trading_day(d):
                return d
            d += datetime.timedelta(days=1)
        raise CalendarNotCovered("no trading day found within 15 days after %s" % day)

    def session(self, day):
        """The Session on `day`, or None when closed."""
        y, assumed = self._year(day)
        warnings = []
        if assumed:
            warnings.append("calendar year %d not covered: weekdays assumed to be trading days (fixture mode)"
                            % day.year)
        if y is not None and day in y["specials"]:
            s = y["specials"][day]
            return Session(day, s["kind"], contracts_mod.hhmm(s["pre_open_start"]), contracts_mod.hhmm(s["open"]),
                           contracts_mod.hhmm(s["close"]), tuple(warnings), assumed)
        if self.holiday_reason(day) is not None:
            return None
        open_t, close_t, resolved = self.specs.session_times(day, self.data_mode)
        warnings.extend(resolved.warnings)
        return Session(day, "REGULAR", None, open_t, close_t, tuple(warnings), assumed)

    def bars_in_session(self, day, minutes=5):
        """Number of complete `minutes` bars between open and close."""
        s = self.session(day)
        if s is None:
            return 0
        span = (datetime.datetime.combine(day, s.close) - datetime.datetime.combine(day, s.open))
        return int(span.total_seconds() // (60 * minutes))


def check_config_fits_session(cfg, session):
    """Raise ConfigError if the entry window or flat-by time does not fit the session."""
    if session is None:
        raise ConfigError("market is closed on that date")

    def t(text):
        return contracts_mod.hhmm(text)
    if t(cfg.entry_window_start_ist) < session.open:
        raise ConfigError("entry window starts before the session opens (%s)" % session.open)
    if t(cfg.flat_by_ist) > session.close:
        raise ConfigError("flat_by_ist %s is after the session closes (%s)" % (cfg.flat_by_ist, session.close))
    if t(cfg.entry_window_end_ist) > t(cfg.flat_by_ist):
        raise ConfigError("entry window ends after flat_by_ist")
