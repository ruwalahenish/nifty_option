"""GET-only reader for two Upstox market-data endpoints. PAPER project: no order endpoint is reachable from here.

* One host (api.upstox.com), HTTPS, method GET, and only these paths: the option chain and the intraday candles.
  Any other path is refused before a request is made.
* The token comes from the environment variable UPSTOX_ANALYTICS_TOKEN (an Analytics token cannot trade). It is never
  printed, logged or stored, and it is scrubbed from error messages.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

ALLOWED_HOST = "api.upstox.com"
ALLOWED_PATH_PREFIXES = ("/v2/option/chain", "/v3/historical-candle/intraday/")
TOKEN_ENV = "UPSTOX_ANALYTICS_TOKEN"
NIFTY_KEY = "NSE_INDEX|Nifty 50"


class UpstoxReadError(Exception):
    def __init__(self, message, status=None):
        super(UpstoxReadError, self).__init__(message)
        self.status = status


def token_from_env(environ=None):
    value = (environ if environ is not None else os.environ).get(TOKEN_ENV, "").strip()
    if not value:
        raise UpstoxReadError("environment variable %s is not set" % TOKEN_ENV)
    return value


def _urllib_transport(url, headers, timeout):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise UpstoxReadError("network error: %s" % getattr(e, "reason", e))


def _get_json(path, query, token, transport, timeout=15):
    if not any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES):
        raise UpstoxReadError("path %r is not on the read-only allow-list" % path)
    url = "https://%s%s%s" % (ALLOWED_HOST, path, ("?" + urllib.parse.urlencode(query)) if query else "")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json",
               "User-Agent": "nifty-options-dts-recorder/1.0 (read-only market data)"}
    status, body = (transport or _urllib_transport)(url, headers, timeout)
    if status != 200:
        text = body.decode("utf-8", "replace").replace(token, "***")[:200] if body else ""
        raise UpstoxReadError("HTTP %s from the data service: %s" % (status, text), status)
    try:
        doc = json.loads(body.decode("utf-8"))
    except ValueError:
        raise UpstoxReadError("the data service sent something that is not JSON")
    if doc.get("status") != "success":
        raise UpstoxReadError("the data service did not report success")
    return doc.get("data")


def _text_price(x):
    """A quoted price as text, or '' when absent or not positive (a missing quote must stay missing)."""
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError):
        return ""
    return format(d, "f") if d > 0 else ""


def _text_qty(x):
    try:
        n = int(x)
    except (TypeError, ValueError):
        return ""
    return str(n) if n > 0 else ""


def _text_count(x):
    try:
        n = int(x)
    except (TypeError, ValueError):
        return ""
    return str(n) if n >= 0 else ""


def get_option_chain(token, expiry="current_week", underlying=NIFTY_KEY, transport=None):
    """Return (spot_text, rows). Each row is a dict with the chain.csv fields (without snapshot_time / timestamp)."""
    data = _get_json("/v2/option/chain", {"instrument_key": underlying, "expiry_date": expiry}, token, transport)
    rows, spot = [], None
    for item in data or []:
        if spot is None:
            spot = _text_price(item.get("underlying_spot_price"))
        for side, kind in (("call_options", "CE"), ("put_options", "PE")):
            leg = item.get(side) or {}
            md = leg.get("market_data") or {}
            rows.append({"expiry": item.get("expiry"), "strike": int(Decimal(str(item["strike_price"]))), "type": kind,
                         "bid": _text_price(md.get("bid_price")), "ask": _text_price(md.get("ask_price")),
                         "ltp": _text_price(md.get("ltp")), "bid_qty": _text_qty(md.get("bid_qty")),
                         "ask_qty": _text_qty(md.get("ask_qty")), "volume": _text_count(md.get("volume")),
                         "open_interest": _text_count(md.get("oi"))})
    if not spot or not rows:
        raise UpstoxReadError("the option chain came back empty")
    return spot, rows


def get_candles(token, minutes=5, underlying=NIFTY_KEY, transport=None):
    """Today's index candles, oldest first, as (start, open, high, low, close, volume-or-'') text tuples."""
    path = "/v3/historical-candle/intraday/%s/minutes/%d" % (urllib.parse.quote(underlying, safe=""), minutes)
    data = _get_json(path, None, token, transport) or {}
    out = []
    for c in data.get("candles", []):
        out.append((str(c[0]), _text_price(c[1]), _text_price(c[2]), _text_price(c[3]), _text_price(c[4]), ""))
    out.sort(key=lambda r: r[0])
    return out
