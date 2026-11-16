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
# The shell, the components, the charts and the Live page live in their own modules; the names are re-exported here.
from tools.charts import LEVEL_STYLE, _chain_table, _epoch, equity_svg, gauge_svg, spot_svg      # noqa: E402,F401
from tools.live_ui import (HEX16, LIVE_SCRIPT, MAX_STATE_BYTES, STALE_AFTER_SECONDS, _history_card, _legs_table,     # noqa: E402,F401
                           _open_position_card, _readiness_card, _selector_card, _setup_rules_card, live_fragment,
                           load_live_state, render_live)
from tools.ui_kit import CSS, NOTICE, _dec, _hhmmss, _money, _table, esc, money, page          # noqa: E402,F401

DEFAULT_DIR = os.path.join(PROJECT, "trader", "journal")
DEFAULT_LIVE_STATE = os.path.join(PROJECT, "trader", "live", "state.json")
BIND_HOST = "127.0.0.1"


def bind_host():
    """Loopback unless DASHBOARD_HOST is set (cloud deploy). A non-loopback host REQUIRES DASHBOARD_PASSWORD."""
    host = os.environ.get("DASHBOARD_HOST", BIND_HOST)
    if host != BIND_HOST and not os.environ.get("DASHBOARD_PASSWORD"):
        raise SystemExit("error: DASHBOARD_HOST=%s needs DASHBOARD_PASSWORD set (the page shows your positions)" % host)
    return host


def _authorized(header):
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if not pw:
        return True
    import base64, hmac
    try:
        user_pass = base64.b64decode((header or "")[6:]).decode("utf-8") if (header or "").startswith("Basic ") else ""
    except Exception:
        return False
    return hmac.compare_digest(user_pass.partition(":")[2].encode(), pw.encode())


def list_journals(folder):
    """Names of the .jsonl files directly inside folder (no sub-folders)."""
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if n.endswith(".jsonl") and os.path.isfile(os.path.join(folder, n)))


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


def api_route(raw_path, live_state=None):
    """Structured decision details as JSON: (status, text). Reads the live state file the runner writes; nothing is computed here."""
    url = urlparse(raw_path)
    if url.path not in ("/api/trade-readiness", "/api/decision-history"):
        return 404, json.dumps({"error": "unknown endpoint"})
    st = load_live_state(live_state or DEFAULT_LIVE_STATE)
    if st is None:
        return 404, json.dumps({"error": "no live session is writing the state file"})
    d = st.get("last_decision") or {}
    if url.path == "/api/decision-history":
        return 200, json.dumps({"day": st.get("day"), "decision_history": st.get("decision_history") or []})
    return 200, json.dumps({"kind": "TRADE_READINESS", "paper": True, "day": st.get("day"), "updated_at": st.get("updated_at"),
                            "session_status": st.get("status"), "decision_time": d.get("decision_time"),
                            "decision_status": d.get("status"), "next_slot": (st.get("slots") or {}).get("next"),
                            "readiness": d.get("readiness"), "legend": st.get("readiness_legend")})


def make_handler(folder, live_state=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PaperDashboard"

        def do_GET(self):
            if not _authorized(self.headers.get("Authorization")):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="paper dashboard"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ctype = "text/html; charset=utf-8"
            try:
                if self.path.startswith("/api/"):
                    status, text = api_route(self.path, live_state)
                    ctype = "application/json; charset=utf-8"
                else:
                    status, text = route(folder, self.path, live_state)
            except Exception as e:                      # never crash the page on one bad journal
                status, text = 500, page("Error", "<h1>Could not build this page</h1><pre>%s</pre>" % esc(e))
                ctype = "text/html; charset=utf-8"
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
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
    server = ThreadingHTTPServer((bind_host(), args.port), make_handler(os.path.abspath(args.journal_dir), args.live_state))
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
