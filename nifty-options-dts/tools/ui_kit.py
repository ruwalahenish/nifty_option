"""Small UI toolkit for the paper-trading dashboard: design tokens (light and dark), the page shell and reusable components.

Plain HTML and CSS, no scripts, no external fonts or images, so it works offline, on the Render deploy and with scripts blocked.
Every status is shown with a word AND an icon, never colour alone. PAPER trading only. Not financial advice.
"""
import html
from decimal import Decimal

NOTICE = "PAPER TRADING ONLY. Simulated fills. Not financial advice. This page cannot place or approve anything."

CSS = """
:root{color-scheme:light dark;
--bg:#f3f5f9;--surface:#ffffff;--surface2:#f6f8fb;--ink:#0f172a;--mute:#5b6b80;--line:#e3e8ef;--accent:#2563eb;--accent-bg:#e8f0fe;
--good:#15803d;--good-bg:#e3f6ea;--bad:#b42318;--bad-bg:#fdecea;--warn:#9a6700;--warn-bg:#fff4d6;--info:#1d4ed8;--info-bg:#e8f0fe;
--card:var(--surface);--r:14px;--shadow:0 1px 2px rgba(16,24,40,.05),0 6px 18px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0a0e16;--surface:#131a27;--surface2:#0f1520;--ink:#e8edf6;--mute:#9aa7bb;--line:#243146;--accent:#7aa7ff;--accent-bg:#16233d;
--good:#4cd48a;--good-bg:#10281c;--bad:#ff8a80;--bad-bg:#32151a;--warn:#f0c15a;--warn-bg:#2f2610;--info:#8db4ff;--info-bg:#16233d;
--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.28)}}
*{box-sizing:border-box}[hidden]{display:none!important}html{scroll-behavior:smooth;scroll-padding-top:70px}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Inter,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1120px;margin:0 auto;padding:0 16px 40px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:24px;margin:22px 0 8px;letter-spacing:-.01em}
h2{font-size:17px;margin:0 0 12px;letter-spacing:-.005em}h3{font-size:13px;margin:16px 0 8px;color:var(--mute);text-transform:uppercase;letter-spacing:.06em;font-weight:700}
.mute{color:var(--mute)}.pos{color:var(--good)}.neg{color:var(--bad)}.warn{color:var(--warn)}.fine{font-size:12px;color:var(--mute)}
.num{font-variant-numeric:tabular-nums}
.banner{margin:0 -16px;padding:6px 16px;font-size:12px;color:var(--mute);background:var(--surface2);border-bottom:1px solid var(--line)}
/* top bar */
.topbar{position:sticky;top:0;z-index:30;margin:0 -16px;padding:10px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;
background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:saturate(1.4) blur(10px);border-bottom:1px solid var(--line)}
.tb-brand{display:flex;align-items:center;gap:10px;font-weight:700}.tb-brand small{font-weight:500;color:var(--mute)}
.tb-nav{display:flex;gap:4px;overflow-x:auto;flex:1;min-width:0;scrollbar-width:none}.tb-nav::-webkit-scrollbar{display:none}
.tb-nav a{padding:5px 11px;border-radius:999px;color:var(--mute);font-size:13px;white-space:nowrap}.tb-nav a:hover{background:var(--accent-bg);color:var(--accent);text-decoration:none}
.tb-right{display:flex;align-items:center;gap:10px;margin-left:auto;font-size:12px;color:var(--mute)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--good);margin-right:6px;animation:pulse 1.4s infinite}
.dot.off{background:var(--bad);animation:none}@keyframes pulse{50%{opacity:.3}}
@media (prefers-reduced-motion:reduce){.dot{animation:none}html{scroll-behavior:auto}}
/* chips and badges */
.chip{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:999px;border:1px solid var(--line);font-size:12px;color:var(--mute);background:var(--surface)}
.chip.live{border-color:var(--good);color:var(--good);background:var(--good-bg)}.chip.replay{border-color:var(--warn);color:var(--warn);background:var(--warn-bg)}
.badge{display:inline-flex;align-items:center;gap:7px;padding:3px 11px;border-radius:999px;font-weight:700;font-size:13px;border:1px solid transparent;white-space:nowrap}
.badge i{font-style:normal;font-size:12px}.badge.big{font-size:16px;padding:6px 16px}
.badge.ok{background:var(--good-bg);color:var(--good);border-color:color-mix(in srgb,var(--good) 35%,transparent)}
.badge.warn{background:var(--warn-bg);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 35%,transparent)}
.badge.bad{background:var(--bad-bg);color:var(--bad);border-color:color-mix(in srgb,var(--bad) 35%,transparent)}
.badge.info{background:var(--info-bg);color:var(--info);border-color:color-mix(in srgb,var(--info) 35%,transparent)}
.badge.mute{background:var(--surface2);color:var(--mute);border-color:var(--line)}
/* cards and sections */
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;margin:0 0 14px;box-shadow:var(--shadow)}
.card.flat{box-shadow:none;background:var(--surface2)}.card.accent{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-bg),var(--shadow)}
section{margin:22px 0 0}.sec-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin:0 0 10px}.sec-head h2{margin:0}.sec-head .sub{color:var(--mute);font-size:13px}
.two{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:14px;align-items:start}.two>.card{margin:0}
.two>*,.hero>*,.strats>*,.kpis>*,.facts>*,.tiles>*{min-width:0}.card{min-width:0}
@media (max-width:860px){.two{grid-template-columns:minmax(0,1fr)}}
.hero{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(0,1fr);gap:14px;align-items:stretch}.hero>.card{margin:0}
@media (max-width:860px){.hero{grid-template-columns:minmax(0,1fr)}}
.hero-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.asof{color:var(--mute);font-size:13px}
.lead{font-size:18px;line-height:1.45;margin:12px 0 14px;font-weight:600;letter-spacing:-.005em;overflow-wrap:anywhere}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));gap:10px;margin-top:14px}
.fact{background:var(--surface2);border:1px solid var(--line);border-radius:12px;padding:9px 12px}.fact>span{display:block;font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
.fact b{display:block;font-size:15px;margin-top:2px}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-content:start}.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px;box-shadow:var(--shadow)}
.kpi.wide{grid-column:1/-1}.kpi>span{display:block;font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}
.kpi b{display:block;font-size:24px;line-height:1.25;margin-top:2px;font-variant-numeric:tabular-nums}.kpi small{display:block;color:var(--mute);font-size:12px;margin-top:2px}
/* stepper */
.stepper{display:flex;gap:0;margin:6px 0 4px;padding:0;list-style:none}.stepper li{flex:1;position:relative;text-align:center;font-size:12px;color:var(--mute);padding-top:30px}
.stepper li::before{content:"";position:absolute;left:50%;top:13px;width:100%;height:3px;background:var(--line);z-index:0}.stepper li:last-child::before{display:none}
.stepper li b{position:absolute;left:50%;top:0;transform:translateX(-50%);width:28px;height:28px;border-radius:50%;display:grid;place-items:center;background:var(--surface);
border:2px solid var(--line);color:var(--mute);font-size:13px;z-index:1}
.stepper li.done{color:var(--ink)}.stepper li.done b{background:var(--good);border-color:var(--good);color:#fff}.stepper li.done::before{background:var(--good)}
.stepper li.now{color:var(--ink);font-weight:700}.stepper li.now b{border-color:var(--accent);color:var(--accent);box-shadow:0 0 0 4px var(--accent-bg)}
.stepper li.now.blocked b{border-color:var(--bad);color:var(--bad);box-shadow:0 0 0 4px var(--bad-bg)}.stepper li.now.idle b{border-color:var(--line);color:var(--mute);box-shadow:none}
/* alerts */
.alert{display:flex;gap:10px;align-items:flex-start;border-radius:12px;padding:10px 14px;margin:12px 0 0;border:1px solid var(--bad);background:var(--bad-bg);color:var(--ink)}
.alert b{color:var(--bad)}.alert.warn{border-color:var(--warn);background:var(--warn-bg)}.alert.warn b{color:var(--warn)}.alert .ic{font-weight:800;color:var(--bad)}
/* checklist */
.cl{list-style:none;margin:0;padding:0}.cl li{display:grid;grid-template-columns:112px minmax(0,1fr);gap:4px 12px;padding:9px 0;border-top:1px solid var(--line);overflow-wrap:anywhere}.cl li:first-child{border-top:0}
.cl b{font-weight:600}.cl .detail{display:block;color:var(--mute);font-size:13px;margin-top:1px}
.pill{display:inline-flex;align-items:center;justify-content:center;gap:5px;height:24px;padding:0 9px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.04em;align-self:start;justify-self:start}
.pill.PASSED{background:var(--good-bg);color:var(--good)}.pill.FAILED,.pill.ERROR{background:var(--bad-bg);color:var(--bad)}
.pill.PENDING{background:var(--warn-bg);color:var(--warn)}.pill.NA,.pill.NOT{background:var(--surface2);color:var(--mute);border:1px solid var(--line)}
.cl li.plain{grid-template-columns:1fr}
@media (max-width:560px){.cl li{grid-template-columns:1fr}}
.meta-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0 0;font-size:13px;color:var(--mute)}
/* strategy cards */
.strats{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr));gap:12px}.strat{margin:0;padding:14px 16px}
.strat.sel{border-color:var(--good);box-shadow:0 0 0 3px var(--good-bg),var(--shadow)}.strat.blk{border-color:var(--warn)}
.strat h3{margin:0 0 6px;font-size:15px;color:var(--ink);text-transform:none;letter-spacing:0}.strat p{margin:8px 0 0;font-size:13px;color:var(--mute)}
.bar{height:8px;border-radius:999px;background:var(--line);overflow:hidden;margin:8px 0 2px}.bar i{display:block;height:100%;background:var(--accent);border-radius:999px}
.bar.ok i{background:var(--good)}.bar.warn i{background:var(--warn)}
.seg{display:flex;height:8px;border-radius:999px;overflow:hidden;background:var(--line);margin-top:6px}.seg i{display:block;height:100%}
.seg .p{background:var(--good)}.seg .f{background:var(--bad)}.seg .w{background:var(--warn)}
/* entry */
.legs{width:100%;border-collapse:collapse;font-size:13px}.legs th{color:var(--mute);font-size:11px;text-transform:uppercase;letter-spacing:.05em;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
.legs td{padding:8px;border-bottom:1px solid var(--line)}.legs .r{text-align:right;font-variant-numeric:tabular-nums}
.side{display:inline-block;min-width:44px;text-align:center;padding:1px 7px;border-radius:6px;font-size:11px;font-weight:800}.side.BUY{background:var(--good-bg);color:var(--good)}.side.SELL{background:var(--bad-bg);color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(130px,100%),1fr));gap:10px;margin:12px 0}.tile{border:1px solid var(--line);border-radius:12px;padding:9px 12px;background:var(--surface2)}
.tile>span{display:block;font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.05em}.tile b{display:block;font-size:17px;margin-top:2px;font-variant-numeric:tabular-nums}.tile small{display:block;margin-top:3px}
.tile.good{border-color:color-mix(in srgb,var(--good) 40%,transparent);background:var(--good-bg)}.tile.bad{border-color:color-mix(in srgb,var(--bad) 40%,transparent);background:var(--bad-bg)}
.empty{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border:1px dashed var(--line);border-radius:12px;background:var(--surface2)}
.empty .ic{font-size:22px;line-height:1}.empty b{display:block}
/* folds */
.fold{border:1px solid var(--line);border-radius:12px;margin:10px 0 0;background:var(--surface)}.fold>summary{cursor:pointer;list-style:none;padding:10px 14px;display:flex;justify-content:space-between;gap:10px;font-weight:600;font-size:14px}
.fold>summary::-webkit-details-marker{display:none}.fold>summary::before{content:"\\25B8";color:var(--mute);margin-right:8px;transition:transform .15s}.fold[open]>summary::before{transform:rotate(90deg)}
.fold>summary span{flex:1}.fold>summary em{font-style:normal;color:var(--mute);font-weight:500;font-size:13px}.fold-body{padding:2px 14px 12px}
.fold>summary:focus-visible,.tb-nav a:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:8px}
/* tables */
.tbl{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:hover td,tr:hover>td{background:var(--surface2)}tr.atm td{font-weight:700;background:var(--surface2)}tr.held td{border-bottom:2px solid var(--accent)}
.tag{font-size:10px;border:1px solid var(--accent);color:var(--accent);border-radius:8px;padding:0 5px;margin-left:4px}
.hist td{font-size:13px}.hist .why{max-width:330px;color:var(--mute)}.hist .sub{display:block;color:var(--mute);font-size:12px}
pre{white-space:pre-wrap;background:var(--surface2);border:1px solid var(--line);border-radius:10px;padding:10px;font-size:12px;margin:8px 0;overflow-x:auto}
svg{width:100%;height:auto;display:block}.stat b{display:block;font-size:20px}.stat span{color:var(--mute);font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.grid .card{margin:0}.big{font-size:26px}
.stat b span{font-size:inherit;color:inherit}.stat b .pos{color:var(--good)}.stat b .neg{color:var(--bad)}
.legend{display:flex;flex-wrap:wrap;gap:8px 16px;margin-top:10px}.legend span{font-size:12px;color:var(--mute)}.legend .pill{height:20px;font-size:10px;margin-right:6px}
@media (max-width:560px){main{padding:0 12px 32px}.banner,.topbar{margin:0 -12px;padding-left:12px;padding-right:12px}.lead{font-size:16px}.kpi b{font-size:20px}.topbar{gap:8px}}
"""


def esc(x):
    return html.escape("" if x is None else str(x))


def money(value):
    d = Decimal(str(value))
    cls = "pos" if d > 0 else "neg" if d < 0 else ""
    return '<span class="%s">%s</span>' % (cls, esc("{:,.2f}".format(d)))


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


def _table(headers, rows, num=()):
    if not rows:
        return "<p class=mute>none</p>"
    head = "".join("<th%s>%s</th>" % (" class=n" if i in num else "", esc(h)) for i, h in enumerate(headers))
    body = "".join("<tr>%s</tr>" % "".join("<td%s>%s</td>" % (" class=n" if i in num else "", c)
                                           for i, c in enumerate(r)) for r in rows)
    return "<div class=tbl><table><tr>%s</tr>%s</table></div>" % (head, body)


def page(title, body, refresh=None, head_extra=""):
    meta = ('<meta http-equiv="refresh" content="%d">' % refresh if refresh else "") + head_extra
    return ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            "initial-scale=1'><meta name=color-scheme content='light dark'>%s<title>%s</title><style>%s</style></head><body><main>"
            "<div class=banner>%s</div>%s</main></body></html>" % (meta, esc(title), CSS, esc(NOTICE), body))


# ---------------------------------------------------------------- components
ICON = {"ok": "✓", "warn": "◷", "bad": "!", "info": "●", "mute": "–"}      # check, clock, !, dot, dash
PILL = {"PASSED": ("PASSED", "✓"), "FAILED": ("FAILED", "✕"), "ERROR": ("ERROR", "!"), "PENDING": ("PENDING", "…"),
        "N/A": ("N/A", "–"), "NOT CONFIRMED": ("NOT CONFIRMED", "○")}
PILL_CLASS = {"PASSED": "PASSED", "FAILED": "FAILED", "ERROR": "ERROR", "PENDING": "PENDING", "N/A": "NA", "NOT CONFIRMED": "NOT"}


def badge(text, tone="mute", big=False, title=""):
    return "<span class='badge %s%s'%s><i aria-hidden=true>%s</i>%s</span>" % (
        tone, " big" if big else "", (" title='%s'" % esc(title)) if title else "", ICON.get(tone, ""), esc(text))


def pill(status):
    label, icon = PILL.get(status, (status, ""))
    return "<span class='pill %s'><span aria-hidden=true>%s</span>%s</span>" % (PILL_CLASS.get(status, "NA"), icon, esc(label))


def bar(done, total, tone=""):
    pct = 0 if not total else max(0, min(100, int(100 * done / total)))
    return "<div class='bar %s' role=progressbar aria-valuemin=0 aria-valuemax=%d aria-valuenow=%d><i style='width:%d%%'></i></div>" % (
        tone, total, done, pct)


def segbar(passed, failed, pending):
    total = passed + failed + pending
    if not total:
        return ""
    return "<div class=seg aria-hidden=true>%s</div>" % "".join(
        "<i class=%s style='width:%.1f%%'></i>" % (c, 100.0 * n / total) for c, n in (("p", passed), ("f", failed), ("w", pending)) if n)


def fold(key, title, inner, open_=False, meta=""):
    """A collapsible section. `key` lets the live page keep it open or closed across its one-second refresh."""
    return "<details class=fold data-k='%s'%s><summary><span>%s</span><em>%s</em></summary><div class=fold-body>%s</div></details>" % (
        esc(key), " open" if open_ else "", esc(title), esc(meta), inner)


def stepper(progress):
    """Signal -> confirmation -> risk check -> executed, with where the decision is now."""
    if not progress:
        return ""
    cur, state = progress.get("current", 0), progress.get("state", "waiting")
    items = []
    for i, name in enumerate(progress.get("steps", [])):
        if i < cur:
            cls, mark = "done", "✓"
        elif i == cur:
            cls, mark = "now %s" % state, ("!" if state == "blocked" else str(i + 1))
        else:
            cls, mark = "", str(i + 1)
        items.append("<li class='%s'><b aria-hidden=true>%s</b>%s</li>" % (cls, mark, esc(name)))
    return "<ol class=stepper aria-label='Progress of this decision'>%s</ol>" % "".join(items)


def kpi(label, value, sub="", wide=False):
    return "<div class='kpi%s'><span>%s</span><b>%s</b>%s</div>" % (
        " wide" if wide else "", esc(label), value, ("<small>%s</small>" % sub) if sub else "")
