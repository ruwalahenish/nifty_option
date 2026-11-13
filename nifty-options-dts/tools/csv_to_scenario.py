"""Turn two CSV exports (index candles, option-chain quotes) into one scenario JSON file for the paper pipeline.

    python tools/csv_to_scenario.py --date 2026-11-05 --name nifty_2026-11-05 --candles candles.csv --chain chain.csv
           [--events events.csv] --out data/nifty_2026-11-05.json [--label RECORDED_REAL]

candles.csv columns: start,open,high,low,close,volume        (start = ISO time with +05:30; volume may be empty)
chain.csv columns:   snapshot_time,spot,expiry,strike,type,bid,ask,ltp,bid_qty,ask_qty,volume,open_interest,timestamp
events.csv columns:  when,name,impact                         (optional; impact HIGH, MEDIUM or LOW)

This tool only converts text. It does not fetch data, call any service or place anything. Every number is kept as the
text you exported, never rounded. The pipeline validates the result completely when it loads it (run `check-data`).
Lot size is not accepted from data: it comes from the versioned contract files.
"""
import argparse
import csv
import json
import sys

LABELS = ("RECORDED_REAL", "FIXTURE_SYNTHETIC", "LIVE_READONLY")
CANDLE_COLUMNS = ("start", "open", "high", "low", "close", "volume")
CHAIN_COLUMNS = ("snapshot_time", "spot", "expiry", "strike", "type", "bid", "ask", "ltp", "bid_qty", "ask_qty", "volume",
                 "open_interest", "timestamp")
EVENT_COLUMNS = ("when", "name", "impact")


def _rows(path, columns):
    import os
    if not os.path.isfile(path):
        raise SystemExit("error: %s does not exist. Nothing has been recorded there yet: the live runner or recorder "
                         "(python -m nifty_live.live_run, or python -m nifty_live.recorder) must run during market hours "
                         "first. It writes chain.csv and candles.csv under data/recordings/<date>/." % path)
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in columns if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit("error: %s lacks column(s) %s" % (path, ", ".join(missing)))
        for n, row in enumerate(reader, 2):
            yield n, row


def _text(row, key):
    value = (row.get(key) or "").strip()
    return value if value != "" else None


def _int(row, key, path, n):
    value = _text(row, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise SystemExit("error: %s line %d: %s must be a whole number, got %r" % (path, n, key, value))


def build(date, name, candles_path, chain_path, events_path=None, label="RECORDED_REAL", interval_minutes=5):
    if label not in LABELS:
        raise SystemExit("error: label must be one of %s" % (LABELS,))
    candles = [{"start": _text(r, "start"), "open": _text(r, "open"), "high": _text(r, "high"), "low": _text(r, "low"),
                "close": _text(r, "close"), "volume": _int(r, "volume", candles_path, n)}
               for n, r in _rows(candles_path, CANDLE_COLUMNS)]
    chains, order = {}, []
    for n, r in _rows(chain_path, CHAIN_COLUMNS):
        snap = _text(r, "snapshot_time")
        if snap not in chains:
            chains[snap] = {"snapshot_time": snap, "spot": _text(r, "spot"), "quotes": []}
            order.append(snap)
        elif chains[snap]["spot"] != _text(r, "spot"):
            raise SystemExit("error: %s line %d: spot differs inside the snapshot %s" % (chain_path, n, snap))
        chains[snap]["quotes"].append({
            "expiry": _text(r, "expiry"), "strike": _int(r, "strike", chain_path, n), "type": _text(r, "type"),
            "bid": _text(r, "bid"), "ask": _text(r, "ask"), "ltp": _text(r, "ltp"),
            "bid_qty": _int(r, "bid_qty", chain_path, n), "ask_qty": _int(r, "ask_qty", chain_path, n),
            "volume": _int(r, "volume", chain_path, n), "open_interest": _int(r, "open_interest", chain_path, n),
            "timestamp": _text(r, "timestamp")})
    events = []
    if events_path:
        events = [{"when": _text(r, "when"), "name": _text(r, "name"), "impact": _text(r, "impact")}
                  for _, r in _rows(events_path, EVENT_COLUMNS)]
    return {"schema_version": 1, "scenario": name, "description": "converted from CSV exports by tools/csv_to_scenario.py",
            "underlying": "NIFTY", "date": date, "interval_minutes": interval_minutes, "data_label": label,
            "candles": candles, "chains": [chains[s] for s in order], "events": events, "metadata": {}}


def main(argv=None):
    p = argparse.ArgumentParser(description="CSV exports to a paper-pipeline data file. Converts text only.")
    p.add_argument("--date", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--candles", required=True)
    p.add_argument("--chain", required=True)
    p.add_argument("--events", default=None)
    p.add_argument("--out", required=True)
    p.add_argument("--label", default="RECORDED_REAL")
    p.add_argument("--interval-minutes", type=int, default=5)
    a = p.parse_args(argv)
    doc = build(a.date, a.name, a.candles, a.chain, a.events, a.label, a.interval_minutes)
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")
    sys.stdout.write("wrote %s: %d candles, %d chain snapshots, %d events. Next: python -m nifty_core.cli check-data "
                     "--scenario %s\n" % (a.out, len(doc["candles"]), len(doc["chains"]), len(doc["events"]), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
