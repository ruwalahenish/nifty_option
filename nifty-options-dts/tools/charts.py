"""SVG charts and the option-chain table for the dashboard: plain inline SVG, no scripts, colours from the theme variables.

    spot_svg    NIFTY through the day with the opening range, trigger levels and the day average
    gauge_svg   where an open position's unrealised net P&L sits between its stop and its target
    equity_svg  cumulative net P&L
    band_svg    where price has to be for a strategy's price conditions to hold (the bands the selector measures against)
PAPER trading only. Not financial advice.
"""
import datetime
from decimal import Decimal

from tools.ui_kit import _dec, esc


def equity_svg(points):
    """points: list of (label, cumulative Decimal). A plain inline SVG line, no scripts."""
    if len(points) < 1:
        return ""
    vals = [Decimal(0)] + [p[1] for p in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or Decimal(1)
    w, h, pad = 640, 180, 28
    n = len(vals)

    def xy(i, v):
        x = pad + (w - 2 * pad) * (Decimal(i) / Decimal(max(n - 1, 1)))
        y = h - pad - (h - 2 * pad) * ((v - lo) / span)
        return "%.1f,%.1f" % (x, y)
    zero_y = xy(0, Decimal(0)).split(",")[1]
    line = " ".join(xy(i, v) for i, v in enumerate(vals))
    return ("<svg viewBox='0 0 %d %d' role=img aria-label='Cumulative net paper P&L after simulated costs'>"
            "<line x1=%d x2=%d y1=%s y2=%s stroke='var(--line)' stroke-dasharray='4 4'/>"
            "<polyline fill=none stroke='var(--accent)' stroke-width=2 points='%s'/>"
            "<text x=%d y=14 fill='var(--mute)' font-size=11>high %s</text>"
            "<text x=%d y=%d fill='var(--mute)' font-size=11>low %s</text></svg>" % (
                w, h, pad, w - pad, zero_y, zero_y, line, pad, esc("{:,.0f}".format(hi)), pad, h - 6,
                esc("{:,.0f}".format(lo))))


def _epoch(iso):
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


LEVEL_STYLE = (("opening_range_high", "range high", "var(--warn)", "6 3"), ("opening_range_low", "range low", "var(--warn)", "6 3"),
               ("breakout_up", "long trigger", "var(--good)", "2 3"), ("breakout_down", "short trigger", "var(--bad)", "2 3"),
               ("day_average", "day avg", "var(--accent)", "2 3"))


def spot_svg(series, candle_closes, levels):
    """NIFTY through the day: candle closes before the first live tick, then every live tick, with the opening range, the
    breakout triggers and the day average drawn as lines. Plain inline SVG, no scripts."""
    first = _epoch(series[0][0]) if series else None
    pts = []
    for rows in (candle_closes or [], series or []):
        for row in rows:
            if not (isinstance(row, list) and len(row) == 2):
                continue
            e, v = _epoch(row[0]), _dec(row[1])
            if e is None or v is None:
                continue
            if rows is candle_closes and first is not None and e >= first:
                continue
            pts.append((e, v, row[0]))
    if len(pts) < 2:
        return ""
    pts.sort(key=lambda p: p[0])
    thinned = pts[::max(1, len(pts) // 700)]
    if thinned[-1] is not pts[-1]:
        thinned.append(pts[-1])
    pts = thinned
    lv = {k: _dec(v) for k, v in (levels or {}).items()}
    lv = {k: v for k, v in lv.items() if v is not None}
    ys = [p[1] for p in pts] + list(lv.values())
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * Decimal("0.08") or Decimal(1)
    lo, hi = lo - pad, hi + pad
    span = hi - lo
    t0, t1 = pts[0][0], pts[-1][0]
    tspan = (t1 - t0) or 1
    w, h, pl, pr, pt, pb = 640, 240, 10, 112, 14, 22

    def x(e):
        return pl + (w - pl - pr) * ((e - t0) / tspan)

    def y(v):
        return pt + (h - pt - pb) * (1 - float((v - lo) / span))
    out = ["<svg viewBox='0 0 %d %d' role=img aria-label='NIFTY spot with the opening range and breakout levels'>" % (w, h)]
    placed = []
    for key, label, colour, dash in LEVEL_STYLE:
        if key not in lv:
            continue
        yy = y(lv[key])
        out.append("<line x1=%d x2=%d y1=%.1f y2=%.1f stroke='%s' stroke-dasharray='%s' stroke-width='1' opacity='.8'/>" % (
            pl, w - pr, yy, yy, colour, dash))
        ly = yy + 3
        while any(abs(ly - p) < 10 for p in placed):
            ly += 10
        placed.append(ly)
        out.append("<text x=%d y=%.1f fill='%s' font-size=10>%s %s</text>" % (
            w - pr + 4, ly, colour, esc(label), esc("{:,.0f}".format(lv[key]))))
    out.append("<polygon fill='var(--accent)' opacity='.08' points='%s %.1f,%d %.1f,%d'/>" % (" ".join(
        "%.1f,%.1f" % (x(e), y(v)) for e, v, _ in pts), x(pts[-1][0]), h - pb, x(pts[0][0]), h - pb))
    out.append("<polyline fill=none stroke='var(--ink)' stroke-width=1.6 points='%s'/>" % " ".join(
        "%.1f,%.1f" % (x(e), y(v)) for e, v, _ in pts))
    e, v, _ = pts[-1]
    out.append("<circle cx=%.1f cy=%.1f r=4 fill='var(--accent)'/>" % (x(e), y(v)))
    out.append("<text x=%.1f y=%.1f fill='var(--accent)' font-size=11 font-weight=700 text-anchor=end>%s</text>" % (
        x(e) - 7, max(y(v) - 7, 11), esc("{:,.2f}".format(v))))
    out.append("<text x=%d y=%d fill='var(--mute)' font-size=10>%s</text>" % (pl, h - 6, esc(str(pts[0][2])[11:16])))
    out.append("<text x=%d y=%d fill='var(--mute)' font-size=10 text-anchor=end>%s</text>" % (
        w - pr, h - 6, esc(str(pts[-1][2])[11:16])))
    out.append("</svg>")
    return "".join(out)


def _chain_table(cv):
    rows = (cv or {}).get("rows") or []
    if not rows:
        return "<p class=mute>no option quotes yet</p>"

    def cell(side, tag):
        side = side or {}
        label = esc(side.get("ltp") or "-") + ("<span class=tag>%s</span>" % esc(tag) if tag else "")
        return label, esc(side.get("bid") or "-"), esc(side.get("ask") or "-")
    body = []
    for r in rows:
        held = r.get("held") or {}
        ce_ltp, ce_bid, ce_ask = cell(r.get("ce"), held.get("CE"))
        pe_ltp, pe_bid, pe_ask = cell(r.get("pe"), held.get("PE"))
        cls = " ".join(c for c in ("atm" if r.get("atm") else "", "held" if (held.get("CE") or held.get("PE")) else "") if c)
        body.append("<tr%s><td class=n>%s</td><td class=n>%s</td><td class=n>%s</td><td class=n>%s</td><td class=n>%s</td>"
                    "<td class=n>%s</td><td class=n>%s</td></tr>" % (
                        " class='%s'" % cls if cls else "", ce_ltp, ce_bid, ce_ask, esc(r.get("strike")), pe_bid, pe_ask,
                        pe_ltp))
    return ("<div class=tbl><table><tr><th class=n>CE last</th><th class=n>CE bid</th><th class=n>CE ask</th>"
            "<th class=n>STRIKE</th><th class=n>PE bid</th><th class=n>PE ask</th><th class=n>PE last</th></tr>%s</table></div>"
            "<p class=mute>Expiry %s. The bold row is at the money; BUY / SELL marks the legs of your open paper position.</p>" % (
                "".join(body), esc(cv.get("expiry"))))


def gauge_svg(net, stop, target):
    """Where the unrealised net P&L sits between the stop level (left) and the profit-target level (right)."""
    net, stop, target = _dec(net), _dec(stop), _dec(target)
    if net is None or stop is None or target is None or target <= stop:
        return ""
    span = target - stop
    frac = min(max((net - stop) / span, Decimal(0)), Decimal(1))
    zero = min(max((Decimal(0) - stop) / span, Decimal(0)), Decimal(1))
    w, pad = 640, 20

    def x(f):
        return pad + (w - 2 * pad) * float(f)
    return ("<svg viewBox='0 0 %d 56' role=img aria-label='Unrealised P&amp;L between the stop level and the target level'>"
            "<rect x=%d y=20 width=%d height=10 rx=5 fill='var(--line)'/>"
            "<line x1=%.1f x2=%.1f y1=14 y2=36 stroke='var(--mute)' stroke-dasharray='3 3'/>"
            "<circle cx=%.1f cy=25 r=8 fill='var(--accent)'/>"
            "<text x=%d y=52 fill='var(--bad)' font-size=11>stop %s</text>"
            "<text x=%d y=52 fill='var(--good)' font-size=11 text-anchor=end>target %s</text>"
            "<text x=%.1f y=12 fill='var(--mute)' font-size=11 text-anchor=middle>0</text></svg>" % (
                w, pad, w - 2 * pad, x(zero), x(zero), x(frac), pad, esc("{:,.0f}".format(stop)), w - pad,
                esc("{:,.0f}".format(target)), x(zero)))



def _num(text):
    try:
        return Decimal(str(text).replace(",", ""))
    except Exception:
        return None


def band_svg(zones, spot, day_low=None, day_high=None):
    """One row per price level or band the strategy's conditions are measured against, with where the market is NOW.
    `zones` are the backend's own (label, price or low-high); nothing is computed here beyond the drawing scale."""
    rows = []
    for z in zones or []:
        if "price" in z and _num(z["price"]) is not None:
            rows.append((z.get("label", ""), _num(z["price"]), None))
        elif "low" in z and _num(z.get("low")) is not None and _num(z.get("high")) is not None:
            rows.append((z.get("label", ""), _num(z["low"]), _num(z["high"])))
    now = _num(spot)
    if not rows or now is None:
        return ""
    xs = [now] + [v for _, a, b in rows for v in (a, b) if v is not None]
    xs += [v for v in (_num(day_low), _num(day_high)) if v is not None]
    lo, hi = min(xs), max(xs)
    pad = (hi - lo) * Decimal("0.12") or Decimal(1)
    lo, hi = lo - pad, hi + pad
    w, left, right, rh = 460, 10, 10, 50

    def x(v):
        return left + (w - left - right) * float((v - lo) / (hi - lo))
    h = rh * len(rows) + 26
    out = ["<svg viewBox='0 0 %d %d' role=img aria-label='Where price must be for each condition, and where it is now'>" % (w, h)]
    for i, (label, a, b) in enumerate(rows):
        y = 4 + i * rh
        value = "{:,.2f}".format(a) if b is None else "{:,.2f} \u2013 {:,.2f}".format(a, b)
        out.append("<text x=%d y=%d fill='var(--ink)' font-size=13 font-weight=600>%s</text>" % (left, y + 13, esc(label)))
        out.append("<text x=%d y=%d fill='var(--accent)' font-size=13 font-weight=600 text-anchor=end>%s</text>" % (w - right, y + 13, esc(value)))
        out.append("<line x1=%d x2=%d y1=%d y2=%d stroke='var(--line)' stroke-width=8 stroke-linecap=round />" % (left, w - right, y + 32, y + 32))
        if b is None:
            out.append("<line x1=%.1f x2=%.1f y1=%d y2=%d stroke='var(--accent)' stroke-width=4 stroke-linecap=round />" % (x(a), x(a), y + 22, y + 42))
        else:
            out.append("<rect x=%.1f y=%d width=%.1f height=12 rx=6 fill='var(--accent)' opacity='.35' stroke='var(--accent)' />" % (
                x(a), y + 26, max(x(b) - x(a), 4)))
    out.append("<line x1=%.1f x2=%.1f y1=2 y2=%d stroke='var(--warn)' stroke-width=2 stroke-dasharray='4 3' />" % (x(now), x(now), h - 16))
    anchor = "end" if x(now) > w * 0.7 else "start" if x(now) < w * 0.3 else "middle"
    out.append("<text x=%.1f y=%d fill='var(--warn)' font-size=12 font-weight=700 text-anchor=%s>now %s</text>" % (
        x(now), h - 3, anchor, esc("{:,.2f}".format(now))))
    out.append("</svg>")
    return "".join(out)
