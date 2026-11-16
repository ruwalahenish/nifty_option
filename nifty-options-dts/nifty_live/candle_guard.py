"""Is the candle that just closed FINAL yet? A decision is only taken on a final candle.

Why this exists. The live loop used to fetch today's 5-minute candles once, 0-10 s after a slot boundary, and decide on
them. On the recorded 2026-11-06 session the vendor's newest candle was still provisional at that moment: its close lagged
the boundary by 15-90 s and differed from the final close by up to 28 index points (0.11%, more than half of the whole
0.20% "not chasing" band). The breakout, extension and resumption rules all read that close, and the decision was never
revisited, so a rule could pass or fail on a price that was no longer the candle's close.

The guard says GO only when ALL of these hold for the candle that ended at the slot boundary:
    1. it is published,
    2. SETTLE seconds have passed since the boundary,
    3. two consecutive reads gave identical open/high/low/close (it has stopped changing),
    4. while the vendor may still be catching up (before SPOT_CHECK_UNTIL seconds), its close is within a small band of
       the index spot we recorded at the boundary (an independent price source).
It says WAIT while that is still pending, and SKIP once MAX_WAIT seconds have passed with the candle still missing or still
changing. A skipped slot takes no NEW entry (a position that is already open is still managed); the next slot decides.

No network and no clock here: the caller hands in the rows, the spot log and the time. PAPER trading only.
"""
import datetime as dt
from collections import namedtuple
from decimal import Decimal

GO, WAIT, SKIP = "GO", "WAIT", "SKIP"
Verdict = namedtuple("Verdict", "state text")

SLOT_MINUTES = 5
MIN_SETTLE_SECONDS = 30
SPOT_CHECK_UNTIL_SECONDS = 90          # the observed vendor lag was 15-90 s; after this a stable candle is accepted
MAX_WAIT_SECONDS = 120
SPOT_TOLERANCE_PCT = Decimal("0.04")   # about 10 index points: one poll's normal movement, far below the 28-point errors seen
SPOT_WINDOW_SECONDS = 20               # only a spot snapshot this close to the boundary counts as "the spot at the boundary"


def _signature(row):
    return tuple(str(row[k]) for k in ("open", "high", "low", "close"))


class CandleGuard(object):
    def __init__(self, min_settle=MIN_SETTLE_SECONDS, spot_check_until=SPOT_CHECK_UNTIL_SECONDS,
                 max_wait=MAX_WAIT_SECONDS, tolerance_pct=SPOT_TOLERANCE_PCT):
        self.min_settle, self.spot_check_until = min_settle, spot_check_until
        self.max_wait, self.tolerance_pct = max_wait, Decimal(tolerance_pct)
        self._slot, self._sig, self._reads = None, None, 0

    def check(self, slot, now, rows, spots):
        """Verdict for the slot ending at `slot`. `rows` are candle dicts (start, open, high, low, close as text),
        `spots` an iterable of (aware datetime, Decimal spot) recorded by the poll loop. Call once per poll."""
        waited = (now - slot).total_seconds()
        want = slot - dt.timedelta(minutes=SLOT_MINUTES)
        row = next((r for r in rows if dt.datetime.fromisoformat(r["start"]) == want), None)
        sig = None if row is None else _signature(row)
        if self._slot != slot:
            self._slot, self._sig, self._reads = slot, None, 0
        stable = sig is not None and sig == self._sig
        self._sig, self._reads = sig, self._reads + 1

        problem = None
        if row is None:
            problem = "the %s candle is not published yet" % want.strftime("%H:%M")
        elif waited < self.min_settle:
            problem = "only %d s since the boundary (settling for %d s)" % (waited, self.min_settle)
        elif not stable:
            problem = "the %s candle changed since the previous read" % want.strftime("%H:%M")
        elif waited < self.spot_check_until:
            problem = self._spot_problem(slot, row, spots)
        if problem is None:
            return Verdict(GO, "candle %s final" % want.strftime("%H:%M"))
        if waited >= self.max_wait and self._reads >= 2:
            return Verdict(SKIP, "%s, still not final after %d s" % (problem, waited))
        return Verdict(WAIT, problem)

    def _spot_problem(self, slot, row, spots):
        near = [(abs((t - slot).total_seconds()), s) for t, s in spots]
        near = [x for x in near if x[0] <= SPOT_WINDOW_SECONDS]
        if not near:
            return None                                   # no independent price near the boundary: stability alone decides
        spot = min(near, key=lambda x: x[0])[1]
        gap = abs(Decimal(row["close"]) / spot - 1) * 100
        if gap > self.tolerance_pct:
            return "candle close %s is %s%% from the spot recorded at the boundary %s (limit %s%%)" % (
                row["close"], gap.quantize(Decimal("0.001")), spot, self.tolerance_pct)
        return None
