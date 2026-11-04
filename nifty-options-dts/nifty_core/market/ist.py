"""Indian Standard Time: fixed UTC+05:30, no daylight saving.

Everything in nifty_core that has a clock time uses timezone-aware datetimes built here.
Nothing reads the machine's local time zone.
"""
import datetime

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
UTC = datetime.timezone.utc


def ist_datetime(day, time_of_day):
    """Aware IST datetime from a date and a datetime.time."""
    return datetime.datetime.combine(day, time_of_day).replace(tzinfo=IST)


def to_ist(moment):
    """Convert any aware datetime to IST. Naive datetimes are refused."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("naive datetime refused: attach a time zone (use parse_ist for IST text)")
    return moment.astimezone(IST)


def parse_ist(text):
    """'YYYY-MM-DD HH:MM' (or with seconds) in IST to an aware datetime."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    raise ValueError("expected 'YYYY-MM-DD HH:MM' in IST, got %r" % (text,))


def ist_date(moment):
    return to_ist(moment).date()


class SystemClock(object):
    """The only place in the package that reads the real time."""

    def now(self):
        return datetime.datetime.now(UTC).astimezone(IST)


class FixedClock(object):
    """A clock that returns a set moment, for tests and replays."""

    def __init__(self, moment):
        self._moment = to_ist(moment)

    def now(self):
        return self._moment

    def set(self, moment):
        self._moment = to_ist(moment)
