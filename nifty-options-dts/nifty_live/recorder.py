"""Record NIFTY option-chain snapshots and index candles to files, then convert them into a scenario file.

    python -m nifty_live.recorder                     # during market hours: poll until 15:30 IST, then build the file
    python -m nifty_live.recorder --once --out-dir probe_out   # one snapshot, for a connectivity check

It records and nothing else: no approval, no paper entry, nothing is sent anywhere. The output feeds the existing
offline pipeline (`check-data`, `run-day`, `tools/backtest.py`, the dashboard). Needs env var UPSTOX_ANALYTICS_TOKEN.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from nifty_live import upstox_reader as ur        # noqa: E402
from tools import csv_to_scenario                 # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OPEN, CLOSE = dt.time(9, 15), dt.time(15, 30)
MIN_POLL_SECONDS = 15                              # the quote-staleness limit is 60 s; stay well inside it
CHAIN_FIELDS = csv_to_scenario.CHAIN_COLUMNS
CANDLE_FIELDS = csv_to_scenario.CANDLE_COLUMNS


def now_ist():
    return dt.datetime.now(IST)


def in_session(t):
    return t.weekday() < 5 and OPEN <= t.time() <= CLOSE


AUTO_EXPIRY_ORDER = ("current_week", "next_week", "current_month")


def resolve_expiry(token, expiry, transport=None):
    """Turn "auto" or a keyword into one explicit expiry date (YYYY-MM-DD) that actually has quotes, so every snapshot
    of a session is for the same expiry. An explicit date is returned unchanged."""
    if len(expiry) == 10 and expiry[4] == "-":
        return expiry
    for key in (AUTO_EXPIRY_ORDER if expiry == "auto" else (expiry,)):
        try:
            _, rows = ur.get_option_chain(token, key, transport=transport)
        except ur.UpstoxReadError as e:
            if e.status is not None:
                raise
            continue                                  # empty chain for this keyword: try the next one
        return rows[0]["expiry"]
    raise ur.UpstoxReadError("no option chain with quotes was found for %r" % expiry)


def record_snapshot(token, out_dir, t, expiry, window, transport=None, sink=None):
    """Append one chain snapshot (strikes within +-window points of spot) to chain.csv. Returns the row count.
    `sink(stamp, spot, rows)`, when given, also receives the rows that were written (the live session keeps them in
    memory instead of re-reading a file that grows all day)."""
    spot, rows = ur.get_option_chain(token, expiry, transport=transport)
    lo, hi = float(spot) - window, float(spot) + window
    keep = [r for r in rows if lo <= r["strike"] <= hi]
    if not keep:
        raise ur.UpstoxReadError("no strikes within %s points of spot %s" % (window, spot))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "chain.csv")
    new = not os.path.exists(path)
    stamp = t.replace(microsecond=0).isoformat()
    with open(path, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CHAIN_FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        for r in keep:
            w.writerow(dict(r, snapshot_time=stamp, spot=spot, timestamp=stamp))
    if sink is not None:
        sink(stamp, spot, keep)
    return len(keep)


def write_candles(token, out_dir, t, transport=None, interval=5):
    """Write candles.csv from the service, leaving out any candle still forming at time t."""
    done = [c for c in ur.get_candles(token, interval, transport=transport)
            if dt.datetime.fromisoformat(c[0]) + dt.timedelta(minutes=interval) <= t]
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "candles.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(CANDLE_FIELDS)
        w.writerows(done)
    return len(done)


def build_scenario(out_dir, day, scenario_path):
    doc = csv_to_scenario.build(day, "nifty_%s" % day, os.path.join(out_dir, "candles.csv"),
                                os.path.join(out_dir, "chain.csv"))
    os.makedirs(os.path.dirname(os.path.abspath(scenario_path)), exist_ok=True)
    with open(scenario_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return doc


def main(argv=None):
    p = argparse.ArgumentParser(description="Record NIFTY snapshots for the paper pipeline. Read-only; sends nothing.")
    p.add_argument("--interval", type=int, default=30, help="seconds between snapshots (minimum %d)" % MIN_POLL_SECONDS)
    p.add_argument("--expiry", default="auto", help="auto (nearest expiry that has quotes), a keyword such as "
                   "next_week, or YYYY-MM-DD")
    p.add_argument("--window", type=int, default=1000, help="keep strikes within this many points of spot")
    p.add_argument("--out-dir", default=None, help="default data/recordings/<date>")
    p.add_argument("--once", action="store_true", help="take one snapshot and stop (connectivity check)")
    a = p.parse_args(argv)
    try:
        token = ur.token_from_env()
    except ur.UpstoxReadError as e:
        sys.exit("%s.\nA terminal opened before `setx` cannot see it. Open a NEW terminal (or restart VS Code), or in "
                 "this one run:\n  $env:%s = [Environment]::GetEnvironmentVariable('%s','User')"
                 % (e, ur.TOKEN_ENV, ur.TOKEN_ENV))
    t = now_ist()
    day = t.date().isoformat()
    out_dir = a.out_dir or os.path.join(PROJECT, "data", "recordings", day)
    interval = max(a.interval, MIN_POLL_SECONDS)
    if not a.once and not in_session(t):
        sys.exit("The market is closed (%s IST). Start this between 09:15 and 15:30 on a trading day, or use --once "
                 "for a connectivity check." % t.strftime("%a %H:%M"))
    try:
        expiry = resolve_expiry(token, a.expiry)
    except ur.UpstoxReadError as e:
        sys.exit("could not choose an expiry: %s" % e)
    print("PAPER project: read-only recording of expiry %s to %s. Nothing is sent or traded. Ctrl+C stops and builds "
          "the file." % (expiry, out_dir))
    taken, failures = 0, 0
    try:
        while True:
            t = now_ist()
            try:
                n = record_snapshot(token, out_dir, t, expiry, a.window)
                taken, failures = taken + 1, 0
                print("%s snapshot %d: %d quotes" % (t.strftime("%H:%M:%S"), taken, n))
            except ur.UpstoxReadError as e:
                failures += 1
                print("%s snapshot failed (%d in a row): %s" % (t.strftime("%H:%M:%S"), failures, e))
                if e.status in (401, 403) or failures >= 10:
                    print("stopping: the token was refused or the service keeps failing")
                    break
            if a.once or not in_session(now_ist()):
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("stopped by you")
    if taken:
        t = now_ist()
        try:
            n = write_candles(token, out_dir, t)
            print("candles: %d completed 5-minute candles" % n)
        except ur.UpstoxReadError as e:
            print("candles failed: %s (chain.csv is kept)" % e)
            return 1
        if not a.once:
            scenario = os.path.join(PROJECT, "data", "nifty_%s.json" % day)
            build_scenario(out_dir, day, scenario)
            print("wrote %s\nNext: python -m nifty_core.cli check-data --scenario %s --config my_real_config.json"
                  % (scenario, scenario))
    return 0 if taken else 1


if __name__ == "__main__":
    sys.exit(main())
