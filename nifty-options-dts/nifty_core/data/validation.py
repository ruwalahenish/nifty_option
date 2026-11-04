"""Data rules that need the versioned contract layer, and the gate for using data in a new entry.

Two kinds of check:
    validate_*   raise DataValidationError: the data is wrong and must not be loaded at all
    check_entry_data / assess_quality   return problems: the data is well-formed but may not be
                 enough to open a position (missing side, stale, expired, synthetic in a real mode)
"""
import datetime
from collections import namedtuple
from decimal import Decimal

from ..errors import ContractError, DataNotUsable, DataValidationError
from ..market import expiry as expiry_mod
from ..market.ist import to_ist
from .models import LABEL_FIXTURE

Issue = namedtuple("Issue", "contract code text")
EntryDataCheck = namedtuple("EntryDataCheck", "ok problems lot_sizes warnings")
BUY, SELL = "BUY", "SELL"


def lot_size_for(contract, specs, data_mode):
    """Lot size for a contract. It comes ONLY from the versioned contract layer."""
    return specs.lot_size(contract.expiry, data_mode).value


def _on_grid(value, step):
    return (value % Decimal(str(step))) == 0


def validate_chain_against_contracts(chain, specs, calendar, data_mode):
    """Strike grid, tick size, real NIFTY expiry date and lot size for every quote. Raises on the first failure."""
    day = to_ist(chain.snapshot_time).date()
    interval = specs.strike_interval(day, data_mode).value
    tick = specs.tick_size(day, data_mode).value
    checked_expiry = {}
    for q in chain.quotes:
        c = q.contract
        if c.expiry < day:
            raise DataValidationError("EXPIRED_CONTRACT", "%s expired before the snapshot date %s" % (c.symbol, day))
        if not _on_grid(c.strike, interval):
            raise DataValidationError("OFF_GRID_STRIKE", "%s: strike is not a multiple of %s" % (c.symbol, interval))
        for name, price in (("bid", q.bid), ("ask", q.ask), ("ltp", q.ltp)):
            if price is not None and not _on_grid(price, tick):
                raise DataValidationError("OFF_TICK", "%s: %s %s is not a multiple of the %s tick" % (
                    c.symbol, name, price, tick))
        if c.expiry not in checked_expiry:
            if not expiry_mod.is_expiry_day(c.expiry, calendar, specs, data_mode):
                raise DataValidationError("UNKNOWN_EXPIRY", "%s: %s is not a NIFTY weekly or monthly expiry date" % (
                    c.symbol, c.expiry))
            try:
                specs.lot_size(c.expiry, data_mode)
            except ContractError as e:
                raise DataValidationError("LOT_SIZE_UNAVAILABLE", "no lot size from the contract layer for %s: %s" % (
                    c.expiry, e))
            checked_expiry[c.expiry] = True


def contract_expired(contract, now, calendar):
    """True if the contract can no longer be opened at `now` (past expiry, or expiry day after the close)."""
    now = to_ist(now)
    today = now.date()
    if contract.expiry < today:
        return True
    if contract.expiry == today:
        session = calendar.session(today)
        return session is None or now.time() >= session.close
    return False


def assert_not_expired_for_entry(contract, now, calendar):
    if contract_expired(contract, now, calendar):
        raise DataValidationError("EXPIRED_CONTRACT", "%s has expired and cannot be used for a new entry" % contract.symbol)


def assess_quality(chain, now, max_age_seconds, wide_spread_pct=Decimal(10)):
    """Quality flags for a chain: one-sided or zero bids, missing volume or open interest, stale
    quotes, wide spreads. These describe the data; whether a flag matters is a strategy decision."""
    issues = []
    for q in chain.quotes:
        c = q.contract
        if q.bid is None or q.ask is None:
            issues.append(Issue(c, "ONE_SIDED", "%s has no %s" % (c.symbol, "bid" if q.bid is None else "ask")))
        elif q.bid == 0:
            issues.append(Issue(c, "ZERO_BID", "%s has a zero bid (nobody to sell to)" % c.symbol))
        if q.open_interest is None:
            issues.append(Issue(c, "MISSING_OPEN_INTEREST", "%s has no open interest" % c.symbol))
        if q.volume is None:
            issues.append(Issue(c, "MISSING_VOLUME", "%s has no volume" % c.symbol))
        age = q.age_seconds(now)
        if age > max_age_seconds:
            issues.append(Issue(c, "STALE", "%s quote is %d s old (limit %d s)" % (c.symbol, age, max_age_seconds)))
        pct = q.spread_pct_of_mid
        if pct is not None and pct > wide_spread_pct:
            issues.append(Issue(c, "WIDE_SPREAD", "%s spread is %.1f%% of mid" % (c.symbol, pct)))
    return issues


def check_entry_data(chain, legs, now, cfg, specs, calendar, data_mode):
    """Can these legs be priced from this chain for a NEW entry at `now`?

    legs: iterable of (OptionContract, "BUY" | "SELL").
    Returns EntryDataCheck. Problems block the entry; they are never repaired or guessed.
    """
    problems, lot_sizes, warnings = [], {}, []
    now = to_ist(now)
    if data_mode != "fixture" and chain.data_label == LABEL_FIXTURE:
        problems.append(("DATA_UNVERIFIED", "synthetic fixture data cannot be used when data_mode=%r" % data_mode))
    if chain.snapshot_time > now:
        problems.append(("QUOTE_FROM_FUTURE", "the chain snapshot %s is after now (%s)" % (chain.snapshot_time, now)))
    for contract, side in legs:
        if side not in (BUY, SELL):
            raise ValueError("side must be BUY or SELL")
        sym = contract.symbol
        try:
            if contract_expired(contract, now, calendar):
                problems.append(("EXPIRED_CONTRACT", "%s has expired" % sym))
        except ContractError as e:
            problems.append(("CALENDAR_UNAVAILABLE", "%s: %s" % (sym, e)))
        try:
            lot_sizes[contract.expiry] = lot_size_for(contract, specs, data_mode)
        except ContractError as e:
            problems.append(("LOT_SIZE_UNVERIFIED", "%s: %s" % (sym, e)))
        q = chain.get(contract)
        if q is None:
            problems.append(("MISSING_QUOTE", "%s is not in the snapshot" % sym))
            continue
        need = q.ask if side == BUY else q.bid
        if need is None or need <= 0:
            problems.append(("NO_%s" % ("ASK" if side == BUY else "BID"),
                             "%s has no usable %s for a %s" % (sym, "ask" if side == BUY else "bid", side)))
        age = q.age_seconds(now)
        if age > cfg.data_stale_after_seconds:
            problems.append(("STALE_DATA", "%s quote is %d s old (limit %d s)" % (sym, age, cfg.data_stale_after_seconds)))
    return EntryDataCheck(not problems, tuple(problems), lot_sizes, tuple(warnings))


def require_entry_data(chain, legs, now, cfg, specs, calendar, data_mode):
    """Like check_entry_data but raises DataNotUsable when anything blocks the entry."""
    result = check_entry_data(chain, legs, now, cfg, specs, calendar, data_mode)
    if not result.ok:
        raise DataNotUsable("data cannot be used for a new entry: " + "; ".join(p[0] for p in result.problems),
                            result.problems)
    return result
