"""Black-76 option price on a forward, with zero interest (so discounting is 1).

For SYNTHETIC fixtures and the simulator only. A model price is not a market quote.
"""
import math


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def price(forward, strike, years, vol, kind):
    """Call ('CE') or put ('PE') value. years <= 0 or vol <= 0 gives the intrinsic value."""
    forward, strike = float(forward), float(strike)
    if kind not in ("CE", "PE"):
        raise ValueError("kind must be 'CE' or 'PE'")
    if forward <= 0 or strike <= 0:
        raise ValueError("forward and strike must be positive")
    intrinsic = max(forward - strike, 0.0) if kind == "CE" else max(strike - forward, 0.0)
    if years <= 0 or vol <= 0:
        return intrinsic
    sd = vol * math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if kind == "CE":
        return forward * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1)
