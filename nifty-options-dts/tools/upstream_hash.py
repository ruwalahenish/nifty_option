"""Hash the original day-trader-skill repo so a test can prove it was never modified.

Usage:
    python tools/upstream_hash.py            # print the current hash
    python tools/upstream_hash.py --record   # (re)write config/upstream_baseline.json

The hash covers every file outside .git and __pycache__: relative path and the
SHA-256 of the bytes. Recording a new baseline is a deliberate human act and
should only be done if the upstream copy was legitimately re-cloned.
"""
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
UPSTREAM = os.path.normpath(os.path.join(PROJECT, "..", "day-trader-skill"))
BASELINE = os.path.join(PROJECT, "config", "upstream_baseline.json")
SKIP_DIRS = (".git", "__pycache__")


def tree_hash(root):
    """Return (combined_sha256, file_count) for every file under root."""
    entries = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            entries.append((rel, digest))
    entries.sort()
    combined = hashlib.sha256()
    for rel, digest in entries:
        combined.update(("%s\0%s\n" % (rel, digest)).encode("utf-8"))
    return combined.hexdigest(), len(entries)


def git_head(root):
    try:
        out = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"],
                                      stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return None


def main(argv):
    if not os.path.isdir(UPSTREAM):
        print("upstream not found at %s" % UPSTREAM)
        return 2
    digest, count = tree_hash(UPSTREAM)
    if "--record" in argv:
        doc = {"upstream_path": "../day-trader-skill", "git_head": git_head(UPSTREAM),
               "file_count": count, "tree_sha256": digest,
               "note": "Files outside .git and __pycache__. Do not edit by hand."}
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        print("recorded %s files, %s" % (count, digest))
    else:
        print("%s files, %s" % (count, digest))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
