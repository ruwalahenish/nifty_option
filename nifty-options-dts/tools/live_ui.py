"""The Live page of the paper-trading dashboard: what the system is doing, why, and what it is waiting for.

Layout, top to bottom
    top bar      sticky: mode, data freshness, the readiness status (always visible) and in-page navigation
    overview     the readiness status, the next action in one sentence, a progress stepper, key facts and the day's numbers
    position     an open paper position with its unrealised P&L, or the ticket waiting for a human yes
    readiness    the checklist (market conditions, execution steps, optional confirmations) beside the expected entry
    strategies   the four strategies side by side
    history      every decision of the day
    market       the price chart, the option chain, closed trades and events (the last three fold away)

This module only RENDERS what the backend decided (nifty_core.selector.readiness and the live session). It computes no trading
logic. The page is swapped in place every second by a small script that keeps collapsible sections as the reader left them.
PAPER trading only. Not financial advice.
"""
import datetime
import json
import os
import re

from tools import charts
from tools.ui_kit import (_dec, _hhmmss, _money, _table, badge, bar, esc, fold, kpi, money, page, pill, segbar, stepper)

MAX_STATE_BYTES = 8 * 1024 * 1024
HEX16 = re.compile(r"^[0-9a-f]{16}$")
STALE_AFTER_SECONDS = 120

STATUS_TONE = {"TRADE EXECUTED": "ok", "READY TO EXECUTE": "ok", "READY FOR RISK VALIDATION": "info",
               "WAITING FOR CONFIRMATION": "warn", "WAITING FOR SIGNAL": "info", "NO VALID STRATEGY": "mute",
               "EXECUTION BLOCKED": "bad"}
STATE_TONE = {"SELECTED": "ok", "QUALIFIES": "info", "BLOCKED": "warn", "NOT YET": "mute"}
DIRECTION = {"BULLISH": ("▲", "pos"), "BEARISH": ("▼", "neg"), "NEUTRAL": ("◆", "")}

LIVE_SCRIPT = """<script>
(function () {
  var box = document.getElementById('live'), conn = document.getElementById('conn'), bad = 0, last = box.innerHTML;
  // The page is replaced every second. Keep each collapsible section as the reader left it, and do not touch the page when
  // nothing changed, so text selection and scrolling are never disturbed.
  function opened() { var o = {}; box.querySelectorAll('details[data-k]').forEach(function (d) { o[d.getAttribute('data-k')] = d.open; }); return o; }
  function tick() {
    if (document.hidden) { return; }
    fetch('/live?fragment=1', {cache: 'no-store'})
      .then(function (r) { if (!r.ok) { throw new Error(r.status); } return r.text(); })
      .then(function (t) {
        bad = 0; conn.hidden = true;
        if (t === last) { return; }
        var keep = opened(), y = window.scrollY;
        box.innerHTML = t; last = t;
        box.querySelectorAll('details[data-k]').forEach(function (d) { var k = d.getAttribute('data-k'); if (k in keep) { d.open = keep[k]; } });
        window.scrollTo(0, y);
      })
      .catch(function () { bad += 1; if (bad >= 3) { conn.hidden = false; } });
  }
  setInterval(tick, 1000);
})();
</script>"""


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


def _cap(text):
    text = str(text or "")
    return text[:1].upper() + text[1:]


def _status_badge(status, big=False):
    return badge(status or "-", STATUS_TONE.get(status, "mute"), big=big)


def _direction(text):
    word = str(text or "")
    for key, (icon, cls) in DIRECTION.items():
        if word.startswith(key):
            return "<span class='%s'>%s %s</span>" % (cls, icon, esc(word))
    return "<span class=mute>%s</span>" % esc(word or "-")


# ---------------------------------------------------------------- checklist, entry, strategies
def _rows(rows):
    if not rows:
        return "<p class=mute>none</p>"
    return "<ul class=cl>%s</ul>" % "".join(
        "<li>%s<div><b>%s</b><span class=detail>%s</span></div></li>" % (pill(r.get("status")), esc(_cap(r.get("name"))), esc(r.get("detail")))
        for r in rows)


def _checklist(ready):
    rows = ready.get("checklist") or []

    def group(r):
        return r.get("group") or ("market" if r.get("required") else "extra")
    top = [r for r in rows if group(r) in ("gate", "market")]
    steps = [r for r in rows if group(r) == "execution"]
    extras = [r for r in rows if group(r) == "extra"]
    countable = [r for r in extras if r.get("status") != "N/A"]
    met = sum(1 for r in countable if r.get("status") == "PASSED")
    out = "<h3>Market and data conditions</h3>" + _rows(top)
    out += "<h3>Execution steps</h3>" + _rows(steps)
    if extras:
        out += fold("extras", "Optional confirmations", _rows(extras), meta="%d of %d met" % (met, len(countable)))
    return out


def _entry_card(ready):
    entry = ready.get("entry")
    if not entry:
        return ""
    reg = ready.get("regime") or {}
    out = "<h2>Expected entry</h2>"
    if not entry.get("available"):
        out += ("<div class=empty><span class=ic aria-hidden=true>⊘</span><div><b>%s</b><span class=mute>%s</span></div></div>" % (
            esc(entry.get("label")), esc(entry.get("reason"))))
        zones = entry.get("watch") or []
        if zones:
            out += "<h3>Where price has to be</h3>" + charts.band_svg(zones, reg.get("spot"), reg.get("day_low"), reg.get("day_high"))
            out += "<ul class=cl>%s</ul>" % "".join(
                "<li class=plain><div><b>%s</b><span class=detail>%s</span></div></li>" % (esc(z.get("label")), esc(z.get("rule")))
                for z in zones)
        for key in ("note", "timeframe"):
            if entry.get(key):
                out += "<p class=fine>%s</p>" % esc(entry[key])
        return out
    net = entry.get("net") or {}
    worst, limit = _dec(entry.get("worst_case_inr")), _dec(entry.get("risk_limit_inr"))
    out += "<p class=mute>%s &middot; priced at %s &middot; expiry %s</p>" % (esc(entry.get("label")), _hhmmss(entry.get("priced_at")), esc(entry.get("expiry")))
    legs = "".join(
        "<tr><td><span class='side %s'>%s</span></td><td>%s</td><td class=r>%s %s</td><td class=r>%s</td><td class=r>%s</td><td class=r>%s</td></tr>" % (
            esc(l.get("action")), esc(l.get("action")), esc(l.get("contract")), esc(l.get("strike")), esc(l.get("type")),
            esc(l.get("price")), esc(l.get("bid")), esc(l.get("ask"))) for l in entry.get("legs", []))
    out += ("<div class=tbl><table class=legs><tr><th>Action</th><th>Contract</th><th class=r>Strike</th><th class=r>Premium</th>"
            "<th class=r>Bid</th><th class=r>Ask</th></tr>%s</table></div>" % legs)
    out += ("<div class=tiles>"
            "<div class=tile><span>NIFTY at the decision</span><b>%s</b></div>"
            "<div class=tile><span>Net %s</span><b>%s pts</b><small class=mute>INR %s</small></div>"
            "<div class=tile><span>Reward-to-risk</span><b>%s</b></div>"
            "<div class=tile><span>Worst case / limit</span><b>%s</b><small class=mute>of INR %s</small>%s</div></div>" % (
                esc(entry.get("underlying")), esc(net.get("kind")), esc(net.get("points")), esc(net.get("inr")),
                esc(entry.get("reward_to_risk")), esc(entry.get("worst_case_inr")), esc(entry.get("risk_limit_inr")),
                bar(int(worst), int(limit), "ok" if worst <= limit * 7 // 10 else "warn") if worst is not None and limit else ""))
    out += ("<div class=tiles><div class='tile bad'><span>Stop-loss</span><small>%s</small></div>"
            "<div class='tile good'><span>Target</span><small>%s</small></div></div>" % (esc(entry.get("stop_loss")), esc(entry.get("target"))))
    out += "<p class=fine><b>Time exit:</b> %s</p>" % esc(entry.get("time_exit"))
    if entry.get("trigger"):
        out += fold("trigger", "Entry trigger: what must hold", "<ol>%s</ol>" % "".join("<li>%s</li>" % esc(t) for t in entry["trigger"]),
                    meta="%d conditions" % len(entry["trigger"]))
    for key in ("timeframe", "note"):
        if entry.get(key):
            out += "<p class=fine>%s</p>" % esc(entry[key])
    return out


def _strategies_section(ready):
    rows = ready.get("strategies") or []
    if not rows:
        return ""
    cards = []
    for s in rows:
        state = s.get("state") or ""
        key = next((k for k in STATE_TONE if state.startswith(k)), "NOT YET")
        tone = STATE_TONE[key]
        cls = " sel" if key == "SELECTED" else " blk" if key == "BLOCKED" else ""
        total, done = s.get("required_total") or 0, s.get("required_passed") or 0
        first = s.get("first_failed")
        label, _, rest = state.partition(" (")                     # "QUALIFIES (below the minimum confidence)": badge + small print
        cards.append(
            "<div class='card strat%s'><h3>%s</h3>%s%s%s<p class=fine>%s of %s required conditions met%s</p><p>%s</p></div>" % (
                cls, esc(s.get("strategy")), badge(label, tone), ("<span class=fine> %s</span>" % esc(rest.rstrip(")"))) if rest else "",
                bar(done, total, "ok" if total and done == total else ""),
                esc(done), esc(total), (" &middot; confidence %s" % esc(s.get("confidence"))) if s.get("confidence") is not None else "",
                esc("%s: %s" % (_cap(first["name"]), first["detail"])) if first else "Every required condition holds."))
    return ("<section id=strategies><div class=sec-head><h2>All four strategies</h2><span class=sub>how close each one is to qualifying</span></div>"
            "<div class=strats>%s</div></section>" % "".join(cards))


def _legend(legend):
    if not legend:
        return ""
    items = "".join("<span>%s%s</span>" % (pill(k), esc(v)) for k, v in legend.items())
    return fold("legend", "What the statuses mean", "<div class=legend>%s</div>" % items)


def _hero_card(ready, next_check):
    reg = ready.get("regime") or {}
    counts = ready.get("counts") or {}
    status = ready.get("status")
    asof = "as of %s" % _hhmmss(reg.get("as_of")) + (" &middot; next check %s" % _hhmmss(next_check) if next_check else "")
    p, f, w = counts.get("PASSED", 0), counts.get("FAILED", 0) + counts.get("ERROR", 0), counts.get("PENDING", 0)
    main = ready.get("next_action_main") or ready.get("next_action")
    fine = ready.get("disclaimer") or ""
    out = "<div class='card%s'>" % (" accent" if status in ("READY TO EXECUTE", "TRADE EXECUTED") else "")
    out += "<div class=hero-head><span class=fine style='text-transform:uppercase;letter-spacing:.06em'>Trade Readiness</span>%s<span class=asof>%s</span></div>" % (
        _status_badge(status, big=True), asof)
    out += "<p class=lead><span class=mute style='font-weight:600'>Next:</span> %s</p>" % esc(main)
    out += stepper(ready.get("progress"))
    out += ("<div class=facts>"
            "<div class=fact><span>Market regime</span><b>%s</b></div>"
            "<div class=fact><span>%s</span><b>%s</b></div>"
            "<div class=fact><span>Direction</span><b>%s</b></div>"
            "<div class=fact><span>Conditions</span><b>%d passed &middot; %d failed &middot; %d pending</b>%s</div></div>" % (
                esc(_cap((reg.get("structure") or "-").replace("_", " ").lower())),
                "Selected strategy" if ready.get("evaluated_selected") else "Closest to qualifying",
                esc(ready.get("evaluated_strategy") or "-"), _direction(ready.get("direction")), p, f, w, segbar(p, f, w)))
    if reg.get("summary"):
        out += "<p class='fine' style='margin-top:12px'>%s</p>" % esc(reg["summary"])
    if fine:
        out += "<p class=fine>%s</p>" % esc(fine)
    return out + "</div>"


def _readiness_section(ready, legend):
    return ("<section id=readiness><div class=sec-head><h2>What the system is waiting for</h2><span class=sub>Checklist: %s &middot; %s</span></div>"
            "<div class=two><div class=card><h2>Checklist: %s</h2>%s</div><div class=card>%s</div></div>%s</section>" % (
                esc(ready.get("evaluated_strategy") or "-"),
                "selected" if ready.get("evaluated_selected") else "closest to qualifying",
                esc(ready.get("evaluated_strategy") or "-"), _checklist(ready), _entry_card(ready), _legend(legend)))


def _readiness_card(ready, legend=None, next_check=None):
    """Hero, checklist, expected entry and the four strategies as one block (the live page lays them out separately)."""
    if not ready:
        return ""
    return _hero_card(ready, next_check) + _readiness_section(ready, legend) + _strategies_section(ready)


def _technical_alerts(ready):
    out = ""
    for issue in (ready or {}).get("technical") or []:
        tone = "" if issue.get("kind") in ("DATA", "EXPIRY", "SAFETY") else " warn"
        out += "<div class='alert%s'><span class=ic aria-hidden=true>!</span><div><b>%s</b> &mdash; %s</div></div>" % (
            tone, esc(issue.get("kind")), esc(issue.get("text")))
    return out


# ---------------------------------------------------------------- history
def _history_card(history):
    """Every decision of the day: what was evaluated, the price, what held, why not, whether it later became valid, whether it traded."""
    if not history:
        return ""

    def row(h):
        later = ("yes, at %s" % _hhmmss(h["later_valid_at"])) if h.get("later_valid_at") else "-"
        expected = ", ".join(x for x in (h.get("expected") or []) if x)
        failed = ("not met: " + ", ".join(h["failed_names"])) if h.get("failed_names") else ""
        return ("<tr><td class=num>%s</td><td><b>%s</b><span class=sub>%s%s</span></td><td>%s</td><td class='n'>%s</td>"
                "<td>%s passed<span class=sub>%s</span></td><td class=why>%s</td><td>%s</td><td>%s</td></tr>" % (
                    _hhmmss(h.get("at")), esc(h.get("strategy") or "-"), esc(_cap((h.get("regime") or "-").replace("_", " ").lower())),
                    (" &middot; " + esc(expected)) if expected else "", _status_badge(h.get("status")), esc(h.get("price") or "-"),
                    esc(h.get("passed")), esc(failed), esc(h.get("reason")), esc(later),
                    "<b class=pos>yes</b>" if h.get("traded") else "no"))
    head = ("<tr><th>Time</th><th>Strategy evaluated</th><th>Readiness</th><th class=n>NIFTY</th><th>Conditions</th><th>Why</th>"
            "<th>Later valid?</th><th>Traded</th></tr>")
    ordered = list(history)[::-1]
    recent, older = ordered[:8], ordered[8:30]
    table = "<div class=tbl><table class=hist>%s%s</table></div>" % (head, "".join(row(h) for h in recent))
    if older:
        table += fold("history-more", "Earlier decisions", "<div class=tbl><table class=hist>%s%s</table></div>" % (
            head, "".join(row(h) for h in older)), meta="%d more" % len(older))
    return ("<section id=history><div class=sec-head><h2>Decision history</h2><span class=sub>%d decision%s today &middot; newest first</span></div>"
            "<div class=card>%s</div></section>" % (len(history), "" if len(history) == 1 else "s", table))


# ---------------------------------------------------------------- older cards (profiles without readiness)
def _setup_rules_card(setup):
    """The opening-range rules behind the latest decision, one row per rule with its value, its limit and whether it
    passed, so a breakout the system did not trade can be explained at a glance. Empty when there is nothing to show."""
    checks = (setup or {}).get("checks") or []
    if not checks:
        return ""
    rows = "".join("<li>%s<div><b>%s</b><span class=detail>value %s &middot; needs %s</span></div></li>" % (
        pill("PASSED" if c.get("ok") else "FAILED"), esc(_cap(str(c.get("rule") or "").replace("_", " "))), esc(c.get("value")),
        esc(c.get("limit"))) for c in checks)
    side = "long (up-breakout)" if (setup or {}).get("side") == "BULL" else "short (down-breakout)"
    return "<section><div class=sec-head><h2>Setup rules (%s)</h2></div><div class=card><ul class=cl>%s</ul></div></section>" % (esc(side), rows)


def _selector_card(sel):
    """How the adaptive selector judged each of the four strategies (used when a decision carries no readiness block)."""
    if not sel or not sel.get("evaluations"):
        return ""
    rows = []
    for ev in sel["evaluations"]:
        chosen = ev.get("strategy") == sel.get("selected_title")
        failed = next((c for c in ev.get("conditions", []) if c.get("required") and not c.get("passed")), None)
        status = ("<b>SELECTED</b>, confidence %s" % esc(ev.get("confidence"))) if chosen else (
            "eligible, confidence %s" % esc(ev.get("confidence")) if ev.get("eligible") else "not eligible")
        rows.append([esc(ev.get("strategy")), status, "" if failed is None else "%s: %s" % (esc(failed.get("name")), esc(failed.get("detail")))])
    head = "<p>%s</p>" % esc(sel.get("market")) if sel.get("market") else ""
    wait = sel.get("awaiting_confirmation")
    if wait:
        head += "<p class=mute>Pick %s of %s consecutive slots needed before entry.</p>" % (esc(wait.get("have")), esc(wait.get("need")))
    return ("<section><div class=sec-head><h2>Strategy selector</h2></div><div class=card>%s%s</div></section>" % (
        head, _table(["strategy", "status", "first thing that failed"], rows)))


# ---------------------------------------------------------------- position, ticket, market
def _legs_table(legs):
    rows = [[esc(leg.get("contract")), "<span class='side %s'>%s</span>" % (esc(leg.get("side")), esc(leg.get("side"))), esc(leg.get("units")),
             esc(leg.get("entry")), esc(leg.get("bid") or "-"), esc(leg.get("ask") or "-"), esc(leg.get("mark") or "-"),
             _money(leg.get("leg_gross_inr"))] for leg in legs]
    return _table(["contract", "side", "units", "paper entry", "bid", "ask", "exit mark", "leg P&L INR"], rows, (2, 3, 4, 5, 6, 7))


def _open_position_card(pos):
    u = pos.get("unrealized")
    body = "<div class=hero-head><b style='font-size:16px'>%s</b>%s<span class=asof>%s lot(s) &middot; entered %s &middot; defined risk INR %s</span></div>" % (
        esc(pos.get("strategy")), badge(pos.get("state"), "info"), esc(pos.get("lots")), _hhmmss(pos.get("entry_time")), esc(pos.get("defined_risk_inr")))
    body += _legs_table(pos.get("legs", []))
    if u:
        body += ("<div class=tiles>"
                 "<div class=tile><span>Unrealised gross</span><b>%s</b></div>"
                 "<div class=tile><span>Entry costs paid</span><b>%s</b></div>"
                 "<div class=tile><span>Exit costs (estimate)</span><b>%s</b></div>"
                 "<div class=tile><span>UNREALISED net P&amp;L (simulated)</span><b>%s</b></div></div>" % (
                     _money(u.get("gross_pnl_inr")), esc(u.get("actual_entry_costs_inr")), esc(u.get("estimated_exit_costs_inr")),
                     _money(u.get("net_pnl_inr"))))
        body += charts.gauge_svg(u.get("net_pnl_inr"), pos.get("stop_at_net_inr"), pos.get("target_at_net_inr"))
    else:
        body += "<div class='alert warn'><span class=ic aria-hidden=true>!</span><div>Mark unavailable right now: %s</div></div>" % esc(
            "; ".join(pos.get("mark_problems") or ["no quote"]))
    if pos.get("exit_reason"):
        waiting = " &mdash; waiting: " + esc(", ".join(pos["pending_reasons"])) if pos.get("pending_reasons") else ""
        body += "<p class=fine>Exit decided: %s (attempt %s)%s</p>" % (esc(pos.get("exit_reason")), esc(pos.get("exit_attempts")), waiting)
    return "<div class=card>%s</div>" % body


def _ticket_cards(st):
    out = ""
    for t in st.get("pending_tickets") or []:
        fp = str(t.get("fingerprint", ""))
        cmd = ("python -m nifty_live.live_run answer %s yes" % fp) if HEX16.match(fp) else "(fingerprint missing)"
        out += ("<div class='card accent'><h2>Ticket waiting for your reply</h2><pre>%s</pre>"
                "<p>Valid until <b>%s</b> (%s s after it was made); after that it is released and nothing is entered. "
                "To approve exactly this paper order, run in a terminal:</p><pre>%s</pre>"
                "<p class=fine>This page cannot approve anything.</p></div>" % (
                    esc(t.get("text")), _hhmmss(t.get("expires_at")), esc(st.get("approval_window_seconds")), esc(cmd)))
    return out


def _chart_card(st):
    price = charts.spot_svg(st.get("spot_series"), st.get("candle_closes"), st.get("levels"))
    if not price:
        return ""
    return "<div class=card><h2>NIFTY today%s</h2>%s</div>" % (
        ", with the opening range and breakout triggers" if st.get("levels") else "", price)


def _market_section(st):
    out = _chart_card(st)
    if st.get("chain_view"):
        out += fold("chain", "Option chain around the money (live quotes)", charts._chain_table(st["chain_view"]),
                    meta="expiry %s" % esc((st["chain_view"] or {}).get("expiry")))
    pts = []
    for row in st.get("curve") or []:
        v = _dec(row[1]) if isinstance(row, list) and len(row) == 2 else None
        if v is not None:
            pts.append((row[0], v))
    if len(pts) >= 2 and any(v != 0 for _, v in pts):
        pts = pts[::max(1, len(pts) // 600)]
        out += "<div class=card><h2>Today's P&amp;L (realised + unrealised)</h2>%s</div>" % charts.equity_svg(pts)
    closed = st.get("closed_trades") or []
    out += fold("closed", "Closed paper trades today", _table(
        ["entered", "closed", "contracts", "exit reason", "gross INR", "net INR"],
        [[_hhmmss(c.get("entry_time")), _hhmmss(c.get("closed_at")),
          esc(" / ".join("%s %s" % (leg.get("side"), leg.get("contract")) for leg in c.get("legs", []))),
          esc(c.get("exit_reason")), _money(c.get("gross_pnl_inr")), _money(c.get("net_pnl_inr"))] for c in closed], (4, 5)),
        open_=bool(closed), meta="%d trade%s" % (len(closed), "" if len(closed) == 1 else "s"))
    events = (st.get("events") or [])[-15:][::-1]
    if events:
        out += fold("events", "Recent events", _table(["time", "event"], [[_hhmmss(e.get("at")), esc(e.get("text"))] for e in events]),
                    meta="latest %d" % len(events))
    return "<section id=market><div class=sec-head><h2>Market and activity</h2></div>%s</section>" % out


# ---------------------------------------------------------------- the page
def _empty_state(state_path):
    return ("<div class=card><h2>No live session yet</h2><p class=mute>No live session is writing <code>%s</code>. Start one during market hours:</p>"
            "<pre>python -m nifty_live.live_run --config my_real_config.json --dashboard</pre>"
            "<p class=mute>or preview it any time on a simulated clock:</p><pre>python -m nifty_live.live_run --replay "
            "data/recordings/YYYY-MM-DD --config my_real_config.json --dashboard</pre></div>" % esc(state_path))


def live_fragment(state_path, journal_dir=None, now=None):
    """The inside of the Live page. The page swaps it in place every second, so the figures move without a reload."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    st = load_live_state(state_path)
    if st is None:
        return "<h1>Live paper session</h1>" + _empty_state(state_path)
    mode = st.get("mode", "LIVE")
    chips = ("<span class='chip live'>LIVE quotes</span>" if mode == "LIVE" else "<span class='chip replay'>REPLAY &middot; simulated clock</span>")
    if st.get("entry_mode") == "AUTO":
        chips += " <span class='chip live'>AUTO paper entry &amp; exit</span>"
    elif st.get("entry_mode") == "MANUAL":
        chips += " <span class=chip>entries need your yes</span>"
    try:
        age = (now - datetime.datetime.fromisoformat(st["written_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        age = None
    stale = mode == "LIVE" and st.get("status") != "FINISHED" and (age is None or age > STALE_AFTER_SECONDS)
    poll = st.get("poll_seconds")
    fresh = mode != "LIVE" or (age is not None and age <= max(4 * (poll or 30), 20))
    d = st.get("last_decision") or {}
    ready = d.get("readiness")
    nxt = (st.get("slots") or {}).get("next")
    market, totals = st.get("market") or {}, st.get("totals")

    nav = "".join("<a href='#%s'>%s</a>" % (i, label) for i, label in (
        ("overview", "Overview"), ("position", "Position"), ("readiness", "Readiness"), ("strategies", "Strategies"),
        ("history", "History"), ("market", "Market")) if i != "strategies" or ready)
    topbar = ("<div class=topbar><div class=tb-brand><a href='/' title='All journals' aria-label='All journals'>&larr;</a> NIFTY paper trader</div>"
              "<nav class=tb-nav aria-label='Sections'>%s</nav><div class=tb-right><span><span class='dot%s'></span>%s</span>%s</div></div>"
              "<div class=meta-row>%s<span>%s &middot; %s &middot; market time %s</span></div>" % (
                  nav, "" if fresh else " off",
                  "replay clock" if mode != "LIVE" else (("updated %s s ago" % int(age) + (" &middot; polling every %s s" % poll if poll else "")) if age is not None else "updating"),
                  _status_badge(ready.get("status")) if ready else "", chips, esc(st.get("label")), esc(st.get("day")), _hhmmss(st.get("updated_at"))))
    body = topbar
    if stale:
        body += ("<div class=alert><span class=ic aria-hidden=true>!</span><div><b>NOT UPDATING.</b> The live runner has not written for %s. "
                 "It may have stopped; everything below is the last known state and is not current.</div></div>" % (
                     "a while" if age is None else "%d s" % age))
    if st.get("problem"):
        body += "<div class='alert warn'><span class=ic aria-hidden=true>!</span><div>%s</div></div>" % esc(st["problem"])
    if st.get("kill_switch"):
        body += "<div class=alert><span class=ic aria-hidden=true>!</span><div><b>KILL switch is on.</b> No new paper entry will be made.</div></div>"
    body += _technical_alerts(ready)

    tiles = [kpi("NIFTY spot", "<span class=num>%s</span>" % esc(market.get("spot") or "-"),
                 "quote age %s s" % esc(market.get("data_age_seconds")) if market else "", wide=True)]
    if totals:
        day_total = totals.get("day_total_inr")
        tiles.append(kpi("Day P&L", _money(day_total) if day_total is not None else "<span class=mute>n/a</span>",
                         "realised %s &middot; unrealised %s" % (_money(totals.get("realised_net_inr")), _money(totals.get("unrealised_net_inr")))))
        tiles.append(kpi("Trades today", esc("%s / %s" % (totals.get("trades_today"), (st.get("limits") or {}).get("max_trades_per_day"))),
                         "%s closed" % esc(totals.get("completed_trades"))))
    kpis = "<div class=kpis>%s</div>" % "".join(tiles)

    if ready:
        body += "<section id=overview><div class=hero>%s%s</div></section>" % (_hero_card(ready, nxt), kpis)
    else:
        decision = ""
        if d:
            why = "<p class=mute>%s</p>" % esc(" | ".join(d.get("reasons") or [])) if d.get("reasons") else ""
            decision = ("<div class=card><div class=hero-head><span class=fine style='text-transform:uppercase;letter-spacing:.06em'>Latest decision</span>%s"
                        "<span class=asof>at %s &middot; regime %s &middot; strategy %s</span></div>%s</div>" % (
                            _status_badge(d.get("status")), _hhmmss(d.get("decision_time")), esc(d.get("regime")), esc(d.get("strategy_outcome")), why))
        else:
            decision = "<div class=card><span class=mute>No decision yet. The first one is made at the first slot (%s).</span></div>" % _hhmmss((st.get("slots") or {}).get("first"))
        body += "<section id=overview><div class=hero>%s%s</div></section>" % (decision, kpis)
    body += "<p class=fine style='margin:10px 0 0'>%s</p>" % esc(st.get("note"))

    opens = st.get("open_positions") or []
    body += "<section id=position><div class=sec-head><h2>Open paper position</h2></div>%s%s</section>" % (
        _ticket_cards(st), "".join(_open_position_card(p) for p in opens) if opens else
        "<div class='card flat'><span class=mute>No open position. A trade appears here, with its live unrealised P&amp;L, as soon as one is entered.</span></div>")
    if ready:
        body += _readiness_section(ready, st.get("readiness_legend"))
        body += _strategies_section(ready)
    else:
        body += _setup_rules_card(d.get("setup")) + _selector_card(d.get("selector"))
    body += _history_card(st.get("decision_history"))
    body += _market_section(st)

    name = st.get("journal_name")
    if journal_dir and name and name in _journals(journal_dir):
        body += "<p><a href='/journal?name=%s'>Full journal of this session</a></p>" % esc(name)
    lim = st.get("limits") or {}
    window = lim.get("entry_window") or ["-", "-"]
    body += ("<p class=fine>Limits: risk per trade INR %s &middot; max daily loss INR %s &middot; flat by %s &middot; entries %s&ndash;%s IST. "
             "Fills are simulated with adverse slippage and costs. Realised statistics on the journal pages stay hidden until 30 completed real "
             "paper trades; the figures here are a live view only. This page updates every second.</p>" % (
                 esc(lim.get("risk_per_trade_inr")), esc(lim.get("max_daily_loss_inr")), esc(lim.get("flat_by_ist")), esc(window[0]), esc(window[1])))
    return body


def _journals(folder):
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if n.endswith(".jsonl") and os.path.isfile(os.path.join(folder, n)))


def render_live(state_path, journal_dir=None, now=None):
    """The full Live page: the fragment plus one small script that re-fetches the fragment every second (GET only). With
    scripts off, a plain meta refresh does the same more slowly."""
    inner = live_fragment(state_path, journal_dir, now)
    body = ("<div id=conn class='alert' hidden><span class=ic aria-hidden=true>!</span><div>Lost the connection to the dashboard server. "
            "The figures below are not updating.</div></div><div id=live>%s</div>%s" % (inner, LIVE_SCRIPT))
    return page("Live paper session", body, head_extra='<noscript><meta http-equiv="refresh" content="3"></noscript>')
