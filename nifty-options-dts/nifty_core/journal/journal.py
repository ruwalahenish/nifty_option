"""The trading journal: APPEND-ONLY JSON Lines, hash-chained.

* The only way this module touches the file is to append one line (mode "a") or to read it. It has no rewrite,
  delete, truncate or edit operation, and a test scans the source to keep it that way.
* Every record carries the hash of the record before it, so an edit made outside the tool breaks the chain and
  `verify` reports where. (This detects tampering; it cannot stop someone with file access from changing a file.)
* Timestamps are the decision time of the run (IST), never the machine clock, so a replay is byte-for-byte repeatable.
* One file holds ONE data source, "fixture" or "real". The two are never mixed: a file that already holds the other
  kind refuses new records.
"""
import hashlib
import json
import os
from collections import namedtuple

from ..errors import JournalError
from ..market.ist import to_ist

GENESIS = "0" * 64
DATA_SOURCES = ("fixture", "real")
KINDS = ("RUN_START", "DECISION", "TICKET", "APPROVAL", "EXECUTION", "EXIT", "POSITION", "CRITICAL", "DAY_LOCKED",
         "MARK", "RUN_END")
VerifyResult = namedtuple("VerifyResult", "ok records problems")


def data_source_for_label(label):
    """Journal data source from a data label: only recorded or live read-only data counts as 'real'."""
    return "fixture" if label == "FIXTURE_SYNTHETIC" else "real"


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash(record_without_hash):
    return hashlib.sha256(_canonical(record_without_hash).encode("utf-8")).hexdigest()


def read_records(path):
    """All records in the file, in order (empty list if the file does not exist). Raises JournalError on bad JSON."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError as e:
                raise JournalError("BAD_JSON", "line %d is not valid JSON: %s" % (n, e))
    return out


def verify(path):
    """Check sequence numbers and the hash chain. Returns VerifyResult(ok, number of records, problems)."""
    problems, prev, sources = [], GENESIS, set()
    try:
        records = read_records(path)
    except JournalError as e:
        return VerifyResult(False, 0, [str(e)])
    for i, rec in enumerate(records):
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("seq") != i + 1:
            problems.append("record %d has seq %r" % (i + 1, rec.get("seq")))
        if rec.get("prev_hash") != prev:
            problems.append("record %d does not follow the record before it (chain broken)" % (i + 1))
        if rec.get("hash") != _hash(body):
            problems.append("record %d was changed after it was written" % (i + 1))
        prev = rec.get("hash")
        sources.add(rec.get("data_source"))
    if len(sources) > 1:
        problems.append("the file mixes data sources: %s" % sorted(str(s) for s in sources))
    return VerifyResult(not problems, len(records), problems)


class Journal(object):
    def __init__(self, path, data_source, run_id):
        if data_source not in DATA_SOURCES:
            raise JournalError("BAD_DATA_SOURCE", "data_source must be one of %s" % (DATA_SOURCES,))
        self.path, self.data_source, self.run_id = path, data_source, run_id
        existing = read_records(path)
        if existing:
            result = verify(path)
            if not result.ok:
                raise JournalError("DAMAGED", "the journal fails verification: %s" % "; ".join(result.problems))
            sources = {r.get("data_source") for r in existing}
            if sources != {data_source}:
                raise JournalError("MIXED_DATA_SOURCES", "%s holds %s data; it will not take %s data" % (
                    path, sorted(sources), data_source))
            self._seq, self._prev = len(existing), existing[-1]["hash"]
        else:
            self._seq, self._prev = 0, GENESIS
            folder = os.path.dirname(os.path.abspath(path))
            if not os.path.isdir(folder):
                os.makedirs(folder)

    def append(self, kind, at, payload):
        """Write one record at the end of the file and return it."""
        if kind not in KINDS:
            raise JournalError("BAD_KIND", "unknown record kind %r" % (kind,))
        body = {"seq": self._seq + 1, "at": to_ist(at).isoformat(), "run_id": self.run_id, "kind": kind,
                "data_source": self.data_source, "paper": True, "payload": json.loads(_canonical(payload)),
                "prev_hash": self._prev}
        record = dict(body, hash=_hash(body))
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(_canonical(record) + "\n")
        self._seq, self._prev = record["seq"], record["hash"]
        return record
