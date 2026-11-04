"""Effective-date-aware, source-tracked values. Shared by the contract and cost layers.

An item is a named list of versions. Each version has:
    value, effective_from, effective_to, status, sources (ids), verified_on, notes

effective_from/effective_to are ISO dates or null. A null effective_from means the start
date is NOT known: the version is trusted only from its verified_on date, so a lookup for
an earlier date is "extrapolated". Extrapolation and UNVERIFIED values are allowed only in
data_mode "fixture" (and are reported as warnings); any other mode refuses them.
"""
import datetime
import json
import os
from collections import namedtuple
from decimal import Decimal

from .errors import ContractError, RateNotCovered, UnverifiedContractValue

PRIMARY = "VERIFIED_PRIMARY"
BROKER = "VERIFIED_BROKER_TARIFF"
UNVERIFIED = "UNVERIFIED"
VERIFIED_STATUSES = (PRIMARY, BROKER)
ALL_STATUSES = (PRIMARY, BROKER, UNVERIFIED)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(ROOT, "config", "sources.json")

Resolved = namedtuple("Resolved", "name value status effective_from effective_to verified_on "
                                  "sources extrapolated warnings")


def parse_date(text, what):
    if text is None:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except (TypeError, ValueError):
        raise ContractError("%s is not an ISO date: %r" % (what, text))


def load_json(path, what="file"):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh, parse_float=Decimal)
    except (OSError, ValueError) as e:
        raise ContractError("cannot read %s %s: %s" % (what, path, e))


def load_sources(path=None):
    doc = load_json(path or SOURCES_PATH, "source registry")
    sources = doc.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ContractError("source registry has no sources")
    for sid, s in sources.items():
        for key in ("title", "url", "sha256", "retrieved_on"):
            if not s.get(key):
                raise ContractError("source %s lacks %s" % (sid, key))
        if len(s["sha256"]) != 64:
            raise ContractError("source %s: sha256 must be 64 hex characters" % sid)
    return sources


class Version(object):
    __slots__ = ("value", "effective_from", "effective_to", "status", "sources", "verified_on", "notes")

    def __init__(self, raw, item, sources, index):
        what = "%s.versions[%d]" % (item, index)
        if "value" not in raw:
            raise ContractError("%s: missing value" % what)
        self.value = raw["value"]
        self.status = raw.get("status")
        if self.status not in ALL_STATUSES:
            raise ContractError("%s: status must be one of %s, got %r" % (what, ALL_STATUSES, self.status))
        self.effective_from = parse_date(raw.get("effective_from"), what + ".effective_from")
        self.effective_to = parse_date(raw.get("effective_to"), what + ".effective_to")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ContractError("%s: effective_to is before effective_from" % what)
        self.verified_on = parse_date(raw.get("verified_on"), what + ".verified_on")
        self.sources = tuple(raw.get("sources") or ())
        self.notes = raw.get("notes", "")
        if self.status in VERIFIED_STATUSES:
            if not self.sources:
                raise ContractError("%s: a verified value needs at least one source id" % what)
            if self.verified_on is None:
                raise ContractError("%s: a verified value needs verified_on" % what)
            for sid in self.sources:
                if sid not in sources:
                    raise ContractError("%s: unknown source id %r" % (what, sid))
        else:
            for sid in self.sources:
                if sid not in sources:
                    raise ContractError("%s: unknown source id %r" % (what, sid))

    def start(self):
        """First date this version is trusted for."""
        if self.effective_from is not None:
            return self.effective_from
        return self.verified_on or datetime.date.max

    def end(self):
        return self.effective_to or datetime.date.max

    def span(self):
        """Date span used only for the overlap check. An unknown start with a known end
        reaches back indefinitely; an unknown start with no end begins at verified_on."""
        if self.effective_from is not None:
            lo = self.effective_from
        elif self.effective_to is not None:
            lo = datetime.date.min
        else:
            lo = self.verified_on or datetime.date.max
        return lo, self.end()


class VersionedItem(object):
    def __init__(self, name, raw, sources):
        self.name = name
        self.unit = raw.get("unit", "")
        versions = raw.get("versions")
        if not isinstance(versions, list) or not versions:
            raise ContractError("%s: needs a non-empty versions list" % name)
        self.versions = [Version(v, name, sources, i) for i, v in enumerate(versions)]
        ordered = sorted(self.versions, key=lambda v: v.span()[0])
        for a, b in zip(ordered, ordered[1:]):
            if b.span()[0] <= a.span()[1]:
                raise ContractError("%s: versions overlap (%s and %s)" % (name, a.span(), b.span()))

    def resolve(self, on, data_mode):
        """The version in force on date `on`. See the module docstring for fixture-mode leniency."""
        fixture = (data_mode == "fixture")
        hit, extrapolated = None, False
        for v in self.versions:
            if v.start() <= on <= v.end():
                hit = v
                break
        if hit is None and fixture:
            # unknown start date: allow use before verified_on, but say so
            for v in self.versions:
                if v.effective_from is None and on <= v.end():
                    hit, extrapolated = v, True
                    break
        if hit is None:
            raise RateNotCovered("%s: no version covers %s" % (self.name, on))
        warnings = []
        if hit.status == UNVERIFIED:
            if not fixture:
                raise UnverifiedContractValue("%s is UNVERIFIED; data_mode=%r needs a verified value" % (
                    self.name, data_mode))
            warnings.append("%s is UNVERIFIED (fixture mode only)" % self.name)
        if extrapolated:
            warnings.append("%s: start date unknown; used on %s, before its verification date %s" % (
                self.name, on, hit.verified_on or "(never verified)"))
        return Resolved(self.name, hit.value, hit.status, hit.effective_from, hit.effective_to,
                        hit.verified_on, hit.sources, extrapolated, tuple(warnings))


class VersionedTable(object):
    """A file of named VersionedItems."""

    def __init__(self, doc, sources):
        self.doc = doc
        self.sources = sources
        items = doc.get("items")
        if not isinstance(items, dict) or not items:
            raise ContractError("no items in table")
        self.items = {name: VersionedItem(name, raw, sources) for name, raw in items.items()}

    def resolve(self, name, on, data_mode):
        if name not in self.items:
            raise ContractError("unknown item %r" % name)
        return self.items[name].resolve(on, data_mode)

    def check(self, name, on, data_mode):
        """Resolve without raising: returns (Resolved or None, problem text or None)."""
        try:
            return self.resolve(name, on, data_mode), None
        except ContractError as e:
            return None, str(e)
