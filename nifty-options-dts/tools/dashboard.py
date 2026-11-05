"""Local, READ-ONLY dashboard for the paper-trading journals.

    python tools/dashboard.py            # then open http://127.0.0.1:8765
    python tools/dashboard.py --port 9000 --journal-dir trader/journal

    http://127.0.0.1:8765/live  shows the running live PAPER session (open position, unrealised P&L, waiting tickets).
    It reads trader/live/state.json, which `python -m nifty_live.live_run` rewrites at every poll.

* It only reads journal files. It answers GET requests only; every other method gets 405. It has no form, no button
  that acts, no way to approve a ticket and no route that writes anything. To approve a ticket you still re-run the
  command line with --answer FINGERPRINT="yes".
* It listens on 127.0.0.1 only (this computer), never on a network address.
* Journal files are chosen from a fixed listing of the journal folder, so a request cannot name another path.
* The same rules as the text report apply: a fixture page carries the "not evidence of profitability" line, and a
  real-data page shows no P&L or equity curve until the minimum number of completed paper trades exists.
PAPER trading only. Not financial advice.
"""
import argparse
import datetime
import html
import json
import os
import re
import sys
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from nifty_core.journal import journal as journal_mod    # noqa: E402
from nifty_core.journal import report as report_mod      # noqa: E402

DEFAULT_DIR = os.path.join(PROJECT, "trader", "journal")
DEFAULT_LIVE_STATE = os.path.join(PROJECT, "trader", "live", "state.json")
MAX_STATE_BYTES = 8 * 1024 * 1024
HEX16 = re.compile(r"^[0-9a-f]{16}$")
STALE_AFTER_SECONDS = 120
BIND_HOST = "127.0.0.1"
NOTICE = "PAPER TRADING ONLY. Simulated fills. Not financial advice. This page cannot place or approve anything."

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14181f;--mute:#5b6572;--line:#dde1e7;--accent:#1f5fbf;--good:#17693a;--bad:#a8261f;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#171c23;--ink:#e8ecf1;--mute:#98a3b1;--line:#2a323d;--accent:#6aa3f0;--good:#58c283;--bad:#ef7b73;--warn:#e0b04a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1000px;margin:0 auto;padding:16px}
.banner{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--warn);padding:8px 12px;border-radius:6px;color:var(--mute);font-size:13px}
h1{font-size:22px;margin:18px 0 6px}h2{font-size:16px;margin:22px 0 8px}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat b{display:block;font-size:20px}.stat span{color:var(--mute);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:var(--good)}.neg{color:var(--bad)}.mute{color:var(--mute)}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px;font-size:12px;margin:6px 0}
.tbl{overflow-x:auto}svg{width:100%;height:auto;display:block}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid var(--line);font-size:12px;color:var(--mute)}
.chip.live{border-color:var(--good);color:var(--good)}.chip.replay{border-color:var(--warn);color:var(--warn)}
.alert{background:var(--card);border:1px solid var(--bad);border-left:4px solid var(--bad);padding:8px 12px;border-radius:6px;margin:10px 0}
.big{font-size:26px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--good);margin-right:6px;animation:pulse 1.2s infinite}
.dot.off{background:var(--bad);animation:none}@keyframes pulse{50%{opacity:.25}}
tr.atm td{font-weight:700;background:var(--bg)}tr.held td{border-bottom:2px solid var(--accent)}
.tag{font-size:10px;border:1px solid var(--accent);color:var(--accent);border-radius:8px;padding:0 5px;margin-left:4px}
.stat b span{font-size:inherit;color:inherit}.stat b .pos{color:var(--good)}.stat b .neg{color:var(--bad)}
"""


def esc(x):
    return html.escape("" if x is None else str(x))


def page(title, body, refresh=None, head_extra=""):
    meta = ('<meta http-equiv="refresh" content="%d">' % refresh if refresh else "") + head_extra
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            "initial-scale=1'>%s<title>%s</title><style>%s</style></head><body><main><div class=banner>%s</div>%s"
            "</main></body></html>" % (meta, esc(title), CSS, esc(NOTICE), body))


def list_journals(folder):
    """Names of the .jsonl files directly inside folder (no sub-folders)."""
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if n.endswith(".jsonl") and os.path.isfile(os.path.join(folder, n)))


def money(value):
    d = Decimal(str(value))
    cls = "pos" if d > 0 else "neg" if d < 0 else ""
    return '<span class="%s">%s</span>' % (cls, esc("{:,.2f}".format(d)))


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


def _table(headers, rows, num=()):
    if not rows:
        return "<p class=mute>none</p>"
    head = "".join("<th%s>%s</th>" % (" class=n" if i in num else "", esc(h)) for i, h in enumerate(headers))
    body = "".join("<tr>%s</tr>" % "".join("<td%s>%s</td>" % (" class=n" if i in num else "", c)
                                           for i, c in enumerate(r)) for r in rows)
    return "<div class=tbl><table><tr>%s</tr>%s</table></div>" % (head, body)


def render_index(folder):
    names = list_journals(folder)
    rows = []
    for n in names:
        rep = report_mod.build_report(os.path.join(folder, n))
        status = "OK" if rep["journal_ok"] else "BROKEN"
        rows.append(["<a href='/journal?name=%s'>%s</a>" % (esc(n), esc(n)), esc(rep.get("data_source")),
                     esc(rep.get("runs", "")), esc(rep.get("completed_trades", "")),
                     esc(status)])
    body = "<p><a href='/live'>Live paper session &rarr;</a></p><h1>Paper-trading journals</h1>"
    if rows:
        body += "<div class=card>%s</div>" % _table(["journal", "data", "runs", "completed trades", "chain"], rows, (2, 3))
    else:
        body += ("<div class=card>No journals in <code>%s</code> yet. Create some with:<pre>python tools/backtest.py "
                 "--scenarios tests/fixtures/scenarios\npython -m nifty_core.cli run-day --scenario bullish_trend</pre></div>"
                 % esc(folder))
    return page("Journals", body, refresh=15)


def _runs_by_id(records):
    return {r["run_id"]: r["payload"].get("day") for r in records if r["kind"] == "RUN_START"}


def render_journal(folder, name):
    if name not in list_journals(folder):
        return None
    path = os.path.join(folder, name)
    rep = report_mod.build_report(path)
    body = "<p><a href='/'>&larr; all journals</a></p><h1>%s</h1>" % esc(name)
    body += "<div class=card><b>%s</b><br><span class=mute>%s</span></div>" % (
        esc(rep.get("label", "PAPER")), esc(rep.get("caveat", "")))
    if not rep["journal_ok"]:
        body += "<div class=card neg>Journal chain BROKEN: %s</div>" % esc("; ".join(rep["journal_problems"]))
    if "error" in rep:
        return page(name, body + "<div class=card>%s</div>" % esc(rep["error"]))
    records = journal_mod.read_records(path)
    days = _runs_by_id(records)
    show = rep["net_pnl_inr"] is not None

    tiles = [("Completed paper trades", rep["completed_trades"]), ("Decisions", rep["decisions"]),
             ("Net P&L (after costs)", money(rep["net_pnl_inr"]) if show else "not shown (n=%d)" % rep["sample_size"]),
             ("Largest loss", money(rep["largest_loss_inr"]) if show else "-"),
             ("Wins / losses", "%s / %s" % (rep["wins"], rep["losses"]) if show else "-"),
             ("Critical exceptions", rep["critical_exceptions"])]
    body += "<div class='grid'>%s</div>" % "".join(
        "<div class='card stat'><b>%s</b><span>%s</span></div>" % (v if isinstance(v, str) and "<span" in v else esc(v), esc(k))
        for k, v in tiles)

    exits = [r for r in records if r["kind"] == "EXIT"]
    if show and exits:
        cum, pts = Decimal(0), []
        for r in exits:
            cum += Decimal(r["payload"]["net_pnl_inr"])
            pts.append((r["at"], cum))
        body += "<h2>Cumulative net P&amp;L</h2><div class=card>%s</div>" % equity_svg(pts)

    body += "<h2>Completed trades</h2><div class=card>%s</div>" % _table(
        ["day", "exit time", "exit reason", "gross INR", "net INR"],
        [[esc(days.get(r["run_id"], "")), esc(r["at"][11:16]), esc(r["payload"].get("exit_reason")),
          money(r["payload"]["gross_pnl_inr"]) if show else "-", money(r["payload"]["net_pnl_inr"]) if show else "-"]
         for r in exits], (3, 4))

    body += "<h2>Decisions by status</h2><div class=card>%s</div>" % _table(
        ["status", "count"], [[esc(k), esc(v)] for k, v in rep["decision_status"].items()], (1,))
    if rep["blocked_reason_counts"]:
        body += "<h2>Why candidates were blocked</h2><div class=card>%s</div>" % _table(
            ["reason code", "count"], [[esc(k), esc(v)] for k, v in rep["blocked_reason_counts"].items()], (1,))

    answered = {r["payload"]["fingerprint"] for r in records if r["kind"] == "APPROVAL"}
    waiting = [r for r in records if r["kind"] == "TICKET" and r["payload"]["fingerprint"] not in answered]
    if waiting:
        body += "<h2>Tickets waiting for your reply</h2>"
        for r in waiting[-5:]:
            fp = r["payload"]["fingerprint"]
            body += ("<div class=card><b>%s</b> <span class=mute>%s</span><pre>%s</pre>"
                     "<span class=mute>To approve exactly this ticket, run the command line with</span> "
                     "<code>--answer %s=\"yes\"</code></div>") % (esc(fp), esc(days.get(r["run_id"], "")),
                                                                  esc(r["payload"]["text"]), esc(fp))

    notable = [r for r in records if r["kind"] in ("DECISION", "CRITICAL", "DAY_LOCKED")
               and (r["kind"] != "DECISION" or r["payload"].get("status") != "NO_TRADE")][-40:]
    body += "<h2>Latest decisions (NO_TRADE hidden)</h2><div class=card>%s</div>" % _table(
        ["day", "time", "kind", "status", "regime", "detail"],
        [[esc(days.get(r["run_id"], "")), esc(r["at"][11:16]), esc(r["kind"]), esc(r["payload"].get("status", "")),
          esc(r["payload"].get("regime", {}).get("label", "") if r["kind"] == "DECISION" else ""),
          esc(", ".join(r["payload"].get("risk", {}).get("reason_codes", [])) if r["kind"] == "DECISION"
              else r["payload"].get("text") or r["payload"].get("reason", ""))] for r in notable])
    return page(name, body, refresh=15)


LIVE_SCRIPT = """<script>
(function () {
  var box = document.getElementById('live'), conn = document.getElementById('conn'), bad = 0;
  function tick() {
    if (document.hidden) { return; }
    fetch('/live?fragment=1', {cache: 'no-store'})
      .then(function (r) { if (!r.ok) { throw new Error(r.status); } return r.text(); })
      .then(function (t) { box.innerHTML = t; bad = 0; conn.hidden = true; })
      .catch(function () { bad += 1; if (bad >= 3) { conn.hidden = false; } });
  }
  setInterval(tick, 1000);
})();
</script>"""


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



def _dec(x):
    try:
        return None if x is None else Decimal(str(x))
    except Exception:
        return None


def _money(x):
    d = _dec(x)
    return '<span class=mute>-</span>' if d is None else money(d)


def _hhmmss(iso):
    return esc(str(iso)[11:19]) if iso else "-"


def load_live_state(path):
    """The live runner's state file as a dict, or None when it is missing, too large or not valid JSON."""
    try:
        if os.path.getsize(path) > MAX_STATE_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("kind") == "LIVE_PAPER_STATE" else None


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


def _legs_table(legs):
    rows = [[esc(leg.get("contract")), esc(leg.get("side")), esc(leg.get("units")), esc(leg.get("entry")),
             esc(leg.get("bid") or "-"), esc(leg.get("ask") or "-"), esc(leg.get("mark") or "-"),
             _money(leg.get("leg_gross_inr"))] for leg in legs]
    return _table(["contract", "side", "units", "paper entry", "bid", "ask", "exit mark", "leg P&L INR"], rows,
                  (2, 3, 4, 5, 6, 7))


def _open_position_card(pos):
    u = pos.get("unrealized")
    body = "<b>%s</b> &middot; %s &middot; %s lot(s) &middot; entered %s &middot; defined risk INR %s" % (
        esc(pos.get("strategy")), esc(pos.get("state")), esc(pos.get("lots")), _hhmmss(pos.get("entry_time")),
        esc(pos.get("defined_risk_inr")))
    body += _legs_table(pos.get("legs", []))
    if u:
        body += ("<div class=grid style='margin-top:10px'>"
                 "<div class='card stat'><b>%s</b><span>unrealised gross</span></div>"
                 "<div class='card stat'><b>%s</b><span>entry costs paid</span></div>"
                 "<div class='card stat'><b>%s</b><span>exit costs (estimate)</span></div>"
                 "<div class='card stat'><b class=big>%s</b><span>UNREALISED net P&amp;L (simulated)</span></div></div>" % (
                     _money(u.get("gross_pnl_inr")), esc(u.get("actual_entry_costs_inr")),
                     esc(u.get("estimated_exit_costs_inr")), _money(u.get("net_pnl_inr"))))
        body += gauge_svg(u.get("net_pnl_inr"), pos.get("stop_at_net_inr"), pos.get("target_at_net_inr"))
    else:
        body += "<p class=neg>Mark unavailable right now: %s</p>" % esc("; ".join(pos.get("mark_problems") or ["no quote"]))
    if pos.get("exit_reason"):
        waiting = " - waiting: " + ", ".join(pos["pending_reasons"]) if pos.get("pending_reasons") else ""
        body += "<p class=mute>Exit decided: %s (attempt %s)%s</p>" % (
            esc(pos.get("exit_reason")), esc(pos.get("exit_attempts")), esc(waiting))
    return "<div class=card>%s</div>" % body


def live_fragment(state_path, journal_dir=None, now=None):
    """The inside of the Live page. The page swaps it in place every second, so the figures move without a reload."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    st = load_live_state(state_path)
    if st is None:
        body = ("<h1>Live paper session</h1><div class=card>No live session is writing <code>%s</code> yet. Start one "
                "during market hours:<pre>python -m nifty_live.live_run --config my_real_config.json --dashboard</pre>"
                "or preview it any time on a simulated clock:<pre>python -m nifty_live.live_run --replay "
                "data/recordings/YYYY-MM-DD --config my_real_config.json --dashboard</pre></div>" % esc(state_path))
        return body
    mode = st.get("mode", "LIVE")
    chip = ("<span class='chip live'>LIVE quotes</span>" if mode == "LIVE"
            else "<span class='chip replay'>REPLAY - simulated clock</span>")
    if st.get("entry_mode") == "AUTO":
        chip += " <span class='chip live'>AUTO paper entry &amp; exit</span>"
    elif st.get("entry_mode") == "MANUAL":
        chip += " <span class='chip'>entries need your yes</span>"
    body = "<p><a href='/'>&larr; journals</a></p><h1>Live paper session %s</h1>" % chip
    body += "<p class=mute>%s &middot; %s &middot; status %s &middot; market time %s</p>" % (
        esc(st.get("label")), esc(st.get("day")), esc(st.get("status")), _hhmmss(st.get("updated_at")))
    try:
        age = (now - datetime.datetime.fromisoformat(st["written_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        age = None
    if mode == "LIVE" and st.get("status") != "FINISHED" and (age is None or age > STALE_AFTER_SECONDS):
        body += ("<div class=alert><b>NOT UPDATING.</b> The live runner has not written for %s. It may have stopped; "
                 "everything below is the last known state and is not current.</div>" % (
                     "a while" if age is None else "%d s" % age))
    if st.get("problem"):
        body += "<div class=alert>%s</div>" % esc(st["problem"])
    if st.get("kill_switch"):
        body += "<div class=alert><b>KILL switch is on.</b> No new paper entry will be made.</div>"

    poll = st.get("poll_seconds")
    fresh = mode != "LIVE" or (age is not None and age <= max(4 * (poll or 30), 20))
    body += "<p class=mute><span class='dot%s'></span>%s &middot; page updates every second</p>" % (
        "" if fresh else " off",
        "REPLAY on a simulated clock" if mode != "LIVE" else "live: last data written %s ago%s" % (
            "?" if age is None else "%d s" % age, " &middot; polling every %s s" % poll if poll else ""))
    market, totals = st.get("market") or {}, st.get("totals")
    tiles = [("NIFTY spot", esc(market.get("spot") or "-")),
             ("quote age", esc("%s s" % market["data_age_seconds"]) if market else "-")]
    if totals:
        day_total = totals.get("day_total_inr")
        tiles += [("Day P&L (realised + unrealised)", _money(day_total) if day_total is not None else "mark unavailable"),
                  ("Unrealised (simulated)", _money(totals.get("unrealised_net_inr"))),
                  ("Realised (closed trades)", _money(totals.get("realised_net_inr"))),
                  ("Trades today", esc("%s / %s" % (totals.get("trades_today"),
                                                    (st.get("limits") or {}).get("max_trades_per_day"))))]
    body += "<div class=grid>%s</div>" % "".join(
        "<div class='card stat'><b>%s</b><span>%s</span></div>" % (v, esc(k)) for k, v in tiles)
    body += "<p class=mute>%s</p>" % esc(st.get("note"))

    for t in st.get("pending_tickets") or []:
        fp = str(t.get("fingerprint", ""))
        cmd = ("python -m nifty_live.live_run answer %s yes" % fp) if HEX16.match(fp) else "(fingerprint missing)"
        body += ("<h2>Ticket waiting for your reply</h2><div class=card><pre>%s</pre>"
                 "<p>Valid until <b>%s</b> (%s s after it was made); after that it is released and nothing is entered. "
                 "To approve exactly this paper order, run in a terminal:</p><pre>%s</pre>"
                 "<p class=mute>This page cannot approve anything.</p></div>" % (
                     esc(t.get("text")), _hhmmss(t.get("expires_at")), esc(st.get("approval_window_seconds")), esc(cmd)))

    body += "<h2>Open paper position</h2>"
    opens = st.get("open_positions") or []
    body += "".join(_open_position_card(p) for p in opens) if opens else "<div class=card><span class=mute>none</span></div>"

    d = st.get("last_decision")
    if d:
        why = "<br><span class=mute>%s</span>" % esc(" | ".join(d.get("reasons") or [])) if d.get("reasons") else ""
        body += ("<h2>Latest decision</h2><div class=card><b>%s</b> at %s &middot; regime %s &middot; strategy %s%s</div>" % (
            esc(d.get("status")), _hhmmss(d.get("decision_time")), esc(d.get("regime")), esc(d.get("strategy_outcome")), why))
    sl = st.get("slots") or {}
    if sl.get("next"):
        body += "<p class=mute>Next decision slot: %s (a slot every 5 minutes from %s to %s).</p>" % (
            _hhmmss(sl.get("next")), _hhmmss(sl.get("first")), _hhmmss(sl.get("last")))

    price = spot_svg(st.get("spot_series"), st.get("candle_closes"), st.get("levels"))
    if price:
        body += "<h2>NIFTY today, with the opening range and breakout triggers</h2><div class=card>%s</div>" % price
    if st.get("chain_view"):
        body += "<h2>Option chain around the money (live quotes)</h2><div class=card>%s</div>" % _chain_table(st["chain_view"])

    pts = []
    for row in st.get("curve") or []:
        v = _dec(row[1]) if isinstance(row, list) and len(row) == 2 else None
        if v is not None:
            pts.append((row[0], v))
    if len(pts) >= 2 and any(v != 0 for _, v in pts):
        pts = pts[::max(1, len(pts) // 600)]
        body += "<h2>Today's P&amp;L (realised + unrealised)</h2><div class=card>%s</div>" % equity_svg(pts)

    closed = st.get("closed_trades") or []
    body += "<h2>Closed paper trades today</h2><div class=card>%s</div>" % _table(
        ["entered", "closed", "contracts", "exit reason", "gross INR", "net INR"],
        [[_hhmmss(c.get("entry_time")), _hhmmss(c.get("closed_at")),
          esc(" / ".join("%s %s" % (leg.get("side"), leg.get("contract")) for leg in c.get("legs", []))),
          esc(c.get("exit_reason")), _money(c.get("gross_pnl_inr")), _money(c.get("net_pnl_inr"))] for c in closed],
        (4, 5))

    events = (st.get("events") or [])[-15:][::-1]
    if events:
        body += "<h2>Recent events</h2><div class=card>%s</div>" % _table(
            ["time", "event"], [[_hhmmss(e.get("at")), esc(e.get("text"))] for e in events])
    name = st.get("journal_name")
    if journal_dir and name and name in list_journals(journal_dir):
        body += "<p><a href='/journal?name=%s'>Full journal of this session</a></p>" % esc(name)
    lim = st.get("limits") or {}
    window = lim.get("entry_window") or ["-", "-"]
    body += ("<p class=mute>Limits: risk per trade INR %s &middot; max daily loss INR %s &middot; flat by %s &middot; "
             "entries %s-%s IST. Fills are simulated with adverse slippage and costs. Realised statistics on the "
             "journal pages stay hidden until 30 completed real paper trades; the figures here are a live view only.</p>" % (
                 esc(lim.get("risk_per_trade_inr")), esc(lim.get("max_daily_loss_inr")), esc(lim.get("flat_by_ist")),
                 esc(window[0]), esc(window[1])))
    return body


def render_live(state_path, journal_dir=None, now=None):
    """The full Live page: the fragment plus one small script that re-fetches the fragment every second (GET only). With
    scripts off, a plain meta refresh does the same more slowly."""
    inner = live_fragment(state_path, journal_dir, now)
    body = ("<div id=conn class=alert hidden>Lost the connection to the dashboard server. The figures below are not "
            "updating.</div><div id=live>%s</div>%s" % (inner, LIVE_SCRIPT))
    return page("Live paper session", body, head_extra='<noscript><meta http-equiv="refresh" content="3"></noscript>')


def route(folder, raw_path, live_state=None):
    """Return (status, html text). Pure function, so it is tested without opening a socket."""
    url = urlparse(raw_path)
    if url.path == "/":
        return 200, render_index(folder)
    if url.path == "/live":
        if "fragment" in parse_qs(url.query):
            return 200, live_fragment(live_state or DEFAULT_LIVE_STATE, folder)
        return 200, render_live(live_state or DEFAULT_LIVE_STATE, folder)
    if url.path == "/journal":
        name = (parse_qs(url.query).get("name") or [""])[0]
        out = render_journal(folder, name)
        if out is not None:
            return 200, out
    return 404, page("Not found", "<h1>Not found</h1><p><a href='/'>Journals</a></p>")


def make_handler(folder, live_state=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperDashboard"

        def do_GET(self):
            try:
                status, text = route(folder, self.path, live_state)
            except Exception as e:                      # never crash the page on one bad journal
                status, text = 500, page("Error", "<h1>Could not build this page</h1><pre>%s</pre>" % esc(e))
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _refuse(self):
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()
        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

        def log_message(self, fmt, *args):
            pass
    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only local dashboard for paper-trading journals.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--journal-dir", default=DEFAULT_DIR)
    p.add_argument("--live-state", default=DEFAULT_LIVE_STATE, help="state file written by nifty_live.live_run")
    args = p.parse_args(argv)
    server = ThreadingHTTPServer((BIND_HOST, args.port), make_handler(os.path.abspath(args.journal_dir), args.live_state))
    print("%s\nServing journals from %s at http://%s:%d  (Ctrl+C to stop)" % (
        NOTICE, args.journal_dir, BIND_HOST, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
