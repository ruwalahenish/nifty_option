"""Generate the deterministic SYNTHETIC scenario fixtures in tests/fixtures/.

    python tools/make_fixtures.py            # write the files
    python tools/make_fixtures.py --check    # exit 1 if the files on disk differ from a fresh run

Nothing here is real market data. Prices come from a seeded random walk for the index and a
Black-76 model for the options, quantised to the contract tick. Lot size is read from the
versioned contract layer (it is used only for order quantities); it is never written to a file.
The same seed always gives the same bytes on the same platform (floating-point library
differences could in principle change a last digit elsewhere).
"""
import copy
import datetime
import json
import math
import os
import random
import sys
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from nifty_core.contracts import ContractSpecs            # noqa: E402
from nifty_core.market import expiry as expiry_mod        # noqa: E402
from nifty_core.market.calendar_nse import NseCalendar    # noqa: E402
from nifty_core.market.ist import IST                     # noqa: E402
from nifty_core.pricing import black76                    # noqa: E402

GENERATOR_VERSION = 1
SCEN_DIR = os.path.join(ROOT, "tests", "fixtures", "scenarios")
INVALID_DIR = os.path.join(ROOT, "tests", "fixtures", "invalid")
CENT, TICK = Decimal("0.01"), Decimal("0.05")
SNAPSHOT_TIMES = ("09:45", "10:30", "12:30", "14:30")
BARS = 77                       # 09:15 to 15:40 in 5-minute bars (NSE session from 2026-08-03)

SCENARIOS = [
    dict(name="bullish_trend", date="2026-09-16", seed=11, prev_close="25000.00", gap=0.0020, drift=0.00018,
         sigma=0.00055, kappa=0.0, base_vol=0.13, liquidity="normal", intent="BULLISH",
         description="Gap-up open, then a steady climb through the session."),
    dict(name="bearish_trend", date="2026-09-17", seed=12, prev_close="25000.00", gap=-0.0020, drift=-0.00018,
         sigma=0.00055, kappa=0.0, base_vol=0.15, liquidity="normal", intent="BEARISH",
         description="Gap-down open, then a steady decline through the session."),
    dict(name="sideways", date="2026-09-23", seed=13, prev_close="25000.00", gap=0.0, drift=0.0,
         sigma=0.00040, kappa=0.12, base_vol=0.12, liquidity="normal", intent="SIDEWAYS",
         description="Small gap, mean-reverting chop inside a narrow range."),
    dict(name="high_volatility", date="2026-10-07", seed=14, prev_close="25000.00", gap=0.0050, drift=0.0,
         sigma=0.00140, kappa=0.0, base_vol=0.28, liquidity="normal", intent="HIGH_VOLATILITY",
         jump_bar=36, jump=-0.0090,
         events=[("11:45", "Synthetic policy announcement (illustrative, not a real event)", "HIGH")],
         description="Wide swings and a sharp jump around a synthetic mid-day announcement."),
    dict(name="illiquid_options", date="2026-09-24", seed=15, prev_close="25000.00", gap=0.0, drift=0.00003,
         sigma=0.00040, kappa=0.05, base_vol=0.13, liquidity="illiquid", intent="ILLIQUID",
         description="Calm index, but option quotes are wide, thin, often with no bid."),
    dict(name="missing_invalid_data", date="2026-09-30", seed=16, prev_close="25000.00", gap=0.0010, drift=0.00005,
         sigma=0.00055, kappa=0.0, base_vol=0.13, liquidity="normal", intent="DATA_DEFECTS",
         defects=True,
         description="Loads, but has candle gaps, a stale snapshot, one-sided and zero bids, missing "
                     "strikes, missing open interest and volume, and a missing snapshot."),
]


# ------------------------------------------------------------------ small helpers
def q2(x):
    return Decimal(repr(float(x))).quantize(CENT, rounding=ROUND_HALF_UP)


def floor_tick(x):
    return (Decimal(repr(float(x))) / TICK).to_integral_value(ROUND_FLOOR) * TICK


def ceil_tick(x):
    return (Decimal(repr(float(x))) / TICK).to_integral_value(ROUND_CEILING) * TICK


def round_tick(x):
    return (Decimal(repr(float(x))) / TICK).to_integral_value(ROUND_HALF_UP) * TICK


def iso(day, hhmm):
    h, m = hhmm.split(":")
    return datetime.datetime.combine(day, datetime.time(int(h), int(m))).replace(tzinfo=IST).isoformat()


def s(d):
    return str(d.quantize(CENT) if isinstance(d, Decimal) else d)


# ------------------------------------------------------------------ index candles
def make_candles(spec, day):
    rng = random.Random(spec["seed"])
    price = Decimal(spec["prev_close"]) * Decimal(repr(1 + spec["gap"]))
    price = price.quantize(CENT)
    anchor = float(price)
    candles = []
    start = datetime.datetime.combine(day, datetime.time(9, 15)).replace(tzinfo=IST)
    for i in range(BARS):
        o = price
        ret = spec["drift"] + spec["sigma"] * rng.gauss(0, 1) - spec["kappa"] * (float(o) / anchor - 1)
        if i == spec.get("jump_bar"):
            ret += spec["jump"]
        c = q2(float(o) * (1 + ret))
        hi = q2(max(float(o), float(c)) * (1 + abs(rng.gauss(0, 1)) * spec["sigma"] * 0.5))
        lo = q2(min(float(o), float(c)) * (1 - abs(rng.gauss(0, 1)) * spec["sigma"] * 0.5))
        hi, lo = max(hi, o, c), min(lo, o, c)
        candles.append({"start": (start + datetime.timedelta(minutes=5 * i)).isoformat(),
                        "open": s(o), "high": s(hi), "low": s(lo), "close": s(c), "volume": None})
        price = c
    return candles


def spot_at(candles, moment):
    """Close of the last candle that finished at or before `moment`."""
    last = None
    for c in candles:
        start = datetime.datetime.fromisoformat(c["start"])
        if start + datetime.timedelta(minutes=5) <= moment:
            last = c
    return Decimal(last["close"]) if last else Decimal(candles[0]["open"])


# ------------------------------------------------------------------ option chains
def build_chain(spec, day, hhmm, candles, expiries, calendar, specs, lot, rng, snap_index):
    moment = datetime.datetime.fromisoformat(iso(day, hhmm))
    spot = spot_at(candles, moment)
    atm = int((spot / 50).to_integral_value(ROUND_HALF_UP)) * 50
    illiquid = spec["liquidity"] == "illiquid"
    quotes = []
    for exp in expiries:
        close_t = calendar.session(exp).close
        exp_dt = datetime.datetime.combine(exp, close_t).replace(tzinfo=IST)
        years = max((exp_dt - moment).total_seconds(), 0) / (365 * 86400.0)
        for k in range(-10, 11):
            strike = atm + 50 * k
            dist = abs(strike - atm)
            vol = spec["base_vol"] + 0.4 * abs(math.log(strike / float(spot)))
            for kind in ("CE", "PE"):
                theo = black76.price(float(spot), strike, years, vol, kind)
                if illiquid:
                    rel = 0.06 if dist <= 100 else 0.20
                else:
                    rel = 0.003 if dist <= 300 else 0.01
                half = max(0.05, rel * theo)
                bid = floor_tick(max(theo - half, 0))
                ask = ceil_tick(theo + half)
                if not illiquid and theo >= 0.10:
                    bid = max(bid, TICK)
                if illiquid and dist >= 200 and rng.random() < 0.5:
                    bid = Decimal("0.00")
                ask = max(ask, bid + TICK) if ask <= bid else ask
                ltp = round_tick(theo + rng.uniform(-half, half) * 0.5)
                if bid > 0:
                    ltp = min(max(ltp, bid), ask)
                else:
                    ltp = min(ltp, ask)
                shape = math.exp(-dist / 200.0)
                jitter = 0.6 + 0.8 * rng.random()
                if illiquid:
                    vol_n, oi_n = int(3000 * shape * jitter), int(8000 * shape * jitter)
                    bqty, aqty = lot * rng.randint(0, 3), lot * rng.randint(0, 3)
                else:
                    vol_n = int(800000 * shape * jitter * (snap_index + 1) / len(SNAPSHOT_TIMES))
                    oi_n = int(2000000 * math.exp(-dist / 250.0) * (0.7 + 0.6 * rng.random()))
                    bqty, aqty = lot * rng.randint(1, 40), lot * rng.randint(1, 40)
                quotes.append({"expiry": exp.isoformat(), "strike": strike, "type": kind,
                               "timestamp": moment.isoformat(), "bid": s(bid), "ask": s(ask), "ltp": s(ltp),
                               "bid_qty": bqty, "ask_qty": aqty, "volume": (vol_n // lot) * lot,
                               "open_interest": (oi_n // lot) * lot})
    return {"snapshot_time": moment.isoformat(), "spot": s(spot), "quotes": quotes}


def apply_defects(doc, rng):
    """Break the data in ways that still load but must be flagged or blocked."""
    candles = doc["candles"]
    gone = {10, 11, 12, 40}
    doc["candles"] = [c for i, c in enumerate(candles) if i not in gone]
    chains = doc["chains"]
    # snapshot 1: every quote is 10 minutes older than the snapshot (stale)
    snap1 = datetime.datetime.fromisoformat(chains[1]["snapshot_time"])
    for q in chains[1]["quotes"]:
        q["timestamp"] = (snap1 - datetime.timedelta(minutes=10)).isoformat()
    # snapshot 2: missing strikes, one-sided and zero bids, missing open interest and volume
    q2_ = chains[2]["quotes"]
    first_exp = min(q["expiry"] for q in q2_)
    atm = min((abs(int(q["strike"]) - int(float(chains[2]["spot"]))), q["strike"]) for q in q2_)[1]
    chains[2]["quotes"] = [q for q in q2_ if not (q["expiry"] == first_exp and q["type"] == "CE"
                                                  and abs(int(q["strike"]) - int(atm)) <= 100)]
    pool = chains[2]["quotes"]
    for q in rng.sample(pool, 10):
        q["bid"] = None
    for q in rng.sample(pool, 5):
        q["ask"] = None
    for q in rng.sample(pool, 8):
        q["open_interest"] = None
    for q in rng.sample(pool, 8):
        q["volume"] = None
    for q in sorted(pool, key=lambda x: -abs(int(x["strike"]) - int(atm)))[:6]:
        q["bid"] = "0.00"
        q["bid_qty"] = 0
    # snapshot 3 (14:30) is missing entirely
    del chains[3]


def day_stats(candles):
    o, c = Decimal(candles[0]["open"]), Decimal(candles[-1]["close"])
    hi = max(Decimal(x["high"]) for x in candles)
    lo = min(Decimal(x["low"]) for x in candles)
    return {"open": s(o), "close": s(c), "high": s(hi), "low": s(lo),
            "day_return_pct": s(((c / o) - 1) * 100), "range_pct": s(((hi - lo) / o) * 100)}


def build_scenario(spec, specs, calendar):
    day = datetime.date.fromisoformat(spec["date"])
    candles = make_candles(spec, day)
    e1 = expiry_mod.next_weekly_expiry(day, calendar, specs, "fixture").date
    e2 = expiry_mod.next_weekly_expiry(e1 + datetime.timedelta(days=1), calendar, specs, "fixture").date
    lot = specs.lot_size(e1, "fixture").value           # for order quantities only; never written to a file
    rng = random.Random(spec["seed"] + 1000)
    chains = [build_chain(spec, day, t, candles, [e1, e2], calendar, specs, lot, rng, i)
              for i, t in enumerate(SNAPSHOT_TIMES)]
    events = [{"when": iso(day, t), "name": n, "impact": imp} for t, n, imp in spec.get("events", [])]
    doc = {"schema_version": 1, "scenario": spec["name"], "description": spec["description"],
           "data_label": "FIXTURE_SYNTHETIC", "underlying": "NIFTY", "date": spec["date"], "interval_minutes": 5,
           "candles": candles, "chains": chains, "events": events,
           "metadata": {"scenario_intent": spec["intent"], "generator_seed": spec["seed"],
                        "generator_version": GENERATOR_VERSION, "expiries": [e1.isoformat(), e2.isoformat()],
                        "previous_close": spec["prev_close"], "day_stats": day_stats(candles),
                        "synthetic_notice": "Synthetic prices from a seeded random walk and a Black-76 model. "
                                            "Not real market data and not evidence of anything."}}
    if spec.get("defects"):
        apply_defects(doc, random.Random(spec["seed"] + 2000))
        doc["metadata"]["day_stats"] = day_stats(doc["candles"])
        doc["metadata"]["defects"] = ["candle gaps at bars 10-12 and 40", "snapshot 10:30 stale by 10 minutes",
                                      "snapshot 12:30 has missing strikes, one-sided and zero bids, missing OI and volume",
                                      "snapshot 14:30 missing"]
    return doc


# ------------------------------------------------------------------ invalid fixtures
def mini(doc):
    """A small valid base to corrupt: 6 candles and one snapshot of the nearest expiry near the money."""
    d = copy.deepcopy(doc)
    d["candles"] = d["candles"][:6]
    chain = d["chains"][0]
    exp = min(q["expiry"] for q in chain["quotes"])
    atm = min(abs(int(q["strike"]) - int(float(chain["spot"]))) for q in chain["quotes"])
    spot_atm = [q for q in chain["quotes"] if q["expiry"] == exp
                and abs(int(q["strike"]) - int(float(chain["spot"]))) == atm][:1]
    near = [q for q in chain["quotes"] if q["expiry"] == exp][:6]
    chain["quotes"] = near
    d["chains"] = [chain]
    d["events"], d["metadata"] = [], {}
    return d


INVALID_CASES = [
    ("naive_timestamp", "NAIVE_TIMESTAMP", lambda d: d["chains"][0]["quotes"][0].update(timestamp="2026-09-16T09:45:00")),
    ("duplicate_contract", "DUPLICATE_CONTRACT", lambda d: d["chains"][0]["quotes"].append(copy.deepcopy(d["chains"][0]["quotes"][0]))),
    ("bid_above_ask", "BID_GT_ASK", lambda d: d["chains"][0]["quotes"][0].update(bid="500.00", ask="400.00")),
    ("negative_price", "NEGATIVE_PRICE", lambda d: d["chains"][0]["quotes"][0].update(ltp="-1.00")),
    ("negative_volume", "NEGATIVE_VOLUME", lambda d: d["chains"][0]["quotes"][0].update(volume=-5)),
    ("negative_open_interest", "NEGATIVE_OPEN_INTEREST", lambda d: d["chains"][0]["quotes"][0].update(open_interest=-1)),
    ("off_tick_price", "OFF_TICK", lambda d: d["chains"][0]["quotes"][0].update(bid="100.03", ask="101.00", ltp="100.50")),
    ("off_grid_strike", "OFF_GRID_STRIKE", lambda d: d["chains"][0]["quotes"][0].update(strike=25025)),
    ("candle_inconsistent", "CANDLE_INCONSISTENT", lambda d: d["candles"][0].update(high="1.00")),
    ("lot_size_in_data", "LOT_SIZE_IN_DATA", lambda d: d["chains"][0]["quotes"][0].update(lot_size=65)),
    ("expired_contract", "EXPIRED_CONTRACT", lambda d: [q.update(expiry="2026-09-15") for q in d["chains"][0]["quotes"]]),
    ("unknown_expiry", "UNKNOWN_EXPIRY", lambda d: [q.update(expiry="2026-09-25") for q in d["chains"][0]["quotes"]]),
    ("quote_from_future", "QUOTE_FROM_FUTURE", lambda d: d["chains"][0]["quotes"][0].update(timestamp="2026-09-16T09:50:00+05:30")),
    ("candle_outside_session", "CANDLE_OUTSIDE_SESSION", lambda d: d["candles"][0].update(start="2026-09-16T09:00:00+05:30")),
    ("market_closed", "MARKET_CLOSED", lambda d: d.update(date="2026-11-10")),
]


def build_invalid(base):
    out = {}
    for name, code, mutate in INVALID_CASES:
        d = mini(base)
        d["scenario"] = "invalid_" + name
        mutate(d)
        d["expected_error_code"] = code
        out["invalid_%s.json" % name] = d
    return out


# ------------------------------------------------------------------ output
def dump(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"


def build_all():
    specs = ContractSpecs.load()
    calendar = NseCalendar.load(specs=specs, data_mode="fixture")
    files = {}
    docs = {}
    for spec in SCENARIOS:
        doc = build_scenario(spec, specs, calendar)
        docs[spec["name"]] = doc
        files[os.path.join(SCEN_DIR, spec["name"] + ".json")] = dump(doc)
    for name, doc in build_invalid(docs["bullish_trend"]).items():
        files[os.path.join(INVALID_DIR, name)] = dump(doc)
    return files


def main(argv):
    files = build_all()
    if "--check" in argv:
        bad = [p for p, text in files.items()
               if not os.path.exists(p) or open(p, encoding="utf-8", newline="").read() != text]
        for p in bad:
            print("differs: %s" % os.path.relpath(p, ROOT))
        return 1 if bad else 0
    os.makedirs(SCEN_DIR, exist_ok=True)
    os.makedirs(INVALID_DIR, exist_ok=True)
    for path, text in files.items():
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    print("wrote %d files" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
