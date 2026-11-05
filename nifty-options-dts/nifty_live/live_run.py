"""Live PAPER trading run: poll Upstox read-only, record every snapshot, paper-trade on it, feed the local dashboard.

    python -m nifty_live.live_run --config my_real_config.json --dashboard      # during market hours
    python -m nifty_live.live_run answer FINGERPRINT yes                         # reply to the ticket you were shown
    python -m nifty_live.live_run --replay data/recordings/2026-11-05 --config my_real_config.json --dashboard
    python -m nifty_live.live_run --replay bullish_trend --dashboard             # offline preview on a fixture

Then open http://127.0.0.1:8765/live

* Quotes come from the read-only Upstox Analytics token (env UPSTOX_ANALYTICS_TOKEN). Nothing is ever sent anywhere
  else: fills are SIMULATED, there is no order path, and the dashboard cannot approve anything.
* Do not run the plain recorder at the same time: this command records the same files itself.
* A ticket is entered only after you reply "yes" to that exact ticket, and only while its approval is fresh
  (config data_stale_after_seconds, 60 s by default). An unanswered ticket is released.
* Decisions and exits are made once per five-minute slot (as in a replay); the dashboard's unrealised P&L refreshes at
  every poll. If this program stops while a paper position is open, that position is not managed any more.
PAPER trading only. Not financial advice.
"""
import argparse
import collections
import csv
import datetime as dt
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from nifty_core import config as config_mod                                    # noqa: E402
from nifty_core import pipeline                                                # noqa: E402
from nifty_core.data.fixture_provider import load_scenario_file, parse_scenario   # noqa: E402
from nifty_core.errors import JournalError, NiftyError                                       # noqa: E402
from nifty_core.journal import journal as journal_mod                          # noqa: E402
from nifty_core.live_session import LiveSession                                # noqa: E402
from nifty_live import recorder                                                # noqa: E402
from nifty_live import upstox_reader as ur                                     # noqa: E402
from tools import csv_to_scenario                                              # noqa: E402

TRADER_DIR = os.path.join(PROJECT, "trader")
LIVE_DIR = os.path.join(TRADER_DIR, "live")
STATE_PATH = os.path.join(LIVE_DIR, "state.json")
ANSWERS_PATH = os.path.join(LIVE_DIR, "answers.jsonl")
JOURNAL_DIR = os.path.join(TRADER_DIR, "journal")
FIXTURE_DIR = os.path.join(PROJECT, "tests", "fixtures", "scenarios")
DEFAULT_CONFIG = os.path.join(PROJECT, "config", "config.example.json")
KEEP_CHAINS = 3                      # the pipeline reads only the newest snapshot; a few are kept for safety
LIVE_MIN_POLL_SECONDS = 5            # fastest live poll; the quote-staleness limit is 60 s, so 5-10 s is comfortable
HEX16 = re.compile(r"^[0-9a-f]{16}$")
ENTRY_MODE_TEXT = {
    "auto": ("ENTRY MODE: AUTOMATIC. A setup that passes every rule and fits the risk limits is entered as a PAPER trade "
             "without asking you, and exits are automatic too. Create the file trader/KILL to stop new entries."),
    "manual": "ENTRY MODE: MANUAL. Every entry waits for your reply: python -m nifty_live.live_run answer <fingerprint> yes"}


# ---------------------------------------------------------------- shared files
def write_state(view, path=STATE_PATH):
    """Replace the dashboard state file in one step. A reader never sees a half-written file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(view, fh, default=str)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return True
        except OSError:                                  # the dashboard has the file open for a moment: try again
            time.sleep(0.05)
    return False


def read_state(path=STATE_PATH):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


class AnswerInbox(object):
    """Replies written by `live_run answer`, read once each. Replies that existed before this run started are ignored."""

    def __init__(self, path=ANSWERS_PATH):
        self.path = path
        self._offset = os.path.getsize(path) if os.path.exists(path) else 0

    def new(self):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "rb") as fh:
            fh.seek(self._offset)
            data = fh.read()
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self._offset += end + 1
        for line in data[:end + 1].decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
                out.append((str(row["fingerprint"]), str(row["reply"])))
            except (ValueError, KeyError, TypeError):
                continue
        return out


def cmd_answer(argv):
    p = argparse.ArgumentParser(prog="live_run answer", description="Reply to ONE waiting paper ticket, word for word.")
    p.add_argument("fingerprint", help="the 16-character fingerprint printed with the ticket")
    p.add_argument("reply", nargs="+", help='your reply; only one that starts with "yes" counts')
    p.add_argument("--answers-file", default=ANSWERS_PATH)
    p.add_argument("--state-file", default=STATE_PATH)
    a = p.parse_args(argv)
    fp = a.fingerprint.strip().lower()
    if not HEX16.match(fp):
        sys.exit("error: the fingerprint is 16 hex characters, as printed on the ticket (got %r)" % a.fingerprint)
    state = read_state(a.state_file)
    waiting = [t["fingerprint"] for t in (state or {}).get("pending_tickets", [])]
    if fp not in waiting:
        print("note: no ticket with this fingerprint is waiting right now (waiting: %s). Your reply is still recorded, "
              "and it will not enter anything unless that ticket is still open." % (", ".join(waiting) or "none"))
    os.makedirs(os.path.dirname(os.path.abspath(a.answers_file)), exist_ok=True)
    with open(a.answers_file, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"fingerprint": fp, "reply": " ".join(a.reply),
                             "written_at": recorder.now_ist().isoformat()}) + "\n")
    print("reply recorded for ticket %s. The running session applies it within a second if the ticket is still fresh." % fp)
    return 0


# ---------------------------------------------------------------- data
def _text(value):
    value = (value or "").strip()
    return value if value != "" else None


def _whole(value):
    value = _text(value)
    return None if value is None else int(value)


def chain_doc(stamp, spot, rows):
    """One snapshot as the scenario format wants it (the same text-to-field rules as tools/csv_to_scenario.py)."""
    return {"snapshot_time": stamp, "spot": _text(spot), "quotes": [{
        "expiry": _text(r["expiry"]), "strike": _whole(str(r["strike"])), "type": _text(r["type"]),
        "bid": _text(r["bid"]), "ask": _text(r["ask"]), "ltp": _text(r["ltp"]), "bid_qty": _whole(r["bid_qty"]),
        "ask_qty": _whole(r["ask_qty"]), "volume": _whole(r["volume"]), "open_interest": _whole(r["open_interest"]),
        "timestamp": stamp} for r in rows]}


def read_candles(out_dir):
    path = os.path.join(out_dir, "candles.csv")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return [{"start": _text(r["start"]), "open": _text(r["open"]), "high": _text(r["high"]), "low": _text(r["low"]),
                 "close": _text(r["close"]), "volume": _whole(r.get("volume"))} for r in csv.DictReader(fh)]


def build_live_scenario(out_dir, day, ctx, recent):
    """Validated scenario from today's candles file and the newest snapshots kept IN MEMORY by the loop (`recent`).
    Nothing grows with the day, so polling every few seconds stays cheap; chain.csv still holds everything for a replay."""
    doc = {"schema_version": 1, "scenario": "nifty_live_%s" % day, "description": "live session, newest snapshots",
           "underlying": "NIFTY", "date": day.isoformat(), "interval_minutes": 5, "data_label": "RECORDED_REAL",
           "candles": read_candles(out_dir), "chains": list(recent)[-KEEP_CHAINS:], "events": [], "metadata": {}}
    return parse_scenario(doc, ctx.specs, ctx.calendar, ctx.data_mode)


def ensure_candles_file(out_dir):
    path = os.path.join(out_dir, "candles.csv")
    if not os.path.exists(path):
        os.makedirs(out_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(",".join(recorder.CANDLE_FIELDS) + "\n")


def scenario_from_recording(folder, day, ctx):
    doc = csv_to_scenario.build(day.isoformat(), "recorded_%s" % day, os.path.join(folder, "candles.csv"),
                                os.path.join(folder, "chain.csv"))
    return parse_scenario(doc, ctx.specs, ctx.calendar, ctx.data_mode)


def _scenario_path(arg):
    if os.path.exists(arg):
        return arg
    cand = os.path.join(FIXTURE_DIR, arg if arg.endswith(".json") else arg + ".json")
    if os.path.exists(cand):
        return cand
    raise SystemExit("error: no scenario file or recording folder %r" % arg)


# ---------------------------------------------------------------- dashboard thread
def start_dashboard(port, journal_dir, state_path):
    from http.server import ThreadingHTTPServer
    from tools import dashboard
    server = ThreadingHTTPServer((dashboard.BIND_HOST, port), dashboard.make_handler(os.path.abspath(journal_dir), state_path))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("dashboard: http://%s:%d/live   (read-only; this computer only)" % (dashboard.BIND_HOST, port))
    return server


# ---------------------------------------------------------------- the loop
class Runner(object):
    """Glue between a clock, a source of data and the session. Used by both the live loop and the replay."""

    def __init__(self, ctx, day, journal, mode, state_path=STATE_PATH, inbox=None, out=print, auto_entry=False):
        self.session = LiveSession(ctx, TRADER_DIR, journal, day, auto_entry=auto_entry)
        self.mode, self.state_path, self.inbox, self.out = mode, state_path, inbox or AnswerInbox(), out
        self._announced = set()
        self.problem = None
        self.poll_seconds = None

    def publish(self, now):
        view = self.session.snapshot(now)
        view["mode"] = self.mode
        view["written_at"] = recorder.now_ist().isoformat()
        view["problem"] = self.problem
        view["poll_seconds"] = self.poll_seconds
        return write_state(view, self.state_path)

    def announce_tickets(self, now):
        runner = self.session.runner
        for fp, p in ((runner.pending if runner else None) or {}).items():
            if fp in self._announced:
                continue
            self._announced.add(fp)
            window = self.session.ctx.cfg.data_stale_after_seconds
            self.out("\n" + p.ticket.text)
            self.out("  >>> To approve exactly this paper order within %d s, in another terminal:\n"
                     "      python -m nifty_live.live_run answer %s yes\n" % (window, fp))

    def apply_replies(self, now):
        for fp, reply in self.inbox.new():
            status = self.session.answer(fp, reply, now)
            self.out("%s reply for %s -> %s" % (now.strftime("%H:%M:%S"), fp, status))
            self.publish(now)

    def step(self, now, scenario):
        for line in self.session.step(now, scenario):
            self.out("%s %s" % (now.strftime("%H:%M:%S"), line))
        self.announce_tickets(now)
        self.publish(now)

    def finish(self, now):
        summary = self.session.finish(now)
        self.publish(now)
        if summary is not None:
            self.out("session finished: entries %d | completed paper trades %d | critical exceptions %d | "
                     "realised net P&L INR %s" % (summary.entries, summary.exits, summary.criticals, summary.net_pnl_inr))
        return summary


def run_live(a):
    cfg = config_mod.load_config(a.config)
    if cfg.data_mode != "real_readonly":
        sys.exit('error: live quotes are real data, so the config needs "data_mode": "real_readonly" (for example '
                 "my_real_config.json). Use --replay to preview with synthetic fixtures.")
    try:
        token = ur.token_from_env()
    except ur.UpstoxReadError as e:
        sys.exit("%s.\nOpen a NEW terminal after `setx`, or in this one run:\n  $env:%s = "
                 "[Environment]::GetEnvironmentVariable('%s','User')" % (e, ur.TOKEN_ENV, ur.TOKEN_ENV))
    t = recorder.now_ist()
    day = t.date()
    try:
        ctx = pipeline.load_context(cfg, cfg.data_mode, a.profile)
    except NiftyError as e:
        sys.exit("refused: %s" % e)
    if ctx.calendar.session(day) is None:
        sys.exit("%s is not an NSE trading day. Use --replay to preview." % day)
    if not recorder.in_session(t):
        sys.exit("The market is closed (%s IST). Start between 09:15 and 15:30 on a trading day, or use --replay."
                 % t.strftime("%a %H:%M"))
    try:
        expiry = recorder.resolve_expiry(token, a.expiry)
    except ur.UpstoxReadError as e:
        sys.exit("could not choose an expiry: %s" % e)
    out_dir = a.out_dir or os.path.join(PROJECT, "data", "recordings", day.isoformat())
    journal = journal_mod.Journal(os.path.join(a.journal_dir, "live_%s.jsonl" % day.isoformat()), "real",
                                  "live-%s-%s" % (day.isoformat(), t.strftime("%H%M%S")))
    runner = Runner(ctx, day, journal, "LIVE", a.state_file, auto_entry=a.entry_mode == "auto")
    print("PAPER trading only: real Upstox quotes (read-only), SIMULATED fills, expiry %s. Recording to %s.\n"
          "Journal: %s. Ctrl+C stops the session." % (expiry, out_dir, journal.path))
    print(ENTRY_MODE_TEXT[a.entry_mode])
    server = start_dashboard(a.port, a.journal_dir, a.state_file) if a.dashboard else None
    interval = max(a.interval, LIVE_MIN_POLL_SECONDS)
    runner.poll_seconds = interval
    recent = collections.deque(maxlen=KEEP_CHAINS)
    failures = 0
    last_candles_slot = None
    try:
        while True:
            t = recorder.now_ist()
            try:
                recorder.record_snapshot(token, out_dir, t, expiry, a.window,
                                         sink=lambda stamp, spot, rows: recent.append(chain_doc(stamp, spot, rows)))
                failures, runner.problem = 0, None
            except ur.UpstoxReadError as e:
                failures += 1
                runner.problem = "snapshot failed (%d in a row): %s" % (failures, e)
                print("%s %s" % (t.strftime("%H:%M:%S"), runner.problem))
                if e.status in (401, 403) or failures >= 10:
                    print("stopping: the token was refused or the service keeps failing")
                    break
            due = [s for s in runner.session.slots if s <= t]
            if (due and due[-1] != last_candles_slot) or not os.path.exists(os.path.join(out_dir, "candles.csv")):
                last_candles_slot = due[-1] if due else None
                try:
                    recorder.write_candles(token, out_dir, t)
                except ur.UpstoxReadError as e:
                    runner.problem = "candles failed: %s" % e
                    print("%s %s" % (t.strftime("%H:%M:%S"), runner.problem))
                ensure_candles_file(out_dir)
            try:
                runner.step(t, build_live_scenario(out_dir, day, ctx, recent))
            except JournalError as e:
                if runner.session.runner is None:                   # the session could not even open: retrying is pointless
                    print("%s the journal refused this run: %s" % (t.strftime("%H:%M:%S"), e))
                    break
                runner.problem = "journal problem, nothing decided this poll: %s" % e
                print("%s %s" % (t.strftime("%H:%M:%S"), runner.problem))
                runner.publish(t)
            except NiftyError as e:
                runner.problem = "data problem, nothing decided this poll: %s" % e
                print("%s %s" % (t.strftime("%H:%M:%S"), runner.problem))
                runner.publish(t)
            if not recorder.in_session(recorder.now_ist()):
                break
            wake = time.monotonic() + interval
            while time.monotonic() < wake:                  # replies are applied within a second, not at the next poll
                time.sleep(0.5)
                runner.apply_replies(recorder.now_ist())
    except KeyboardInterrupt:
        print("stopped by you")
    t = recorder.now_ist()
    if runner.session.runner is not None and runner.session.runner.pm.active() and t.time() < recorder.CLOSE:
        print("WARNING: a paper position is still open and is no longer being managed.")
    runner.finish(t)
    if server is not None:
        print("The dashboard stays up. Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


def replay_times(scenario, ctx, day):
    """Simulated clock for a replay: every recorded snapshot plus every five-minute slot, in order."""
    snaps = [c.snapshot_time for c in scenario.chains]
    first, last = min(snaps), max(snaps)
    times = set(snaps)
    times.update(pipeline.tick_times(ctx, day))
    # a partial recording stops one slot after its last snapshot instead of running on to the close
    return sorted(x for x in times if first <= x <= last + dt.timedelta(minutes=pipeline.TICK_MINUTES))


def run_replay(a):
    cfg = config_mod.load_config(a.config)
    ctx = pipeline.load_context(cfg, cfg.data_mode, a.profile)
    src = a.replay
    if os.path.isdir(src):
        day = dt.date.fromisoformat(os.path.basename(os.path.normpath(src)))
        scenario = scenario_from_recording(src, day, ctx)
    else:
        scenario = load_scenario_file(_scenario_path(src), ctx.specs, ctx.calendar, ctx.data_mode)
        day = scenario.day
    synthetic = scenario.data_label == "FIXTURE_SYNTHETIC"
    if synthetic != (cfg.data_mode == "fixture"):
        sys.exit("error: the data is labelled %s but the config data_mode is %r (use \"fixture\" for synthetic "
                 "fixtures and \"real_readonly\" for recorded real data)" % (scenario.data_label, cfg.data_mode))
    if not scenario.chains:
        sys.exit("error: %s holds no option-chain snapshots" % src)
    source = journal_mod.data_source_for_label(scenario.data_label)
    stamp = dt.datetime.now().strftime("%H%M%S")
    journal = journal_mod.Journal(os.path.join(a.journal_dir, "liveview_%s_%s.jsonl" % (scenario.name, stamp)),
                                  source, "liveview-%s-%s" % (scenario.name, stamp))
    runner = Runner(ctx, day, journal, "REPLAY", a.state_file, auto_entry=a.entry_mode == "auto")
    print(ENTRY_MODE_TEXT[a.entry_mode])
    print("REPLAY of %s on a simulated clock (%s). Journal: %s" % (scenario.name, scenario.data_label, journal.path))
    server = start_dashboard(a.port, a.journal_dir, a.state_file) if a.dashboard else None
    times = replay_times(scenario, ctx, day)
    last = times[-1]
    try:
        for now in times:
            last = now
            if a.until and now.strftime("%H:%M") > a.until:
                break
            runner.step(now, scenario)
            wake = time.monotonic() + a.speed
            while time.monotonic() < wake:
                time.sleep(min(0.25, max(a.speed, 0.01)))
                runner.apply_replies(now)
        runner.finish(last)
    except KeyboardInterrupt:
        print("stopped by you")
    if server is not None:
        print("Replay finished. The dashboard stays up. Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="Live PAPER trading on read-only Upstox quotes, with a local dashboard. "
                                "Simulated fills only; nothing is ever sent to a broker.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="config JSON (live needs data_mode real_readonly)")
    p.add_argument("--profile", default="intraday", help="entry-rule profile in config/profiles/: intraday (default: "
                   "opening-range breakout entered on a retest), human (votes on trend, looser), or default (the strict "
                   "reference thresholds)")
    p.add_argument("--entry-mode", choices=("auto", "manual"), default="auto",
                   help="auto (default): paper entries and exits are automatic; manual: each entry waits for your yes")
    p.add_argument("--interval", type=int, default=10, help="seconds between live polls (default 10, minimum %d)" %
                   LIVE_MIN_POLL_SECONDS)
    p.add_argument("--expiry", default="auto", help="auto, a keyword such as next_week, or YYYY-MM-DD")
    p.add_argument("--window", type=int, default=1000, help="keep strikes within this many points of spot")
    p.add_argument("--out-dir", default=None, help="recording folder (default data/recordings/<date>)")
    p.add_argument("--journal-dir", default=JOURNAL_DIR)
    p.add_argument("--state-file", default=STATE_PATH)
    p.add_argument("--dashboard", action="store_true", help="also serve the read-only dashboard from this process")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--replay", default=None, metavar="FIXTURE_OR_RECORDING",
                   help="preview on a simulated clock: a fixture name, a scenario JSON, or data/recordings/<date>")
    p.add_argument("--speed", type=float, default=3.0, help="replay only: seconds per simulated step")
    p.add_argument("--until", default=None, metavar="HH:MM", help="replay only: stop at this simulated time")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "answer":
        return cmd_answer(argv[1:])
    a = build_parser().parse_args(argv)
    try:
        return run_replay(a) if a.replay else run_live(a)
    except NiftyError as e:
        sys.exit("refused: %s" % e)


if __name__ == "__main__":
    sys.exit(main())
