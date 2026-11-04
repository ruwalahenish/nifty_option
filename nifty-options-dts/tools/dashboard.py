"""Local, READ-ONLY dashboard for the paper-trading journals.

    python tools/dashboard.py            # then open http://127.0.0.1:8765
    python tools/dashboard.py --port 9000 --journal-dir trader/journal

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
import html
import os
import sys
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from nifty_core.journal import journal as journal_mod    # noqa: E402
from nifty_core.journal import report as report_mod      # noqa: E402

DEFAULT_DIR = os.path.join(PROJECT, "trader", "journal")
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
"""


def esc(x):
    return html.escape("" if x is None else str(x))


def page(title, body, refresh=None):
    meta = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ""
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
    body = "<h1>Paper-trading journals</h1>"
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


def route(folder, raw_path):
    """Return (status, html text). Pure function, so it is tested without opening a socket."""
    url = urlparse(raw_path)
    if url.path == "/":
        return 200, render_index(folder)
    if url.path == "/journal":
        name = (parse_qs(url.query).get("name") or [""])[0]
        out = render_journal(folder, name)
        if out is not None:
            return 200, out
    return 404, page("Not found", "<h1>Not found</h1><p><a href='/'>Journals</a></p>")


def make_handler(folder):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperDashboard"

        def do_GET(self):
            try:
                status, text = route(folder, self.path)
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
    args = p.parse_args(argv)
    server = HTTPServer((BIND_HOST, args.port), make_handler(os.path.abspath(args.journal_dir)))
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
