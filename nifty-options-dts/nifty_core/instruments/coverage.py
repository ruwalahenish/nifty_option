"""Naked-short detection. Reused by the risk engine in a later phase.

A short option is COVERED only if a long option exists with the same underlying, expiry and option
type, enough units, and a strike at least as protective:
    short call: a long call at the same or a LOWER strike
    short put : a long put at the same or a HIGHER strike
Each long leg can cover shorts only up to its own units (it is not counted twice). Anything
else is uncovered, including a credit spread's short leg, which this system does not allow.
"""


def _protective(long_leg, short_leg):
    if long_leg.option_type == "CE":
        return long_leg.strike <= short_leg.strike
    return long_leg.strike >= short_leg.strike


def uncovered_shorts(legs):
    """List of (short leg, uncovered units) for every short that is not fully covered."""
    capacity = {i: leg.units for i, leg in enumerate(legs) if leg.is_long}
    result = []
    for short in (leg for leg in legs if not leg.is_long):
        need = short.units
        for i, long_leg in enumerate(legs):
            if need == 0:
                break
            if not long_leg.is_long or capacity.get(i, 0) == 0:
                continue
            if (long_leg.underlying, long_leg.expiry, long_leg.option_type) != \
                    (short.underlying, short.expiry, short.option_type):
                continue
            if not _protective(long_leg, short):
                continue
            used = min(need, capacity[i])
            capacity[i] -= used
            need -= used
        if need > 0:
            result.append((short, need))
    return result
