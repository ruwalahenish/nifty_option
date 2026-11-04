"""The human approval step: a ticket for one exact paper order, and the check that a reply is a real "yes" for it.

Pattern re-implemented from the upstream trader-broker design (read, not copied; upstream is unchanged):
    * a reply counts only if it STARTS with "yes"
    * a question, a batch word, a condition or a doubt makes it not count
    * any number in the reply must match this order (defined risk, net debit, strikes, lots ...)
    * the reply is recorded WORD FOR WORD, whether it was accepted or not

Nothing here can approve by itself: it needs a reply string supplied from outside. It cannot tell a human from a
script that supplies the reply; that limit is stated in the documentation.
"""
import re
from collections import namedtuple
from decimal import Decimal

from .risk import engine as risk_engine

YES_WORDS = frozenset(["yes"])
BATCH_WORDS = frozenset(["all", "both", "every", "everything", "any", "batch", "these", "those", "them", "others",
                         "rest", "always", "future", "tomorrow"])
DOUBT_WORDS = frozenset(["but", "if", "unless", "maybe", "perhaps", "probably", "think", "guess", "depends",
                         "except", "however", "though", "not", "no", "never", "wait", "hold", "later", "unsure",
                         "dont", "don't", "cant", "can't", "wouldn't", "shouldn't"])
HEX16 = re.compile(r"\b[0-9a-f]{16}\b")

Ticket = namedtuple("Ticket", "fingerprint candidate_id strategy_type text numbers decision_time")


def _q(d):
    return Decimal(d).quantize(Decimal("0.01"))


def make_ticket(candidate, risk_decision):
    """The plain-language ticket for one risk-APPROVED candidate. Its fingerprint is the first 16 hex of the candidate's."""
    cid = risk_engine.candidate_fingerprint(candidate)
    fp = cid[:16]
    calc = risk_decision.calculation
    s = candidate.spread
    kind = "Bull Call Debit Spread" if candidate.strategy_type == "BULL_CALL_DEBIT" else "Bear Put Debit Spread"
    limit = Decimal(risk_decision.limits["risk_per_trade_inr"])
    text = ("PAPER ORDER (simulated, no real order) %s: %s\n"
            "  BUY  %s at about %s | SELL %s at about %s\n"
            "  %d lot(s) of %d = %d units | width %s points | net debit %s points = INR %s\n"
            "  Total defined risk INR %s (net debit + entry costs + estimated exit costs + slippage); "
            "per-trade limit INR %s\n"
            "  Decided %s | fingerprint %s" % (
                kind, candidate.expiry.isoformat(), candidate.long_contract.symbol,
                candidate.entry_price_long, candidate.short_contract.symbol, candidate.entry_price_short,
                candidate.lots, candidate.lot_size, s.units, candidate.width_points, candidate.net_debit_points,
                _q(candidate.net_debit_inr), _q(calc["total_defined_risk_inr"]), _q(limit),
                candidate.decision_time.strftime("%Y-%m-%d %H:%M IST"), fp))
    nums = [Decimal(candidate.lots), Decimal(candidate.lot_size), Decimal(s.units), candidate.width_points,
            candidate.net_debit_points, _q(candidate.net_debit_inr), _q(calc["total_defined_risk_inr"]),
            candidate.long_contract.strike, candidate.short_contract.strike, candidate.entry_price_long,
            candidate.entry_price_short, limit]
    return Ticket(fp, cid, candidate.strategy_type, text, tuple(nums), candidate.decision_time)


def parse_yes(reply, ticket):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    low = raw.lower()
    words = re.findall(r"[a-z']+", low)
    if not words or words[0] not in YES_WORDS:
        return False, 'only a reply that starts with "yes" counts (not ok, sure, go, do it)'
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch or a standing instruction"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    for token in HEX16.findall(low):
        if token != ticket.fingerprint:
            return False, "the reply names fingerprint %s, but this order is %s" % (token, ticket.fingerprint)
    cleaned = HEX16.sub(" ", low)
    nums = [Decimal(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", cleaned)]
    for n in nums:
        if not any(abs(n - w) < Decimal("0.005") for w in ticket.numbers):
            return False, "the reply says %s, which is not a figure on this order" % n
    return True, "explicit yes for order %s" % ticket.fingerprint
